"""Permission modes govern real tool dispatch and survive upgrades and restarts."""

import json

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from deep_harness.application.permissions import approval_reason
from deep_harness.application.tools import ToolGateway
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError, ProfileScope, Session, ToolCall
from deep_harness.runtime.models import LangChainModel
from deep_harness.transports.api import create_api


class PermissionChat(BaseChatModel):
    calls: list[dict]

    @property
    def _llm_type(self):
        return "permissions-test"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "harness-test", "ls_model_name": "permissions"}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        results = [message.content for message in messages if message.type == "tool"]
        answer = AIMessage(content=json.dumps(results))
        if len(results) < len(self.calls):
            answer = AIMessage(
                content="", tool_calls=[{**self.calls[len(results)], "id": f"step-{len(results)}"}]
            )
        return ChatResult(generations=[ChatGeneration(message=answer)])


def project(tmp_path, calls):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ProjectHost(
        tmp_path / "state", workspace, lambda scope: LangChainModel(PermissionChat(calls=calls))
    )


@pytest.mark.parametrize(
    "command",
    [
        "ECHO safe",
        "Pwd",
        "get-location",
        "write-output safe",
        "echo safe; curl example.invalid",
        "echo $TOKEN",
    ],
)
def test_auto_never_mistakes_external_programs_or_expansions_for_bash_builtins(tmp_path, command):
    session = Session(
        "one", "profile", "operator", "prompt", ("bash",), "standard", permission_mode="auto"
    )
    scope = ProfileScope("profile", tmp_path / "state", tmp_path, tmp_path / "state")
    call = ToolCall("one", "bash", {"command": command, "shell": "bash"})
    assert approval_reason(call, session, scope, lambda path: tmp_path / path) is not None


@pytest.mark.parametrize("engine", ["standard", "deep"])
@pytest.mark.parametrize("mode", ["ask", "auto", "full"])
async def test_modes_control_workspace_write_and_external_read(tmp_path, engine, mode):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside evidence", encoding="utf-8")
    calls = [
        {"name": "write", "args": {"path": "hello.txt", "content": "inside"}},
        {"name": "read", "args": {"path": str(outside)}},
        {"name": "read", "args": {"path": "hello.txt"}},
    ]
    host = project(tmp_path, calls)
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "tools", ("read", "write"), engine=engine, permission_mode=mode,
            full_access_acknowledged=(mode == "full"),
        )
        admission = await app.submit(session.id, "send", "act")
        await app.wait_idle()
        expected = {"ask": ["write", "read"], "auto": ["write", "read"], "full": []}[mode]
        for name in expected:
            pending = await app.store.get_admission(admission.id, session.id)
            assert pending.status == "waiting_approval"
            approval = (await app.sessions.status(session.id))["approvals"][0]
            assert approval["proposal"]["tool_name"] == name
            assert approval["proposal"]["reason"]
            if name == "write":
                assert not (tmp_path / "workspace/hello.txt").exists()
            await app.approve(session.id, approval["id"], pending.generation, "approve")
            await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        result = [json.loads(value) for value in json.loads(done.result)]
        assert result[1]["content"] == "outside evidence"
        assert result[2]["content"] == "inside"
    finally:
        await host.close()


@pytest.mark.parametrize("mode", ["ask", "auto", "full"])
async def test_network_commands_dispatch_only_under_the_selected_policy(
    tmp_path, mode, monkeypatch
):
    dispatched = []

    async def command(arguments, path):
        dispatched.append(arguments.command)
        return {"exit_code": 0, "stdout": "network stub", "stderr": ""}

    monkeypatch.setattr("deep_harness.application.tools.bash", command)
    calls = [{"name": "bash", "args": {"command": "curl https://example.invalid"}}]
    host = project(tmp_path, calls)
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "tools", ("bash",), permission_mode=mode,
            full_access_acknowledged=(mode == "full"),
        )
        admission = await app.submit(session.id, "send", "network")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        if mode != "full":
            assert pending.status == "waiting_approval"
            assert dispatched == []
            approval = (await app.sessions.status(session.id))["approvals"][0]
            with pytest.raises(DomainError, match="pending work"):
                await app.set_permissions(session.id, "full", 0)
            await app.approve(session.id, approval["id"], pending.generation, "approve")
            await app.wait_idle()
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        assert dispatched == ["curl https://example.invalid"]
    finally:
        await host.close()


async def test_auto_checks_each_command_in_one_admission(tmp_path, monkeypatch):
    dispatched = []

    async def command(arguments, path):
        dispatched.append(arguments.command)
        return {"exit_code": 0, "stdout": "stub", "stderr": ""}

    monkeypatch.setattr("deep_harness.application.tools.bash", command)
    host = project(
        tmp_path,
        [
            {"name": "bash", "args": {"command": "echo safe"}},
            {"name": "bash", "args": {"command": "echo risky > risky.txt"}},
        ],
    )
    try:
        app = await host.application("one")
        session = await app.create_session("create", "tools", ("bash",), permission_mode="auto")
        admission = await app.submit(session.id, "send", "run")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval"
        assert dispatched == []
        approval = (await app.sessions.status(session.id))["approvals"][0]
        assert approval["proposal"]["arguments"]["command"] == "echo safe"
        assert approval["proposal"]["scope_key"] == "terminal:workspace"
        assert approval["proposal"]["reusable"] is False
        await app.approve(session.id, approval["id"], pending.generation, "approve")
        await app.wait_idle()
        pending = await app.store.get_admission(admission.id, session.id)
        assert pending.status == "waiting_approval"
        assert dispatched == ["echo safe"]
        approval = (await app.sessions.status(session.id))["approvals"][0]
        assert approval["proposal"]["arguments"]["command"] == "echo risky > risky.txt"
        await app.approve(session.id, approval["id"], pending.generation, "approve")
        await app.wait_idle()
        assert dispatched == ["echo safe", "echo risky > risky.txt"]
        assert (await app.store.get_admission(admission.id, session.id)).status == "completed"
        events = await app.store.event_history(session.id)
        assert not any(event["type"] == "approval.reused" for event in events)
    finally:
        await host.close()


@pytest.mark.parametrize("mode", ["ask", "auto", "full"])
async def test_external_directory_discovery_and_file_creation_follow_policy(tmp_path, mode):
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "evidence.txt").write_text("external evidence", encoding="utf-8")
    calls = [
        {"name": "list_files", "args": {"path": str(directory)}},
        {"name": "search_files", "args": {"path": str(directory), "query": "evidence"}},
        {"name": "write", "args": {"path": str(directory / "created.txt"), "content": "new"}},
    ]
    host = project(tmp_path, calls)
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "tools", ("list_files", "search_files", "write"), permission_mode=mode,
            full_access_acknowledged=(mode == "full"),
        )
        admission = await app.submit(session.id, "send", "inspect outside")
        await app.wait_idle()
        if mode != "full":
            for tool in ("list_files", "search_files", "write"):
                pending = await app.store.get_admission(admission.id, session.id)
                assert pending.status == "waiting_approval"
                assert not (directory / "created.txt").exists()
                approval = (await app.sessions.status(session.id))["approvals"][0]
                assert approval["proposal"]["tool_name"] == tool
                await app.approve(session.id, approval["id"], pending.generation, "approve")
                await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        results = [json.loads(value) for value in json.loads(done.result)]
        assert results[0]["files"] == [(directory / "evidence.txt").as_posix()]
        assert results[1]["matches"][0]["text"] == "external evidence"
        assert (directory / "created.txt").read_text(encoding="utf-8") == "new"
    finally:
        await host.close()


async def test_idle_switch_is_audited_preserves_model_snapshot_and_survives_restart(tmp_path):
    host = project(tmp_path, [])
    try:
        app = await host.application("one")
        session = await app.create_session("create", "unchanged prompt", permission_mode="ask")
        before = await app.sessions.status(session.id)
        changed = await app.set_permissions(session.id, "full", 0, True)
        assert changed.permission_mode == "full" and changed.permission_revision == 1
        assert await app.set_permissions(session.id, "full", 0, True) == changed
        after = await app.sessions.status(session.id)
        assert before["runtime"] == after["runtime"]
        assert changed.prompt == session.prompt and changed.tools == session.tools
        with pytest.raises(DomainError, match="refresh"):
            await app.set_permissions(session.id, "auto", 0)
        fork = await app.fork_session(session.id, "fork")
        assert fork.permission_mode == "full"
        assert (
            len(
                [
                    event
                    for event in await app.store.event_history(session.id)
                    if event["type"] == "permissions.changed"
                ]
            )
            == 1
        )
    finally:
        await host.close()
    restarted = project(tmp_path, [])
    try:
        app = await restarted.application("one")
        restored = await app.store.get_session(session.id, "operator")
        assert restored.permission_mode == "full" and restored.permission_revision == 1
        await app.sessions.validate(restored)
    finally:
        await restarted.close()


async def test_gateway_rejects_unapproved_external_read_even_without_runtime(tmp_path):
    host = project(tmp_path, [])
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        app = await host.application("one")
        session = await app.create_session("create", "tools", ("read",), permission_mode="ask")
        admission = await app.store.admit(
            session, principal="operator", submission_id="one", text="x"
        )
        claim = await app.executions.claim(session.id, app.owner)
        assert claim.admission.id == admission.id
        gateway = ToolGateway(app.scope, session, app.receipts, claim)
        with pytest.raises(DomainError, match="durable approval"):
            await gateway.execute(ToolCall("outside", "read", {"path": str(outside)}))
    finally:
        await host.close()


async def test_api_validates_modes_and_enforces_profile_and_operator_access(tmp_path):
    host = project(tmp_path, [])
    token = "permissions-test-token-at-least-32-characters"
    api = create_api(host, token)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            response = await client.post(
                "/profiles/one/sessions",
                headers=headers,
                json={"request_id": "create", "permission_mode": "auto"},
            )
            assert response.status_code == 200
            session = response.json()
            route = f"/profiles/one/sessions/{session['id']}/permissions"
            body = {
                "permission_mode": "full",
                "expected_revision": 0,
                "risk_acknowledged": True,
            }
            assert (await client.post(route, json=body)).status_code == 401
            assert (
                await client.post(route.replace("/one/", "/two/"), headers=headers, json=body)
            ).status_code == 404
            assert (
                await client.post(
                    route, headers=headers, json={**body, "permission_mode": "unsafe-spelling"}
                )
            ).status_code == 422
            changed = await client.post(route, headers=headers, json=body)
            assert changed.status_code == 200 and changed.json()["permission_mode"] == "full"
    finally:
        await host.close()


async def test_recursive_workspace_read_keeps_sensitive_candidates_outside_grant(tmp_path):
    calls = [
        {"name": "read", "args": {"path": "."}},
        {"name": "list_files", "args": {"path": "."}},
        {"name": "search_files", "args": {"path": ".", "query": "example"}},
    ]
    host = project(tmp_path, calls)
    workspace = tmp_path / "workspace"
    (workspace / ".env").write_text("example-sensitive", encoding="utf-8")
    (workspace / "public.txt").write_text("example-public", encoding="utf-8")
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "Prompt", ("read", "list_files", "search_files"), permission_mode="ask"
        )
        admission = await app.submit(session.id, "scan", "inspect workspace")
        await app.wait_idle()
        final = await app.store.get_admission(admission.id, session.id)
        assert final.status == "completed"
        assert ".env" not in final.result
        assert "example-sensitive" not in final.result
        assert "example-public" in final.result
    finally:
        await host.close()
