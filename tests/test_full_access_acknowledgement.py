"""Full access is the only exempt mode and needs an explicit risk acknowledgement."""

import httpx
import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.cli import parser
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.transports.api import create_api


def host_for(tmp_path):
    return ProjectHost(tmp_path / "state", tmp_path, lambda scope: DiagnosticModel())


def test_cli_commands_expose_the_acknowledgement_flag(tmp_path):
    prefix = ["--state-dir", str(tmp_path / "state")]
    for command, extra in (
        ("chat", ["hello"]),
        ("repl", []),
        ("permissions", ["session", "--mode", "full", "--expected-revision", "0"]),
    ):
        args = parser().parse_args([*prefix, command, *extra])
        assert args.acknowledge_full_access is False
        flagged = parser().parse_args(
            [*prefix, command, *extra, "--acknowledge-full-access"]
        )
        assert flagged.acknowledge_full_access is True


async def test_new_full_session_requires_an_acknowledgement(tmp_path):
    host = host_for(tmp_path)
    try:
        app = await host.application("one")
        with pytest.raises(DomainError, match="Full access requires"):
            await app.create_session("create", "prompt", permission_mode="full")
        session = await app.create_session(
            "create", "prompt", permission_mode="full", full_access_acknowledged=True
        )
        assert session.permission_mode == "full"
        # risk_acknowledged is not part of the creation digest, so a replay is idempotent.
        replay = await app.create_session(
            "create", "prompt", permission_mode="full", full_access_acknowledged=True
        )
        assert replay.id == session.id
    finally:
        await host.close()


async def test_switching_to_full_requires_an_acknowledgement(tmp_path):
    host = host_for(tmp_path)
    try:
        app = await host.application("one")
        session = await app.create_session("create", "prompt", permission_mode="ask")
        with pytest.raises(DomainError, match="Full access requires"):
            await app.set_permissions(session.id, "full", 0)
        changed = await app.set_permissions(session.id, "full", 0, True)
        assert changed.permission_mode == "full" and changed.permission_revision == 1
        # Switching away from full never needs an acknowledgement.
        relaxed = await app.set_permissions(session.id, "auto", 1)
        assert relaxed.permission_mode == "auto"
    finally:
        await host.close()


async def test_full_mode_audits_every_exempt_operation(tmp_path):
    host = host_for(tmp_path)
    try:
        app = await host.application("one")
        session = await app.create_session(
            "create", "prompt", permission_mode="full", full_access_acknowledged=True
        )
        admission = await app.submit(session.id, "send", "/write exempt.txt value")
        await app.wait_idle()
        done = await app.store.get_admission(admission.id, session.id)
        assert done.status == "completed"
        events = await app.store.event_history(session.id)
        assert any(event["type"] == "approval.exempt" for event in events)
    finally:
        await host.close()


async def test_http_endpoints_enforce_the_acknowledgement(tmp_path):
    host = host_for(tmp_path)
    token = "full-access-test-token-" + "x" * 32
    api = create_api(host, token)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test"
        ) as client:
            blocked = await client.post(
                "/profiles/one/sessions",
                headers=headers,
                json={"request_id": "blocked", "permission_mode": "full"},
            )
            assert blocked.status_code == 409
            assert blocked.json()["code"] == "full_access_acknowledgement_required"
            created = await client.post(
                "/profiles/one/sessions",
                headers=headers,
                json={
                    "request_id": "created",
                    "permission_mode": "full",
                    "risk_acknowledged": True,
                },
            )
            assert created.status_code == 200
            route = f"/profiles/one/sessions/{created.json()['id']}/permissions"
            assert (
                await client.post(
                    route,
                    headers=headers,
                    json={"permission_mode": "ask", "expected_revision": 0},
                )
            ).status_code == 200
            blocked_switch = await client.post(
                route,
                headers=headers,
                json={"permission_mode": "full", "expected_revision": 1},
            )
            assert blocked_switch.status_code == 409
            assert blocked_switch.json()["code"] == "full_access_acknowledgement_required"
            allowed = await client.post(
                route,
                headers=headers,
                json={
                    "permission_mode": "full",
                    "expected_revision": 1,
                    "risk_acknowledged": True,
                },
            )
            assert allowed.status_code == 200
            assert allowed.json()["permission_mode"] == "full"
    finally:
        await host.close()
