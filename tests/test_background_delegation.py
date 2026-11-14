"""Background specialists survive parent turns and obey durable steer/stop control."""

import asyncio
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import LangChainModel

REPORT = json.dumps(
    {"summary": "child done", "evidence": [], "changes": [], "tests": [], "limitations": []}
)


class TestChatModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "harness-background-test"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "harness-test", "ls_model_name": "background"}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("Deep runtime must use asynchronous model calls")


class BackgroundModel:
    request_timeout = 30
    identity = {"provider": "test", "model": "background"}

    def __init__(self, started, release):
        self.started = started
        self.release = release

        class Model(TestChatModel):
            async def _agenerate(inner, messages, stop=None, run_manager=None, **kwargs):
                humans = [message.content for message in messages if message.type == "human"]
                if humans[0] == "parent":
                    if any(message.type == "tool" for message in messages):
                        answer = AIMessage(content="parent returned handle")
                    else:
                        answer = AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": "background-one",
                                    "name": "delegate_task",
                                    "args": {
                                        "kind": "research",
                                        "task": "child",
                                        "background": True,
                                    },
                                }
                            ],
                        )
                else:
                    if humans[-1] == "child":
                        started.set()
                        await release.wait()
                    answer = AIMessage(content=REPORT)
                return ChatResult(generations=[ChatGeneration(message=answer)])

        self.model = Model()

    def wrapped(self):
        return LangChainModel(self.model, identity=self.identity)


async def parent_completed(app, session, admission):
    for _ in range(200):
        state = await app.store.get_admission(admission.id, session.id)
        if state.status == "completed":
            return (await app.list_delegations(session.id))[0]
        await asyncio.sleep(0.02)
    raise AssertionError("parent did not complete")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "auto", "full"])
async def test_background_parent_completes_then_steer_runs_next_turn(tmp_path, mode):
    started, release = asyncio.Event(), asyncio.Event()
    model = BackgroundModel(started, release)
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model.wrapped())
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create",
            "frozen",
            ("delegate_task", "check_delegation"),
            engine="deep",
            permission_mode=mode,
        )
        admission = await app.submit(session.id, "parent", "parent")
        row = await parent_completed(app, session, admission)
        assert row["child_session_id"] and row["mode"] == "background"
        await asyncio.wait_for(started.wait(), 5)
        child = await app.store.get_session(row["child_session_id"], "operator")
        assert child.permission_mode == mode
        with pytest.raises(DomainError, match="pending work"):
            await app.set_permissions(session.id, "ask", 0)
        steered = await app.steer_delegation(session.id, row["id"], "steer-1", "steer")
        duplicate = await app.steer_delegation(session.id, row["id"], "steer-1", "steer")
        assert duplicate.id == steered.id
        release.set()
        await app.wait_idle()
        assert (await app.get_delegation(session.id, row["id"]))["status"] == "completed"
        assert (
            await app.store.get_admission(steered.id, row["child_session_id"])
        ).status == "completed"
        assert [
            message["content"]
            for message in await app.store.history(row["child_session_id"])
            if message["role"] == "user"
        ] == ["child", "steer"]
    finally:
        release.set()
        await host.close()


@pytest.mark.asyncio
async def test_stop_fences_running_child_and_queued_steer(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    model = BackgroundModel(started, release)
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model.wrapped())
    try:
        app = await host.application("one")
        session = await app.create_session("create", "frozen", ("delegate_task",), engine="deep")
        admission = await app.submit(session.id, "parent", "parent")
        row = await parent_completed(app, session, admission)
        await asyncio.wait_for(started.wait(), 5)
        steered = await app.steer_delegation(session.id, row["id"], "steer-1", "steer")
        stopped = await app.stop_delegation(session.id, row["id"])
        assert stopped["stop_requested"] == 1
        assert stopped["status"] == "unknown"
        assert (
            await app.store.get_admission(steered.id, row["child_session_id"])
        ).status == "cancelled"
        with pytest.raises(DomainError, match="stopped"):
            await app.steer_delegation(session.id, row["id"], "steer-2", "again")
        assert await app.executions.claim(row["child_session_id"], "other") is None
    finally:
        release.set()
        await host.close()


@pytest.mark.asyncio
async def test_queued_background_child_recovers_after_restart(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    release.set()
    model = BackgroundModel(started, release)
    state = tmp_path / "state"
    host = ProjectHost(state, tmp_path, lambda scope: model.wrapped())
    app = await host.application("one")
    original = app.schedule

    def defer_child(session_id):
        if session_id == session.id:
            original(session_id)

    session = await app.create_session("create", "frozen", ("delegate_task",), engine="deep")
    app.schedule = defer_child
    admission = await app.submit(session.id, "parent", "parent")
    row = await parent_completed(app, session, admission)
    assert (
        await app.store.get_admission(row["child_admission_id"], row["child_session_id"])
    ).status == "queued"
    await host.close()
    restarted = ProjectHost(state, tmp_path, lambda scope: model.wrapped())
    try:
        app = await restarted.application("one")
        await app.wait_idle()
        assert (await app.get_delegation(session.id, row["id"]))["status"] == "completed"
    finally:
        await restarted.close()
