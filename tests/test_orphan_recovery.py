"""Runtime recovery of admissions stranded in `started` without a live task.

A crash between claim and settle leaves an admission in `started` while its
execution task is gone. Because `started` is in `BLOCKING`, every later input
for that session stays `queued` forever. Startup ``recover()`` cannot help
when the strand happens after the profile already opened, so the scheduler
must detect and settle such orphans at runtime.
"""

from deep_harness.bootstrap import ProjectHost


async def test_orphan_started_is_settled_at_runtime_and_unblocks_follower(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Frozen")

        first = await app.store.admit(
            session, principal="operator", submission_id="one", text="first"
        )
        await app.store.admit(
            session, principal="operator", submission_id="two", text="follower"
        )

        claim = await app.executions.claim(session.id, app.owner)
        assert claim is not None and claim.admission.id == first.id
        # No model/effect/interaction happened and no execution task owns it:
        # this is the exact state left by an interrupted claim.
        assert await app.executions.claim(session.id, "another-owner") is None

        recovered = await app.executions.recover_stale_active(set(app.active))
        assert [row["admission_id"] for row in recovered] == [first.id]
        assert all(row["status"] == "queued" for row in recovered)
        assert (await app.store.get_admission(first.id, session.id)).status == "queued"

        # After recovery the lane is claimable again and follows FIFO order.
        next_claim = await app.executions.claim(session.id, app.owner)
        assert next_claim is not None and next_claim.admission.id == first.id
        events = await app.store.event_history(session.id)
        assert any(
            row["type"] == "execution.queued"
            and row["payload"]["admission_id"] == first.id
            for row in events
        )
    finally:
        await host.close()


async def test_live_task_is_never_recovered_as_orphan(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Frozen")
        admission = await app.store.admit(
            session, principal="operator", submission_id="one", text="first"
        )
        claim = await app.executions.claim(session.id, app.owner)
        assert claim is not None

        assert await app.executions.recover_stale_active({admission.id}) == []
        assert (await app.store.get_admission(admission.id, session.id)).status == "started"
    finally:
        await host.close()


async def test_dispatched_effect_orphan_becomes_unknown_not_replayed(tmp_path):
    host = ProjectHost(tmp_path / "state", tmp_path)
    try:
        app = await host.application("default")
        session = await app.create_session("create", "Frozen")
        admission = await app.store.admit(
            session, principal="operator", submission_id="one", text="write"
        )
        claim = await app.executions.claim(session.id, app.owner)
        assert claim is not None
        await app.receipts.prepare_effect(claim, "effect-1", "digest")
        await app.receipts.dispatch_effect(claim, "effect-1")

        recovered = await app.executions.recover_stale_active(set(app.active))
        assert [row["status"] for row in recovered] == ["unknown"]
        assert (await app.store.get_admission(admission.id, session.id)).status == "unknown"
        assert await app.executions.claim(session.id, "another-owner") is None
    finally:
        await host.close()
