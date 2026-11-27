"""Atomic claims, fencing and explicit uncertain-outcome recovery."""

import time
from dataclasses import replace

from sqlalchemy import delete, func, insert, select, update

from deep_harness.domain.models import DomainError, ExecutionClaim
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.store import BusinessStore, admission_from

BLOCKING = ("started", "waiting_approval", "job_paused", "unknown", "interrupted", "reconciled")


class ExecutionLedger:
    def __init__(self, store: BusinessStore):
        self.store = store

    async def cancel_pending(self, session_id, admission_id):
        async with self.store.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.admissions).where(
                            s.admissions.c.id == admission_id,
                            s.admissions.c.session_id == session_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise DomainError("admission_not_found", "Admission is not accessible")
            if row["status"] == "started":
                raise DomainError(
                    "execution_busy", "Execution handoff is in progress; retry cancellation"
                )
            if row["status"] not in {"queued", "waiting_approval", "job_paused"}:
                return admission_from(row)
            status = "cancelled" if row["status"] == "queued" else "interrupted"
            await conn.execute(
                update(s.admissions)
                .where(s.admissions.c.id == admission_id)
                .values(
                    status=status,
                    generation=row["generation"] + 1,
                    owner=None,
                    result="Cancelled by operator; use a fork for interrupted conversations",
                )
            )
            await self.store.append_event(
                conn, session_id, f"execution.{status}", {"admission_id": admission_id}
            )
        return await self.store.get_admission(admission_id, session_id)

    async def _job_allows_claim(self, conn, session_id):
        row = (
            await conn.execute(
                select(s.jobs.c.status, s.jobs.c.deadline)
                .join(s.job_stage_attempts, s.job_stage_attempts.c.job_id == s.jobs.c.id)
                .where(s.job_stage_attempts.c.session_id == session_id)
            )
        ).first()
        return row is None or (
            row.status == "running" and (row.deadline is None or row.deadline > time.time())
        )

    async def _queued_candidate(self, conn, session_id):
        if not await self._job_allows_claim(conn, session_id):
            return None
        stopped = (
            await conn.execute(
                select(s.delegations.c.id).where(
                    s.delegations.c.child_session_id == session_id,
                    s.delegations.c.stop_requested == 1,
                )
            )
        ).first()
        if stopped:
            return None
        blocked = (
            await conn.execute(
                select(s.admissions.c.id).where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status.in_(BLOCKING),
                )
            )
        ).first()
        if blocked:
            return None
        return (
            (
                await conn.execute(
                    select(s.admissions)
                    .where(
                        s.admissions.c.session_id == session_id,
                        s.admissions.c.status == "queued",
                    )
                    .order_by(s.admissions.c.ordinal)
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )

    async def queued_candidate(self, session_id):
        async with self.store.engine.connect() as conn:
            row = await self._queued_candidate(conn, session_id)
            return admission_from(row) if row is not None else None

    async def finish_workspace_preflight(self, admission, status, result, code):
        async with self.store.transaction() as conn:
            changed = await conn.execute(
                update(s.admissions)
                .where(
                    s.admissions.c.id == admission.id,
                    s.admissions.c.status == "queued",
                    s.admissions.c.generation == admission.generation,
                )
                .values(status=status, result=result)
            )
            if changed.rowcount:
                await self.store.append_event(
                    conn, admission.session_id, f"execution.{status}",
                    {"admission_id": admission.id, "code": code, "result": result},
                )
            return bool(changed.rowcount)

    async def workspace_phase(self, admission, phase, **detail):
        async with self.store.transaction() as conn:
            queued = await conn.scalar(
                select(s.admissions.c.id).where(
                    s.admissions.c.id == admission.id,
                    s.admissions.c.status == "queued",
                    s.admissions.c.generation == admission.generation,
                )
            )
            if queued is None:
                raise DomainError("workspace_preflight_stale", "Input is no longer queued")
            await self.store.append_event(
                conn, admission.session_id, f"workspace.{phase}",
                {"admission_id": admission.id, "phase": phase, **detail},
            )

    async def retry_workspace(self, session_id, admission_id):
        allowed = ("workspace_busy", "workspace_timeout")
        async with self.store.transaction() as conn:
            changed = await conn.execute(
                update(s.admissions)
                .where(
                    s.admissions.c.id == admission_id,
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.generation == 0,
                    s.admissions.c.status == "interrupted",
                    s.admissions.c.result.in_(
                        [f"Workspace preparation stopped [{code}]" for code in allowed]
                    ),
                )
                .values(status="queued", result=None)
            )
            if not changed.rowcount:
                raise DomainError(
                    "workspace_retry_denied", "Workspace preparation cannot be retried"
                )
            await self.store.append_event(
                conn, session_id, "workspace.retry", {"admission_id": admission_id},
            )

    async def claim_summary_retry(self, admission, owner):
        async with self.store.transaction() as conn:
            pending_effect = await conn.scalar(
                select(s.effects.c.id).where(
                    s.effects.c.admission_id == admission.id, s.effects.c.status == "dispatched",
                ).limit(1)
            )
            began_finalizing = await conn.scalar(
                select(s.events.c.seq).where(
                    s.events.c.session_id == admission.session_id,
                    s.events.c.type == "model.started",
                    s.events.c.payload["phase"].as_string() == "finalizing",
                    s.events.c.payload["id"].as_string().startswith(admission.id + ":",
                                                                   autoescape=True),
                ).limit(1)
            )
            if pending_effect is not None or began_finalizing is None:
                raise DomainError("summary_retry_denied", "No safe summary-only retry is available")
            changed = await conn.execute(
                update(s.admissions)
                .where(
                    s.admissions.c.id == admission.id,
                    s.admissions.c.session_id == admission.session_id,
                    s.admissions.c.status == "interrupted",
                    s.admissions.c.generation == admission.generation,
                )
                .values(status="started", generation=admission.generation + 1, owner=owner)
            )
            if not changed.rowcount:
                raise DomainError("summary_retry_denied", "The input state changed")
            claim = ExecutionClaim(
                replace(admission, generation=admission.generation + 1, status="started"), owner,
            )
            await self.store.append_event(
                conn, admission.session_id, "summary.retry_started",
                {"admission_id": admission.id, "generation": claim.admission.generation},
            )
            return claim

    async def claim(
        self, session_id: str, owner: str, *, expected_admission=None,
    ) -> ExecutionClaim | None:
        async with self.store.transaction() as conn:
            row = await self._queued_candidate(conn, session_id)
            if row is None or (expected_admission is not None and row["id"] != expected_admission):
                return None
            admission = replace(
                admission_from(row),
                status="started",
                generation=row["generation"] + 1,
            )
            await conn.execute(
                update(s.admissions)
                .where(
                    s.admissions.c.id == admission.id,
                )
                .values(status="started", generation=admission.generation, owner=owner)
            )
            return ExecutionClaim(admission, owner)

    async def assert_claim(self, conn, claim: ExecutionClaim) -> None:
        row = (
            await conn.execute(
                select(s.admissions.c.id).where(
                    s.admissions.c.id == claim.admission.id,
                    s.admissions.c.generation == claim.admission.generation,
                    s.admissions.c.owner == claim.owner,
                    s.admissions.c.status == "started",
                )
            )
        ).first()
        if row is None:
            raise DomainError("lease_lost", "Execution no longer owns this admission")

    async def tool_count(self, admission_id):
        async with self.store.engine.connect() as conn:
            return await conn.scalar(
                select(func.count()).select_from(s.tool_attempts).where(
                    s.tool_attempts.c.admission_id == admission_id,
                )
            )

    async def reserve_tools(self, claim, call_ids, limit):
        ids = set(call_ids)
        if not ids:
            return
        admission_id = claim.admission.id
        async with self.store.transaction() as conn:
            await self.assert_claim(conn, claim)
            known = (
                (
                    await conn.execute(
                        select(s.tool_attempts.c.call_id).where(
                            s.tool_attempts.c.admission_id == admission_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            fresh = ids.difference(known)
            if fresh and len(known) + len(fresh) > limit:
                raise DomainError("budget_exhausted", "Per-run tool call budget exhausted")
            if fresh:
                await conn.execute(
                    insert(s.tool_attempts),
                    [{"admission_id": admission_id, "call_id": key} for key in sorted(fresh)],
                )

    async def active_seconds(self, admission_id):
        async with self.store.engine.connect() as conn:
            return await conn.scalar(
                select(s.admissions.c.active_seconds).where(s.admissions.c.id == admission_id)
            )

    async def settle(
        self,
        claim: ExecutionClaim,
        status: str,
        result: str | None = None,
        *,
        interactions=(),
        elapsed=0.0,
    ):
        async with self.store.transaction() as conn:
            await self.assert_claim(conn, claim)
            if status == "waiting_approval" and interactions:
                await conn.execute(
                    insert(s.approval_waits).values(
                        admission_id=claim.admission.id,
                        generation=claim.admission.generation,
                        interaction_ids=list(dict.fromkeys(interactions)),
                    )
                )
            await conn.execute(
                update(s.admissions)
                .where(
                    s.admissions.c.id == claim.admission.id,
                )
                .values(
                    status=status,
                    result=result,
                    active_seconds=s.admissions.c.active_seconds + elapsed,
                )
            )
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                f"execution.{status}",
                {
                    "admission_id": claim.admission.id,
                    "generation": claim.admission.generation,
                    "elapsed_seconds": round(elapsed, 3),
                    "result": result,
                },
            )

    async def paused_job_admission(self, session_id):
        async with self.store.engine.connect() as conn:
            return await conn.scalar(
                select(s.admissions.c.id).where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status == "job_paused",
                )
            )

    async def resume(
        self,
        admission_id: str,
        session_id: str,
        owner: str,
        *,
        expected_generation=None,
        job_paused=False,
    ) -> ExecutionClaim | None:
        async with self.store.transaction() as conn:
            if not await self._job_allows_claim(conn, session_id):
                return None
            row = (
                (
                    await conn.execute(
                        select(s.admissions).where(
                            s.admissions.c.id == admission_id,
                            s.admissions.c.session_id == session_id,
                            s.admissions.c.status
                            == ("job_paused" if job_paused else "waiting_approval"),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None or (
                expected_generation is not None and row["generation"] != expected_generation
            ):
                return None
            if not job_paused:
                wait = (
                    (
                        await conn.execute(
                            select(s.approval_waits).where(
                                s.approval_waits.c.admission_id == admission_id,
                                s.approval_waits.c.generation == row["generation"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if wait is None or not wait["interaction_ids"]:
                    return None
                decisions = (
                    (
                        await conn.execute(
                            select(s.interactions.c.decision).where(
                                s.interactions.c.admission_id == admission_id,
                                s.interactions.c.id.in_(wait["interaction_ids"]),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(decisions) != len(wait["interaction_ids"]) or None in decisions:
                    return None
                await conn.execute(
                    delete(s.approval_waits).where(s.approval_waits.c.admission_id == admission_id)
                )
            admission = replace(
                admission_from(row),
                status="started",
                generation=row["generation"] + 1,
            )
            await conn.execute(
                update(s.admissions)
                .where(
                    s.admissions.c.id == admission_id,
                )
                .values(status="started", owner=owner, generation=admission.generation)
            )
            return ExecutionClaim(admission, owner)

    async def report_checkpoint(self, claim):
        async with self.store.transaction() as conn:
            await self.assert_claim(conn, claim)
            if not await self._has_report_checkpoint(conn, claim.admission):
                await self.store.append_event(
                    conn,
                    claim.admission.session_id,
                    "coding.report_pending",
                    {"admission_id": claim.admission.id},
                )

    async def report_pending(self, admission):
        async with self.store.engine.connect() as conn:
            return await self._has_report_checkpoint(conn, admission)

    @staticmethod
    async def _has_report_checkpoint(conn, admission):
        return (
            await conn.scalar(
                select(s.events.c.seq)
                .where(
                    s.events.c.session_id == admission.session_id,
                    s.events.c.type == "coding.report_pending",
                    s.events.c.payload["admission_id"].as_string() == admission.id,
                )
                .limit(1)
            )
            is not None
        )

    async def _has_summary_retry(self, conn, admission):
        return await conn.scalar(
            select(s.events.c.seq).where(
                s.events.c.session_id == admission.session_id,
                s.events.c.type == "summary.retry_started",
                s.events.c.payload["admission_id"].as_string() == admission.id,
                s.events.c.payload["generation"].as_integer() == admission.generation,
            ).limit(1)
        ) is not None

    async def recover(self) -> list[str]:
        """Recover only work that completed its graph and has no outstanding effect."""
        async with self.store.transaction() as conn:
            rows = (
                (
                    await conn.execute(
                        select(s.admissions).where(
                            s.admissions.c.status == "started",
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                admission = admission_from(row)
                pending_effect = await conn.scalar(
                    select(s.effects.c.id)
                    .where(
                        s.effects.c.admission_id == admission.id,
                        s.effects.c.status == "dispatched",
                    )
                    .limit(1)
                )
                report_only = pending_effect is None and await self._has_report_checkpoint(
                    conn, admission
                )
                if pending_effect is None and await self._has_summary_retry(conn, admission):
                    status = "interrupted"
                else:
                    status = "queued" if report_only else "unknown"
                await conn.execute(
                    update(s.admissions)
                    .where(s.admissions.c.id == admission.id)
                    .values(status=status, owner=None)
                )
                await self.store.append_event(
                    conn,
                    admission.session_id,
                    f"execution.{status}",
                    {"admission_id": admission.id},
                )
            queued = await conn.execute(
                select(s.admissions.c.session_id)
                .where(
                    s.admissions.c.status == "queued",
                )
                .distinct()
            )
            return list(queued.scalars())

    async def recover_stale_active(
        self, live_ids: set[str], session_id: str | None = None
    ) -> list[dict]:
        """Settle `started` admissions that no longer have a live execution task.

        Startup ``recover()`` only runs once, so a crash between claim and settle
        that happens later leaves the admission in ``started`` forever. Since
        ``started`` is blocking, every later input in that session is starved.
        An admission that is started but absent from the owner's live task set
        is provably no longer executing. Settlement follows the same
        no-replay principle as startup recovery:

        * no dispatched effect and no model receipt means no observable work
          was committed, so the input is safely re-queued;
        * an outstanding dispatched effect, or a completed report checkpoint,
          is handled as in ``recover()`` (report-only re-queues, otherwise
          ``unknown``) and still requires explicit reconciliation.
        """
        recovered: list[dict] = []
        async with self.store.transaction() as conn:
            query = select(s.admissions).where(s.admissions.c.status == "started")
            if session_id is not None:
                query = query.where(s.admissions.c.session_id == session_id)
            rows = (await conn.execute(query)).mappings().all()
            for row in rows:
                admission = admission_from(row)
                if admission.id in live_ids:
                    continue
                pending_effect = await conn.scalar(
                    select(s.effects.c.id)
                    .where(
                        s.effects.c.admission_id == admission.id,
                        s.effects.c.status == "dispatched",
                    )
                    .limit(1)
                )
                model_receipt = await conn.scalar(
                    select(s.model_receipts.c.id)
                    .where(s.model_receipts.c.admission_id == admission.id)
                    .limit(1)
                )
                report_only = pending_effect is None and await self._has_report_checkpoint(
                    conn, admission
                )
                untouched = pending_effect is None and model_receipt is None
                if pending_effect is None and await self._has_summary_retry(conn, admission):
                    status, reason = "interrupted", "summary_retry"
                else:
                    status = "queued" if (untouched or report_only) else "unknown"
                    reason = "untouched" if untouched else ("report" if report_only else "effect")
                await conn.execute(
                    update(s.admissions)
                    .where(s.admissions.c.id == admission.id)
                    .values(status=status, owner=None)
                )
                await self.store.append_event(
                    conn,
                    admission.session_id,
                    f"execution.{status}",
                    {
                        "admission_id": admission.id,
                        "recovered": "orphaned_started",
                        "reason": reason,
                    },
                )
                recovered.append(
                    {
                        "admission_id": admission.id,
                        "session_id": admission.session_id,
                        "status": status,
                    }
                )
        return recovered
