"""Regression tests: a stuck tool/model await must fail fast and stay observable.

The v1.1.5 coding flow could hang a whole admission on a single tool await until
the 600s global wall budget expired, with no progress events in between. These
tests lock in the liveness guarantees added in v1.1.6.
"""

import asyncio

import pytest
from sqlalchemy import select

from deep_harness.application.tools import ToolGateway
from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError, ToolCall
from deep_harness.infrastructure import schema as s
from deep_harness.runtime.models import DiagnosticModel


async def _session_gateway(tmp_path, timeout_value):
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: DiagnosticModel())
    app = await host.application("one")
    session = await app.create_session(
        "create", "Analyze", ("list_files",), permission_mode="auto"
    )
    await app.store.admit(
        session, principal="operator", submission_id="live", text="analyze"
    )
    claim = await app.executions.claim(session.id, app.owner)
    gateway = ToolGateway(
        app.scope, session, app.receipts, claim, tool_timeout_seconds=timeout_value
    )
    return host, app, session, claim, gateway


async def test_stuck_readonly_tool_fails_fast(tmp_path):
    """A hung read-only tool is bounded by the tool timeout, not the wall budget.

    Read-only tools have no side effect, so the timeout is surfaced as a normal
    tool error (safe to retry) rather than unknown_effect.
    """
    host, app, session, claim, gateway = await _session_gateway(tmp_path, 0.3)
    original = gateway._invoke

    async def stuck(name, arguments, key):
        if name == "list_files":
            await asyncio.sleep(30)
        return await original(name, arguments, key)

    gateway._invoke = stuck
    try:
        started = asyncio.get_running_loop().time()
        result = await gateway.execute(ToolCall("c1", "list_files", {"path": "."}))
        elapsed = asyncio.get_running_loop().time() - started
        assert "TimeoutError" in result
        assert elapsed < 5, f"tool timeout did not fire promptly ({elapsed:.1f}s)"
        async with app.store.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(s.effects.c.status).where(
                        s.effects.c.id == f"{claim.admission.id}:c1"
                    )
                )
            ).scalar_one_or_none()
        # The dispatched effect is still settled with the error result.
        assert row == "succeeded"
    finally:
        await host.close()


async def test_stuck_mutating_tool_becomes_unknown_effect(tmp_path):
    """A hung mutating tool must become unknown_effect (never auto-retried)."""
    host = ProjectHost(tmp_path / "state", tmp_path, lambda _: DiagnosticModel())
    app = await host.application("one")
    session = await app.create_session(
        "create", "Write", ("write",), permission_mode="full",
        full_access_acknowledged=True,
    )
    await app.store.admit(
        session, principal="operator", submission_id="mut", text="write"
    )
    claim = await app.executions.claim(session.id, app.owner)
    gateway = ToolGateway(
        app.scope, session, app.receipts, claim, tool_timeout_seconds=0.3
    )
    original = gateway._invoke

    async def stuck(name, arguments, key):
        if name == "write":
            await asyncio.sleep(30)
        return await original(name, arguments, key)

    gateway._invoke = stuck
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(DomainError) as exc:
            await gateway.execute(
                ToolCall("c2", "write", {"path": "x.txt", "content": "hi"})
            )
        elapsed = asyncio.get_running_loop().time() - started
        assert exc.value.code == "unknown_effect"
        assert elapsed < 5, f"tool timeout did not fire promptly ({elapsed:.1f}s)"
    finally:
        await host.close()


async def test_model_semaphore_wait_is_bounded():
    """A stuck model slot must not hang every later model call forever.

    `concurrent_requests` can be 1. If a holder never releases the semaphore,
    the wait used to sit outside the request timeout and consume the whole wall
    budget silently. It now fails within request_timeout.
    """
    from types import SimpleNamespace

    from deep_harness.runtime.guard import HarnessGuardMiddleware

    lock = asyncio.Semaphore(0)  # permanently unavailable
    runtime = SimpleNamespace(
        model_lock=lock,
        request_timeout=0.3,
        pause_probe=None,
        claim=SimpleNamespace(admission=SimpleNamespace(generation=1)),
    )
    guard = HarnessGuardMiddleware(runtime)

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await guard.awrap_model_call(object(), object())
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 5, f"semaphore wait did not time out promptly ({elapsed:.1f}s)"


async def test_tool_progress_events_are_emitted(tmp_path):
    """tool.started and tool.completed are written so the UI never looks idle."""
    host, app, session, claim, gateway = await _session_gateway(tmp_path, 5)
    try:
        await gateway.execute(ToolCall("c1", "list_files", {"path": "."}))
        events = await app.store.event_history(session.id)
        kinds = [event["type"] for event in events]
        assert "tool.started" in kinds
        assert "tool.completed" in kinds
        started = next(event for event in events if event["type"] == "tool.started")
        completed = next(event for event in events if event["type"] == "tool.completed")
        assert started["payload"]["tool"] == "list_files"
        assert completed["payload"]["tool"] == "list_files"
        assert completed["payload"]["effect_id"] == f"{claim.admission.id}:c1"
        assert completed["payload"]["elapsed_ms"] >= 0
    finally:
        await host.close()
