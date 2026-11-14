"""Deep sessions run through the production owner and durable ledgers."""

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deep_harness.application.config import ExecutionSettings
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.delegations import DelegationLedger
from deep_harness.runtime.models import LangChainModel


class DeepTestModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "deep-harness-deep-test"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "harness-test", "ls_model_name": "deep"}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="deep works"))])


class DeepToolModel(DeepTestModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(message.type == "tool" for message in messages):
            answer = AIMessage(content="tool complete")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-one",
                        "name": "write_file",
                        "args": {"path": "deep.txt", "content": "saved"},
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


class RepeatedReadModel(DeepTestModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(message.type == "tool" for message in messages):
            answer = AIMessage(content="both read")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {"id": f"read-{number}", "name": "harness_read_file", "args": {"path": "a.txt"}}
                    for number in (1, 2)
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


class DelegatingModel(DeepTestModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(message.type == "tool" for message in messages):
            answer = AIMessage(content="parent used child")
        elif messages[-1].content == "analyze this":
            answer = AIMessage(content="child insight")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "delegate-one",
                        "name": "delegate_task",
                        "args": {"kind": "research", "task": "analyze this"},
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


class CollaboratingModel(DeepTestModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = next(message.content for message in messages if message.type == "human")
        if user == "coordinate":
            if any(message.type == "tool" for message in messages):
                findings = [message.content for message in messages if message.type == "tool"]
                answer = AIMessage(
                    content=json.dumps(
                        {
                            "summary": "Coordinated both specialists",
                            "evidence": [
                                {"source": "a.txt", "observation": findings[0]},
                                {"source": "b.txt", "observation": findings[1]},
                            ],
                            "changes": [],
                            "tests": [],
                            "limitations": [],
                        }
                    )
                )
            else:
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "delegate-a",
                            "name": "delegate_task",
                            "args": {"kind": "research", "task": "inspect a.txt"},
                        },
                        {
                            "id": "delegate-b",
                            "name": "delegate_task",
                            "args": {"kind": "coding", "task": "inspect b.txt"},
                        },
                    ],
                )
        elif any(message.type == "tool" for message in messages):
            answer = AIMessage(content=f"Observed {messages[-1].content}")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {"id": "read-1", "name": "harness_read_file", "args": {"path": user[8:]}}
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


class WritingSpecialistModel(DeepTestModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        user = next(message.content for message in messages if message.type == "human")
        if user == "delegate write":
            if any(message.type == "tool" for message in messages):
                answer = AIMessage(content="parent accepted specialist result")
            else:
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "delegate-write",
                            "name": "delegate_task",
                            "args": {"kind": "coding", "task": "write c.txt"},
                        }
                    ],
                )
        elif any(message.type == "tool" for message in messages):
            answer = AIMessage(content="specialist wrote file")
        else:
            answer = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "write-1",
                        "name": "write_file",
                        "args": {"path": "c.txt", "content": "from child"},
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


async def test_deep_session_uses_production_graph_and_receipts(tmp_path):
    model = LangChainModel(DeepTestModel())
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("deep")
        session = await app.create_session("create", "frozen", engine="deep")
        admission = await app.submit(session.id, "send", "hello")
        await app.wait_idle()
        result = await app.store.get_admission(admission.id, session.id)
        assert result.status == "completed"
        assert result.result == "deep works"
        assert [message["role"] for message in await app.store.history(session.id)] == [
            "user",
            "assistant",
        ]
    finally:
        await host.close()


async def test_deep_write_waits_for_durable_approval(tmp_path):
    def host_factory():
        return ProjectHost(
            tmp_path / "state", tmp_path, lambda scope: LangChainModel(DeepToolModel())
        )

    host = host_factory()
    try:
        app = await host.application("deep")
        session = await app.create_session("create", "frozen", ("write_file",), engine="deep")
        admission = await app.submit(session.id, "send", "write it")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval"
        assert not (tmp_path / "deep.txt").exists()
        interactions = [
            event["payload"]
            for event in await app.store.event_history(session.id)
            if event["type"] == "interaction.open"
        ]
        assert len(interactions) == 1
        await host.close()
        host = host_factory()
        app = await host.application("deep")
        await app.approve(session.id, interactions[0]["id"], pending.generation, "approve")
        await app.wait_idle()
        completed = await app.store.get_admission(admission.id, session.id)
        assert completed.status == "completed"
        assert (tmp_path / "deep.txt").read_text() == "saved"
    finally:
        await host.close()


async def test_deep_repeated_identical_tool_arguments_keep_distinct_call_ids(tmp_path):
    (tmp_path / "a.txt").write_text("same", encoding="utf-8")
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(RepeatedReadModel())
    )
    try:
        app = await host.application("deep")
        session = await app.create_session("create", "frozen", engine="deep")
        admission = await app.submit(session.id, "send", "read twice")
        await app.wait_idle()
        completed = await app.store.get_admission(admission.id, session.id)
        assert completed.status == "completed"
        history = await app.store.history(session.id)
        tools = [message for message in history if message["role"] == "tool"]
        assert {message["tool_call_id"] for message in tools} == {"read-1", "read-2"}
    finally:
        await host.close()


async def test_deep_delegation_is_explicit_and_child_has_no_tools(tmp_path):
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(DelegatingModel())
    )
    try:
        app = await host.application("deep")
        session = await app.create_session("create", "frozen", ("delegate_task",), engine="deep")
        admission = await app.submit(session.id, "send", "delegate")
        await app.wait_idle()
        result = await app.store.get_admission(admission.id, session.id)
        assert result.status == "completed"
        history = await app.store.history(session.id)
        assert [row["role"] for row in history] == ["user", "assistant", "tool", "assistant"]
        assert history[2]["content"] == "child insight"
        with pytest.raises(DomainError, match="only in Deep"):
            await app.create_session("standard", "frozen", ("delegate_task",))
    finally:
        await host.close()


async def test_coding_harness_coordinates_two_readonly_specialists(tmp_path):
    (tmp_path / "a.txt").write_text("ALPHA", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BETA", encoding="utf-8")
    host = ProjectHost(
        tmp_path / "state", tmp_path, lambda scope: LangChainModel(CollaboratingModel())
    )
    try:
        app = await host.application("deep")
        session = await app.create_session(
            "create",
            "frozen",
            ("read_file", "delegate_task"),
            engine="deep",
            harness="coding",
        )
        admission = await app.submit(session.id, "send", "coordinate")
        await app.wait_idle()
        result = await app.store.get_admission(admission.id, session.id)
        assert result.status == "completed"
        report = json.loads(result.result)
        assert report["summary"] == "Coordinated both specialists"
        assert "ALPHA" in report["evidence"][0]["observation"]
        assert "BETA" in report["evidence"][1]["observation"]
        children = await DelegationLedger(app.store, app.executions).list(session.id)
        assert len(children) == 2
        assert all(row["status"] == "completed" for row in children)
        assert [row["role"] for row in await app.store.history(session.id)] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "assistant",
        ]
        other = await host.application("other")
        with pytest.raises(DomainError, match="not accessible"):
            await other.list_delegations(session.id)
        with pytest.raises(DomainError, match="Creation request parameters changed"):
            await app.create_session(
                "create",
                "frozen",
                ("read_file", "delegate_task"),
                engine="deep",
                harness="research",
            )
        with pytest.raises(DomainError, match="Research harness cannot"):
            await app.create_session(
                "invalid", "frozen", ("write_file",), engine="deep", harness="research"
            )
    finally:
        await host.close()


async def test_coding_specialist_write_waits_for_approval_and_survives_restart(tmp_path):
    def host_factory():
        return ProjectHost(
            tmp_path / "state",
            tmp_path,
            lambda scope: LangChainModel(WritingSpecialistModel()),
        )

    host = host_factory()
    try:
        app = await host.application("deep")
        session = await app.create_session(
            "create", "frozen", ("delegate_task", "write_file"), engine="deep"
        )
        admission = await app.submit(session.id, "send", "delegate write")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval"
        assert not (tmp_path / "c.txt").exists()
        proposal = (await app.sessions.status(session.id))["approvals"][0]
        assert proposal["proposal"]["tool_name"] == "write_file"
        await host.close()
        host = host_factory()
        app = await host.application("deep")
        await app.approve(session.id, proposal["id"], pending.generation, "approve")
        await app.wait_idle()
        completed = await app.store.get_admission(admission.id, session.id)
        assert completed.status == "completed"
        assert (tmp_path / "c.txt").read_text() == "from child"
        assert [row["role"] for row in await app.store.history(session.id)] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
    finally:
        await host.close()


async def test_specialized_harness_requires_structured_result(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: LangChainModel(DeepTestModel()))
    try:
        app = await host.application("deep")
        session = await app.create_session(
            "create", "frozen", (), engine="deep", harness="research"
        )
        admission = await app.submit(session.id, "send", "research")
        await app.wait_idle()
        result = await app.store.get_admission(admission.id, session.id)
        assert result.status == "interrupted"
        assert "harness_output" in result.result
    finally:
        await host.close()


async def test_specialist_model_calls_share_parent_budget(tmp_path):
    host = ProjectHost(
        tmp_path / "state",
        tmp_path,
        lambda scope: LangChainModel(DelegatingModel()),
        execution_limits=ExecutionSettings(max_model_calls=2),
    )
    try:
        app = await host.application("deep")
        session = await app.create_session("create", "frozen", ("delegate_task",), engine="deep")
        admission = await app.submit(session.id, "send", "delegate")
        await app.wait_idle()
        result = await app.store.get_admission(admission.id, session.id)
        assert result.status == "interrupted"
        assert "budget_exhausted" in result.result
    finally:
        await host.close()
