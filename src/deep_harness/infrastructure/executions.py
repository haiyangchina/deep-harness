"""Atomic claims, fencing and explicit uncertain-outcome recovery."""

from dataclasses import replace

from sqlalchemy import select, update

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

    async def settle(self, claim: ExecutionClaim, status: str, result: str | None = None):
        async with self.store.transaction() as conn:
            await self.assert_claim(conn, claim)
            await conn.execute(
                update(s.admissions)
                .where(
                    s.admissions.c.id == claim.admission.id,
                )
                .values(status=status, result=result)
            )
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                f"execution.{status}",
                {"admission_id": claim.admission.id, "result": result},
            )

    async def resume(self, admission_id: str, session_id: str, owner: str) -> ExecutionClaim:
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
            if row is None:
                raise DomainError("not_waiting", "Admission is not waiting for approval")
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
