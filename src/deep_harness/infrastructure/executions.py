"""Atomic claims, fencing and explicit uncertain-outcome recovery."""

from dataclasses import replace

from sqlalchemy import delete, insert, select, update

from deep_harness.domain.models import DomainError, ExecutionClaim
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.store import BusinessStore, admission_from

BLOCKING = ("started", "waiting_approval", "unknown", "interrupted", "reconciled")


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
            if row["status"] not in {"queued", "waiting_approval"}:
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

    async def claim(self, session_id: str, owner: str) -> ExecutionClaim | None:
        async with self.store.transaction() as conn:
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
            row = (
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
            if row is None:
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

    async def resume(
        self, admission_id: str, session_id: str, owner: str, *, expected_generation=None
    ) -> ExecutionClaim | None:
        async with self.store.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.admissions).where(
                            s.admissions.c.id == admission_id,
                            s.admissions.c.session_id == session_id,
                            s.admissions.c.status == "waiting_approval",
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

    async def unbound_waits(self):
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(
                select(s.admissions)
                .outerjoin(s.approval_waits, s.approval_waits.c.admission_id == s.admissions.c.id)
                .where(
                    s.admissions.c.status == "waiting_approval",
                    s.approval_waits.c.admission_id.is_(None),
                )
            )
            return [admission_from(row) for row in rows.mappings()]

    async def bind_wait(self, admission, interaction_ids):
        ids = list(dict.fromkeys(interaction_ids))
        if not ids:
            return
        async with self.store.transaction() as conn:
            pending = await conn.scalar(
                select(s.admissions.c.id).where(
                    s.admissions.c.id == admission.id,
                    s.admissions.c.status == "waiting_approval",
                    s.admissions.c.generation == admission.generation,
                )
            )
            matches = (
                (
                    await conn.execute(
                        select(s.interactions.c.id).where(
                            s.interactions.c.admission_id == admission.id,
                            s.interactions.c.id.in_(ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if pending and set(matches) == set(ids):
                await conn.execute(
                    insert(s.approval_waits)
                    .prefix_with("OR IGNORE")
                    .values(
                        admission_id=admission.id,
                        generation=admission.generation,
                        interaction_ids=ids,
                    )
                )

    async def recover(self) -> list[str]:
        """A crashed started execution is uncertain; never silently requeue it."""
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
                await conn.execute(
                    update(s.admissions)
                    .where(
                        s.admissions.c.id == row["id"],
                    )
                    .values(status="unknown", owner=None)
                )
                await self.store.append_event(
                    conn,
                    row["session_id"],
                    "execution.unknown",
                    {"admission_id": row["id"]},
                )
            queued = await conn.execute(
                select(s.admissions.c.session_id)
                .where(
                    s.admissions.c.status == "queued",
                )
                .distinct()
            )
            return list(queued.scalars())
