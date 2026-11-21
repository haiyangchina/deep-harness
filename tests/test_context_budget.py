"""Context projection, usage accounting and streamed model evidence."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from deep_harness.application.config import ExecutionSettings
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.runtime.context import (
    TRUNCATION_MARKER,
    ContextProjector,
    serialized_size,
)
from deep_harness.runtime.models import DiagnosticModel, LangChainModel


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
        summary = next(
            row for row in model.requests[-1] if row["id"].startswith("context-summary:")
        )
        assert summary["role"] == "user"
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


class _EmptyHeadResult:
    def mappings(self):
        return self

    def first(self):
        return None


class _Connection:
    async def execute(self, *args, **kwargs):
        return _EmptyHeadResult()


class _ConnectionContext:
    async def __aenter__(self):
        return _Connection()

    async def __aexit__(self, *args):
        return False


class _Engine:
    def connect(self):
        return _ConnectionContext()


class _Store:
    engine = _Engine()


def _claim():
    return SimpleNamespace(admission=SimpleNamespace(session_id="s1", id="a1"))


def test_token_window_is_converted_to_bytes_not_treated_as_bytes():
    model = LangChainModel(None, identity={"context_window_tokens": 32768})
    assert model.context_budget_bytes == (32768 - 2048) * 4
    assert LangChainModel(None, identity={}).context_budget_bytes is None


async def test_default_character_budget_accepts_a_full_read_page(tmp_path):
    (tmp_path / "big.txt").write_text("line-of-text\n" * 2000, encoding="utf-8")
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: DiagnosticModel())
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Read", tools=("read",))
        admission = await app.submit(session.id, "input", "/read big.txt")
        await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
    finally:
        await host.close()


async def test_current_turn_tool_output_is_clipped_not_rejected():
    limits = ExecutionSettings(max_context_characters=6000, summary_characters=256)
    projector = ContextProjector(_Store(), None, limits)
    original = "汉" * 30_000
    messages = [
        {"id": "a1:user", "role": "user", "content": "read the file"},
        {"id": "a1:model:0", "role": "assistant", "content": ""},
        {"id": "a1:tool:1", "role": "tool", "tool_call_id": "1", "content": original},
    ]
    projected = await projector.project(_claim(), "prompt", [], messages, max_context_bytes=122_880)
    tool = next(row for row in projected if row["role"] == "tool")
    assert TRUNCATION_MARKER in tool["content"]
    assert len(tool["content"]) < len(original)
    # Clipping only rewrites the projected view; the persisted transcript is intact.
    assert messages[-1]["content"] == original
    available = limits.max_context_characters - (len("prompt") + serialized_size([]) + 256)
    assert serialized_size(projected) <= available


async def test_oversize_user_input_still_fails_explicitly():
    limits = ExecutionSettings(max_context_characters=2000, summary_characters=256)
    projector = ContextProjector(_Store(), None, limits)
    messages = [{"id": "a1:user", "role": "user", "content": "x" * 20_000}]
    with pytest.raises(DomainError) as error:
        await projector.project(_claim(), "prompt", [], messages)
    assert error.value.code == "context_budget_exhausted"
