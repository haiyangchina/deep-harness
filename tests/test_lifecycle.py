"""Cancellation, graceful shutdown and explicit reconciliation with real ledgers."""

import asyncio

import pytest

from deep_harness.bootstrap import ProjectHost
from deep_harness.domain.models import DomainError
from deep_harness.runtime.models import DiagnosticModel


class BlockingModel(DiagnosticModel):
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, prompt, messages, tools):
        self.entered.set()
        await self.release.wait()
        return await super().complete(prompt, messages, tools)


async def test_cancel_queued_and_running_inputs_fences_execution_without_replaying(tmp_path):
    model = BlockingModel()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Frozen")
        running = await app.submit(session.id, "one", "running")
        async with asyncio.timeout(5):
            await model.entered.wait()
        queued = await app.submit(session.id, "two", "queued")
        assert (await app.cancel(session.id, queued.id)).status == "cancelled"
        cancelled = await app.cancel(session.id, running.id)
        assert cancelled.status == "unknown"
        await app.wait_idle()
        assert not await app.executions.claim(session.id, "another-owner")
        assert len(await app.store.history(session.id)) == 2
    finally:
        await host.close(grace_seconds=0)


async def test_graceful_drain_finishes_current_turn_and_preserves_unstarted_queue(tmp_path):
    model = BlockingModel()
    host = ProjectHost(tmp_path / "state", tmp_path, lambda scope: model)
    app = await host.application("default")
    session = await app.create_session("create", "Frozen")
    one = await app.submit(session.id, "one", "finish me")
    async with asyncio.timeout(5):
        await model.entered.wait()
    two = await app.submit(session.id, "two", "leave queued")
    model.release.set()
    await app.close(grace_seconds=5)
    with pytest.raises(DomainError, match="draining"):
        await app.submit(session.id, "three", "refused")
    await host.close()
    restarted = ProjectHost(tmp_path / "state", tmp_path, recover_queued=False)
    try:
        service = await restarted.application("default")
        assert (await service.store.get_admission(one.id, session.id)).status == "completed"
        assert (await service.store.get_admission(two.id, session.id)).status == "queued"
    finally:
        await restarted.close()


async def test_reconciliation_requires_all_effects_and_is_idempotent_without_unlocking_lane(
    tmp_path,
):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Frozen")
        admission = await app.store.admit(
            session, principal="operator", submission_id="one", text="write"
        )
        claim = await app.executions.claim(session.id, app.owner)
        await app.receipts.prepare_effect(claim, "effect", "proposal")
        await app.receipts.dispatch_effect(claim, "effect")
        await app.executions.settle(claim, "unknown")
        with pytest.raises(DomainError, match="every dispatched effect"):
            await app.reconcile(
                session.id, admission.id, claim.admission.generation, "checked external state", []
            )
        decisions = [{"effect_id": "effect", "proposal_hash": "proposal", "outcome": "applied"}]
        await app.reconcile(
            session.id,
            admission.id,
            claim.admission.generation,
            "checked external state",
            decisions,
        )
        await app.reconcile(
            session.id,
            admission.id,
            claim.admission.generation,
            "checked external state",
            decisions,
        )
        with pytest.raises(DomainError, match="cannot be rewritten"):
            await app.reconcile(
                session.id, admission.id, claim.admission.generation, "different claim", decisions
            )
        assert (await app.store.get_admission(admission.id, session.id)).status == "reconciled"
        assert not await app.executions.claim(session.id, "new-owner")
        branch = await app.fork_session(session.id, "safe-branch")
        assert not await app.store.history(branch.id)
    finally:
        await host.close()
