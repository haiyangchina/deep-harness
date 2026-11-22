"""Real persistence and restart contracts for local operator workflows."""

import asyncio
import json
import sys

import httpx
import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.cli import build_host, parser
from deep_harness.domain.models import DomainError
from deep_harness.transports.api import create_api
from deep_harness.transports.events import stream_events


async def test_workspace_binding_blocks_recovery_into_another_directory(tmp_path):
    state = tmp_path / "state"
    host = ProjectHost(state, tmp_path)
    app = await host.application("writer")
    session = await app.create_session("create", "Frozen", ("write",))
    admission = await app.submit(session.id, "send", "/write result.txt correct")
    await app.wait_idle()
    pending = await app.store.get_admission(admission.id, session.id)
    await host.close()
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    changed = ProjectHost(state, wrong)
    try:
        with pytest.raises(DomainError, match="Profile workspace"):
            await changed.application("writer")
        assert not (wrong / "result.txt").exists()
    finally:
        await changed.close()
    restored = ProjectHost(state, tmp_path)
    try:
        app = await restored.application("writer")
        await app.approve(session.id, f"{pending.id}:write-1", pending.generation, "approve")
        await app.wait_idle()
        assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "correct"
    finally:
        await restored.close()


async def test_session_pages_and_pending_approvals_are_authorized_and_current(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("one")
        sessions = [await app.create_session(str(i), "Frozen", ("write",)) for i in range(3)]
        await app.store.create_session(principal="other", request_id="hidden", prompt="", tools=())
        token = "a-local-operator-token-longer-than-thirty-two"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_api(host, token)), base_url="http://local"
        ) as client:
            route = "/profiles/one/sessions"
            assert (await client.get(route)).status_code == 401
            first = (await client.get(route, params={"limit": 2}, headers=headers)).json()
            second = (
                await client.get(
                    route, params={"limit": 2, "after": first["next_cursor"]}, headers=headers
                )
            ).json()
            assert {row["id"] for row in first["sessions"] + second["sessions"]} == {
                session.id for session in sessions
            }
            assert second["next_cursor"] is None
            admission = await app.submit(sessions[0].id, "send", "/write denied.txt value")
            await app.wait_idle()
            status_route = f"{route}/{sessions[0].id}/status"
            status = (await client.get(status_route, headers=headers)).json()
            approval = status["approvals"][0]
            assert approval["proposal"]["arguments"]["content"] == "value"
            response = await client.post(
                f"{route}/{sessions[0].id}/approvals",
                headers=headers,
                json={
                    "interaction_id": approval["id"],
                    "generation": approval["generation"],
                    "decision": "deny",
                },
            )
            assert response.status_code == 202
            await app.wait_idle()
            assert (await client.get(status_route, headers=headers)).json()["approvals"] == []
            assert (
                await app.store.get_admission(admission.id, sessions[0].id)
            ).status == "completed"
            assert not (tmp_path / "denied.txt").exists()
            foreign = await client.get(
                f"/profiles/two/sessions/{sessions[0].id}/status", headers=headers
            )
            assert foreign.status_code == 404
            invalid = await client.get(
                f"{route}/{sessions[0].id}/events/stream",
                headers={**headers, "Last-Event-ID": "bad"},
            )
            assert invalid.status_code == 422
            overflow = await client.get(
                f"{route}/{sessions[0].id}/events/stream",
                headers={**headers, "Last-Event-ID": str(2**63)},
            )
            assert overflow.status_code == 422
    finally:
        await host.close()


async def test_sse_reconnect_and_live_commit_do_not_lose_events(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "Frozen")
        await app.submit(session.id, "one", "hello")
        await app.wait_idle()
        journal = await app.store.event_history(session.id)

        async def disconnected():
            return False

        stream = stream_events(app.store, session.id, journal[-2]["seq"], disconnected)
        replay = await anext(stream)
        assert f"id: {journal[-1]['seq']}\n" in replay
        assert "event: execution.completed" in replay
        assert await anext(stream) == ": keep-alive\n\n"
        live = asyncio.create_task(anext(stream))
        try:
            await app.submit(session.id, "two", "again")
            async with asyncio.timeout(5):
                event = await live
            assert "event: admission.accepted" in event
            data = json.loads(event.split("data: ", 1)[1])
            assert data["seq"] > journal[-1]["seq"]
        finally:
            live.cancel()
            await asyncio.gather(live, return_exceptions=True)
            await stream.aclose()
        await app.wait_idle()
    finally:
        await host.close()


async def test_cli_continues_conversation_across_real_processes(tmp_path):
    async def command(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "deep_harness.cli",
            "--state-dir",
            str(tmp_path / "state"),
            "--workspace",
            str(tmp_path),
            "--provider",
            "diagnostic",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(30):
            stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode("utf-8")
        return json.loads(stdout.decode("utf-8"))

    first = await command("chat", "hello", "--submission-id", "first")
    second = await command(
        "chat", "again", "--session", first["session_id"], "--submission-id", "second"
    )
    assert first["session_id"] == second["session_id"]
    retry = await command(
        "chat", "again", "--session", first["session_id"], "--submission-id", "second"
    )
    assert retry["id"] == second["id"]
    sessions = await command("sessions")
    assert [row["id"] for row in sessions["sessions"]] == [first["session_id"]]
    branch = await command("fork", first["session_id"], "--request-id", "fork-retry")
    replay = await command("fork", first["session_id"], "--request-id", "fork-retry")
    assert replay["id"] == branch["id"] != first["session_id"]
    inspected = await command("inspect", branch["id"], "--messages")
    assert len(inspected["messages"]) == 4
    assert len(inspected["lineage"]["copied_admissions"]) == 2
    assert inspected["runtime"]["compatible"]
    continued = await command("chat", "branch continuation", "--session", branch["id"])
    assert continued["status"] == "completed"
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        messages = await app.store.history(first["session_id"])
        assert [message["role"] for message in messages] == ["user", "assistant"] * 2
        assert len(await app.store.history(branch["id"])) == 6
    finally:
        await host.close()


async def test_inspection_does_not_start_or_cancel_queued_work(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path, recover_queued=False)
    app = await host.application("default")
    session = await app.create_session("create", "Frozen")
    queued = await app.store.admit(
        session, principal="operator", submission_id="queued", text="still queued"
    )
    await host.close()
    args = parser().parse_args(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--workspace",
            str(tmp_path),
            "--provider",
            "diagnostic",
            "inspect",
            session.id,
        ]
    )
    reader = build_host(args)
    try:
        service = await reader.application("default")
        await service.wait_idle()
        status = await service.store.session_status(session.id, "operator")
        assert status["latest_admission"].status == "queued"
        assert [row.id for row in status["active_admissions"]] == [queued.id]
        assert len(await service.store.history(session.id)) == 1
    finally:
        await reader.close()


async def test_repl_handles_invalid_approval_and_continues_same_session(
    tmp_path, monkeypatch, capsys
):
    from deep_harness.transports.console import repl

    answers = iter(["/approve bad not-a-generation", '/deny "broken', "hello", "/status", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Frozen")
        assert await repl(app, session) == 0
        output = capsys.readouterr().out
        assert "invalid_generation" in output
        assert "invalid_command" in output
        assert "completed" in output
        assert len(await app.store.history(session.id)) == 2
    finally:
        await host.close()
