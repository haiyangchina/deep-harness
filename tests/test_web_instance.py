"""Offline contracts for the login-free browser instance and business extensions."""

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from deep_harness.application.config import load_model_settings
from deep_harness.application.instance import InstanceHost
from deep_harness.application.instance_config import InstanceSettings, SandboxSettings
from deep_harness.application.shell import BashArgs
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.connectors import HttpTools
from deep_harness.runtime.model_factory import open_model
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.runtime.sandbox import DockerExecutor
from deep_harness.transports.events import stream_events
from deep_harness.transports.web import create_web_api

BASE = "/api/v1/workspaces/reference"


class NamedModel(DiagnosticModel):
    def __init__(self, name):
        self.identity = {"provider": "diagnostic", "name": name}

    async def complete(self, prompt, messages, tools):
        if messages[-1]["role"] == "user" and messages[-1]["content"] == "sandbox fixture":
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "sandbox-test",
                        "name": "bash",
                        "args": {"command": "echo changed > sample.txt"},
                    }
                ],
            }
        if self.identity["name"] == "draft":
            data = {"title": "Printer"} if "ticket" in messages[-1]["content"] else {"amount": 12}
            return {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "status": "complete",
                        "summary": "Draft",
                        "artifacts": [json.dumps(data)],
                        "next_step": None,
                    }
                ),
            }
        response = await super().complete(prompt, messages, tools)
        if response["content"] and not response.get("tool_calls"):
            response["content"] += " [" + self.identity["name"] + "]"
        return response


def settings(tmp_path, **options):
    workspace = tmp_path / "reference"
    workspace.mkdir(exist_ok=True)
    return InstanceSettings.model_validate(
        {
            "workspaces": {"reference": {"path": workspace}},
            "models": {
                name: {
                    "adapter": "diagnostic",
                    "model": name,
                    "capabilities": ["text", "tool_calls"],
                }
                for name in ("one", "two", "draft")
            },
            "default_workspace": "reference",
            "default_model": "one",
            **options,
        }
    )


def instance(tmp_path, **options):
    return InstanceHost(
        settings(tmp_path, **options),
        tmp_path / "state",
        model_factory=lambda entry: NamedModel(entry.settings.name),
    )


async def session(client, *, request_id="create", model_id="two", tools=()):
    response = await client.post(
        f"{BASE}/sessions", json={"request_id": request_id, "model_id": model_id, "tools": tools}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_web_sessions_bind_models_and_reconnect_after_detached_submit(tmp_path):
    host = instance(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<h1>Workbench</h1>", encoding="utf-8")
    api = create_web_api(host, assets)
    async with api.router.lifespan_context(api):
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert "Workbench" in (await client.get("/")).text
            catalog = (await client.get("/api/v1/models")).json()
            assert {row["id"] for row in catalog} == {"one", "two", "draft"}
            assert "base_url" not in catalog[0]
            second = await session(client)
            first = await session(client, request_id="first", model_id="one")
            results = []
            for sid in (first, second):
                response = await client.post(
                    f"{BASE}/sessions/{sid}/runs", json={"submission_id": "send", "text": "hello"}
                )
                assert response.status_code == 202
                results.append((sid, response.json()["run_id"]))
        app = await host.application("reference")
        await app.wait_idle()
        for (sid, rid), model_id in zip(results, ("one", "two"), strict=True):
            admission = await app.store.get_admission(rid, sid)
            assert admission.status == "completed" and admission.result.endswith(f"[{model_id}]")
        sid, rid = results[-1]
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post(
                f"{BASE}/sessions/{sid}/runs", json={"submission_id": "send", "text": "hello"}
            )
            assert response.json()["run_id"] == rid
        events = await app.store.event_history(sid)
        after = events[-2]["seq"]

        async def connected():
            return False

        stream = stream_events(app.store, sid, after, connected)
        try:
            assert (await anext(stream)).startswith(f"id: {events[-1]['seq']}\n")
        finally:
            await stream.aclose()

    restarted = instance(tmp_path, default_model="one")
    try:
        await restarted.start()
        app = await restarted.application("reference")
        status = await app.sessions.status(sid)
        assert status["runtime"]["binding"]["snapshot"]["model_id"] == "two"
        again = await app.submit(sid, "next", "again")
        await app.wait_idle()
        assert (await app.store.get_admission(again.id, sid)).result.endswith("[two]")
    finally:
        await restarted.close()


async def test_web_boundary_and_attachment_contract(tmp_path):
    host = instance(tmp_path, limits={"max_upload_bytes": 24})
    api = create_web_api(host, None)
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1"
        ) as client,
    ):
        assert (
            await client.get("/health", headers={"host": "untrusted.example"})
        ).status_code == 400
        assert (
            await client.post(
                f"{BASE}/sessions",
                json={"request_id": "cross"},
                headers={"origin": "https://untrusted.example"},
            )
        ).status_code == 403
        assert (
            await client.post(f"{BASE}/sessions", data={"request_id": "form"})
        ).status_code == 415
        denied = await client.post(f"{BASE}/sessions", json={"request_id": "no", "tools": ["bash"]})
        assert denied.json()["code"] == "tool_denied"
        sid = await session(client)
        other = await session(client, request_id="other")
        url = f"{BASE}/sessions/{sid}/attachments"
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Harness-Upload": "1",
            "X-Filename": "..%2Fnotes.txt",
        }
        assert (await client.post(url, headers=headers, content=b"x" * 25)).status_code == 413
        uploaded = await client.post(url, headers=headers, content=b"evidence")
        assert uploaded.status_code == 201, uploaded.text
        artifact = uploaded.json()
        assert "/" not in artifact["name"]
        denied = await client.post(
            f"{BASE}/sessions/{other}/runs",
            json={"submission_id": "invalid", "text": "attach", "artifact_ids": [artifact["id"]]},
        )
        assert denied.status_code == 404
        accepted = await client.post(
            f"{BASE}/sessions/{sid}/runs",
            json={"submission_id": "valid", "text": "attach", "artifact_ids": [artifact["id"]]},
        )
        assert accepted.status_code == 202
        app = await host.application("reference")
        await app.wait_idle()
        rid = accepted.json()["run_id"]
        listed = await client.get(f"{BASE}/runs/{rid}/artifacts")
        assert artifact["id"] in {row["id"] for row in listed.json()}
        download = await client.get(f"{BASE}/artifacts/{artifact['id']}")
        assert download.content == b"evidence"
        assert download.headers["content-type"] == "application/octet-stream"


def workflows(tmp_path, operation):
    field, kind = ("title", "string") if operation == "ticket" else ("amount", "number")
    tool_name = f"{operation}.submit"
    return instance(
        tmp_path,
        connectors={
            tool_name: {
                "url": "http://127.0.0.1:9393/submit",
                "reconcile_url": "http://127.0.0.1:9393/status",
                "writes": True,
                "description": "Submit purchase",
                "schema": {
                    "type": "object",
                    "properties": {field: {"type": kind}},
                    "required": [field],
                    "additionalProperties": False,
                },
            }
        },
        agents={
            "procurement": {
                "model": "draft",
                "instructions": "Draft a purchase",
                "workflow": operation,
            }
        },
        workflows={
            operation: {
                "steps": [
                    {"id": "draft", "type": "agent"},
                    {
                        "id": "verify",
                        "type": "verify",
                        "input_from": "draft",
                        "verifier": "json_schema",
                    },
                    {
                        "id": "submit",
                        "type": "tool",
                        "tool": tool_name,
                        "input_from": "verify",
                    },
                ]
            }
        },
    )


@pytest.mark.parametrize(
    "operation,lose_response", [("purchase", False), ("purchase", True), ("ticket", False)]
)
async def test_workflow_verifies_approves_and_never_replays_unknown(
    tmp_path, monkeypatch, operation, lose_response
):
    calls = []

    async def post(self, definition, method, url, headers, **payload):
        if method == "GET":
            assert payload["params"]["effect_id"] == calls[0][1]
            return {"applied": True, "reference": "PO-42"}
        calls.append((method, headers["Idempotency-Key"], payload["json"]))
        if lose_response:
            raise httpx.ReadTimeout("mock")
        return {"purchase_id": "PO-42"}

    monkeypatch.setattr(HttpTools, "request", post)
    host = workflows(tmp_path, operation)
    api = create_web_api(host, None)
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1"
        ) as client,
    ):
        create = await client.post(
            f"{BASE}/workflows/{operation}/runs",
            json={
                "request_id": "workflow",
                "agent_id": "procurement",
                "goal": f"Create test {operation}",
            },
        )
        assert create.status_code == 202, create.text
        jid = create.json()["id"]
        app = await host.application("reference")
        await app.wait_idle()
        detail = (await client.get(f"{BASE}/jobs/{jid}")).json()
        assert detail["stages"][1]["verified"], detail
        final = detail["stages"][2]
        assert final["admission_status"] == "waiting_approval" and not calls
        state = (await client.get(f"{BASE}/sessions/{final['session_id']}")).json()
        approval = state["approvals"][0]
        expected = {"title": "Printer"} if operation == "ticket" else {"amount": 12}
        assert approval["proposal"]["arguments"] == expected
        aid = approval["admission_id"]
        decision = f"{BASE}/runs/{aid}/approvals/{approval['id']}/decision"
        fields = {
            "decision": "approve",
            "generation": approval["generation"],
            "proposal_hash": approval["proposal_hash"],
        }
        refused = await client.post(decision, json={**fields, "proposal_hash": "0" * 64})
        assert refused.status_code == 409
        assert (await client.post(decision, json=fields)).status_code == 200
        await app.wait_idle()
        assert (await client.post(decision, json=fields)).status_code == 200
        await app.wait_idle()
        assert calls == [("POST", approval["id"], expected)]
        detail = (await client.get(f"{BASE}/jobs/{jid}")).json()
        assert detail["status"] == ("blocked" if lose_response else "completed")
        result = (await client.get(f"{BASE}/runs/{aid}")).json()
        assert result["status"] == ("unknown" if lose_response else "completed")
        if lose_response:
            assert (
                await client.post(f"{BASE}/jobs/{jid}/commands", json={"action": "resume"})
            ).status_code == 409
            lookup = await client.get(f"{BASE}/runs/{aid}/effects/{approval['id']}/status")
            assert lookup.json() == {"applied": True, "reference": "PO-42"}
            resolved = await client.post(
                f"{BASE}/runs/{aid}/reconciliation",
                json={
                    "generation": result["generation"],
                    "evidence": "PO-42 confirmed",
                    "effects": [
                        {
                            "effect_id": approval["id"],
                            "proposal_hash": approval["proposal_hash"],
                            "outcome": "applied",
                        }
                    ],
                },
            )
            assert resolved.status_code == 200
    reopened = workflows(tmp_path, operation)
    try:
        await reopened.start()
        await (await reopened.application("reference")).wait_idle()
        assert len(calls) == 1
    finally:
        await reopened.close()


async def test_single_web_owner(tmp_path):
    first = instance(tmp_path)
    second = instance(tmp_path)
    await first.start()
    try:
        with pytest.raises(DomainError, match="Another server"):
            await second.start()
    finally:
        await first.close()
    await second.start()
    await second.close()


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_remote_model_tool_round_trip_uses_configured_adapter(provider, monkeypatch):
    requests = []

    async def respond(reader, writer):
        try:
            raw = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            headers = dict(line.lower().split(": ", 1) for line in raw.splitlines()[1:] if line)
            request = json.loads(await reader.readexactly(int(headers["content-length"])))
            requests.append(request)
            message = {"role": "assistant", "content": "confirmed"}
            reason = "provider-only-fixture-state"
            if len(requests) == 1:
                message = {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reason,
                    "tool_calls": [
                        {
                            "id": "call-read",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"sample.txt"}'},
                        }
                    ],
                }
            body = json.dumps(
                {
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": ("tool_calls" if len(requests) == 1 else "stop"),
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                }
            ).encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("HARNESS_TEST_MODEL_SECRET", "fixture-key")
    async with await asyncio.start_server(respond, "127.0.0.1", 0) as endpoint:
        port = endpoint.sockets[0].getsockname()[1]
        configured = load_model_settings(
            provider=provider,
            name="fixture",
            base_url=f"http://127.0.0.1:{port}/v1",
            reasoning=provider == "deepseek",
            request_timeout_seconds=5,
        )
        model = open_model(configured, "HARNESS_TEST_MODEL_SECRET", remote_window=True)
        user = {"role": "user", "content": "Read sample"}
        first = await model.complete("Use the workspace.", [user], ("read",))
        result = {
            "role": "tool",
            "content": "sample contents",
            "tool_call_id": first["tool_calls"][0]["id"],
        }
        answer = await model.complete("Use the workspace.", [user, first, result], ("read",))
    assert answer["content"] == "confirmed"
    assert requests[1]["messages"][-1]["tool_call_id"] == "call-read"
    if provider == "deepseek":
        assert requests[0]["thinking"]["type"] == "enabled"
        assert requests[1]["messages"][-2]["reasoning_content"] == "provider-only-fixture-state"


@pytest.mark.skipif(os.environ.get("HARNESS_DOCKER_TEST") != "1", reason="explicit Docker smoke")
async def test_docker_executable_isolated(tmp_path):
    workspace = tmp_path.resolve()
    (workspace / "sample.txt").write_bytes(b"original\n")
    sandbox = DockerExecutor(SandboxSettings(image="alpine:3.22"))

    def resolve(name):
        return Path(workspace / name)

    try:
        result = await sandbox.run(
            BashArgs(
                command=(
                    'test "$(id -u)" = 65534 && test "$(ls /sys/class/net)" = lo && '
                    "! (echo denied > /source/sample.txt) 2>/dev/null && "
                    'test "$(cat /source/sample.txt)" = original && echo changed > sample.txt'
                )
            ),
            resolve,
            workspace,
            run_id="test-run",
        )
        continued = await sandbox.run(
            BashArgs(command="cat sample.txt"), resolve, workspace, run_id="test-run"
        )
        assert continued["stdout"].strip() == "changed"
        with pytest.raises(TimeoutError):
            await sandbox.run(
                BashArgs(command="sleep 20", timeout_seconds=1),
                resolve,
                workspace,
                run_id="test-run",
            )
        assert "test-run" not in sandbox.workspaces
        with pytest.raises(DomainError, match="workspace was lost"):
            await sandbox.prepare("test-run", workspace, previously_used=True)
    finally:
        await sandbox.close()
    assert result["exit_code"] == 0, result
    assert "+changed" in result["workspace_diff"]
    assert (workspace / "sample.txt").read_text(encoding="utf-8") == "original\n"


@pytest.mark.skipif(os.environ.get("HARNESS_DOCKER_TEST") != "1", reason="explicit Docker smoke")
async def test_web_sandbox_approval_and_diff(tmp_path):
    workspace = tmp_path / "reference"
    host = instance(
        tmp_path,
        workspaces={"reference": {"path": workspace, "mode": "sandbox"}},
        sandbox={"image": "alpine:3.22"},
    )
    (workspace / "sample.txt").write_bytes(b"original\n")
    api = create_web_api(host, None)
    async with (
        api.router.lifespan_context(api),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://127.0.0.1"
        ) as client,
    ):
        app = await host.application("reference")
        sid = await session(client, tools=("bash",))
        reply = await client.post(
            f"{BASE}/sessions/{sid}/runs",
            json={"submission_id": "sandbox", "text": "sandbox fixture"},
        )
        assert reply.status_code == 202, reply.text
        run_url = f"{BASE}/runs/{reply.json()['run_id']}"
        await app.wait_idle()
        current = (await client.get(run_url)).json()
        assert current["status"] == "waiting_approval", current
        state = (await client.get(f"{BASE}/sessions/{sid}")).json()
        approval = state["approvals"][0]
        decision = await client.post(
            f"{run_url}/approvals/{approval['id']}/decision",
            json={
                "decision": "approve",
                "generation": approval["generation"],
                "proposal_hash": approval["proposal_hash"],
            },
        )
        assert decision.status_code == 200, decision.text
        await app.wait_idle()
        current = (await client.get(run_url)).json()
        assert current["status"] == "completed", current
        artifacts = (await client.get(f"{run_url}/artifacts")).json()
        diff = next(row for row in artifacts if row["kind"] == "diff")
        download = await client.get(f"{BASE}/artifacts/{diff['id']}")
        assert "+changed" in download.text
        assert (workspace / "sample.txt").read_bytes() == b"original\n"
        assert not host.executor.workspaces
