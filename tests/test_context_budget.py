"""Context projection, usage accounting and streamed model evidence."""

from sqlalchemy import select

from deep_harness.application.config import ExecutionSettings
from deep_harness.bootstrap import ProjectHost
from deep_harness.infrastructure import schema as s
from deep_harness.runtime.models import DiagnosticModel


class ObservedModel(DiagnosticModel):
    def __init__(self):
        self.requests = []

    async def complete(self, prompt, messages, tools):
        self.requests.append(messages)
        return {
            "role": "assistant",
            "content": "ok",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 5,
                "total_tokens": 105,
            },
        }


class StreamingModel:
    identity = {"provider": "stream-test", "version": 1}
    request_timeout = 10

    async def stream(self, prompt, messages, tools):
        yield {"kind": "delta", "text": "a" * 130}
        yield {"kind": "delta", "text": "b" * 10}
        yield {
            "kind": "result",
            "response": {
                "role": "assistant",
                "content": "a" * 130 + "b" * 10,
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            },
        }


async def test_compaction_keeps_transcript_and_uses_archived_summary(tmp_path):
    model = ObservedModel()
    limits = ExecutionSettings(max_context_characters=2000, summary_characters=256)
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model, execution_limits=limits)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Compact context", tools=())
        for ordinal in range(4):
            admission = await app.submit(session.id, f"send-{ordinal}", str(ordinal) * 380)
            await app.wait_idle()
            assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        history = await app.store.history(session.id)
        assert len(history) == 8
        assert [row["content"] for row in history[::2]] == [str(i) * 380 for i in range(4)]
        assert any(row["role"] == "system" for row in model.requests[-1])
        async with app.store.engine.connect() as conn:
            archives = list((await conn.execute(select(s.context_archives))).mappings())
        assert archives and archives[0]["messages"]
        assert any(
            row["type"] == "context.compacted" for row in await app.store.event_history(session.id)
        )
    finally:
        await host.close()


async def test_budget_rejects_oversize_input_before_model_call(tmp_path):
    model = ObservedModel()
    host = ProjectHost(
        tmp_path / "state",
        tmp_path,
        lambda scope: model,
        execution_limits=ExecutionSettings(max_run_tokens=100),
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Budget")
        admission = await app.submit(session.id, "input", "x" * 1000)
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "interrupted"
        assert model.requests == []
    finally:
        await host.close()


async def test_stream_deltas_and_provider_usage_are_durable(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: StreamingModel())
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Stream")
        admission = await app.submit(session.id, "input", "say something")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        events = await app.store.event_history(session.id)
        fragments = (row["payload"]["text"] for row in events if row["type"] == "model.delta")
        assert "".join(fragments) == "a" * 130 + "b" * 10
        from deep_harness.runtime.inference import usage_totals

        assert await usage_totals(app.store, session.id) == {
            "model_calls": 1,
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "contains_estimates": False,
        }
    finally:
        await host.close()
