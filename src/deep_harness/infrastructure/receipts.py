"""Receipts for model responses, effects and human decisions."""

from sqlalchemy import insert, select, update

from deep_harness.domain.models import DomainError, ExecutionClaim
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.executions import ExecutionLedger
from deep_harness.infrastructure.store import BusinessStore


class ReceiptLedger:
    def __init__(self, store: BusinessStore, executions: ExecutionLedger):
        self.store = store
        self.executions = executions

    async def model_result(self, key: str, request_hash: str) -> dict | None:
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.model_receipts).where(
                            s.model_receipts.c.id == key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise DomainError("snapshot_mismatch", "Model replay request changed")
        return row["response"]

    async def approved(self, key: str, digest: str) -> bool:
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.interactions).where(
                            s.interactions.c.id == key,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is not None and row["proposal_hash"] != digest:
            raise DomainError("interaction_conflict", "Approval proposal changed")
        return row is not None and row["decision"] == "approve"

    async def save_model(
        self,
        claim: ExecutionClaim,
        key: str,
        request_hash: str,
        response: dict,
        *,
        append_to_history: bool = True,
    ):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            await conn.execute(
                insert(s.model_receipts).values(
                    id=key,
                    admission_id=claim.admission.id,
                    request_hash=request_hash,
                    response=response,
                )
            )
            if append_to_history:
                await self.store.append_message(conn, claim, response)
                await self.store.append_event(
                    conn,
                    claim.admission.session_id,
                    "message.committed",
                    response,
                )

    async def prepare_effect(self, claim: ExecutionClaim, key: str, digest: str) -> dict | None:
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            row = (
                (
                    await conn.execute(
                        select(s.effects).where(
                            s.effects.c.id == key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["proposal_hash"] != digest:
                    raise DomainError("effect_conflict", "Tool arguments changed during replay")
                if row["status"] == "dispatched":
                    raise DomainError("unknown_effect", "Tool outcome must be reconciled")
                return dict(row)
            await conn.execute(
                insert(s.effects).values(
                    id=key,
                    admission_id=claim.admission.id,
                    proposal_hash=digest,
                    status="prepared",
                )
            )
            return None

    async def dispatch_effect(self, claim: ExecutionClaim, key: str):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            result = await conn.execute(
                update(s.effects)
                .where(
                    s.effects.c.id == key,
                    s.effects.c.status == "prepared",
                )
                .values(status="dispatched")
            )
            if result.rowcount != 1:
                raise DomainError("unknown_effect", "Effect cannot be dispatched again")

    async def save_effect(self, claim: ExecutionClaim, key: str, result: str):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            await conn.execute(
                update(s.effects)
                .where(
                    s.effects.c.id == key,
                    s.effects.c.status == "dispatched",
                )
                .values(status="succeeded", result=result)
            )

    async def interaction(
        self,
        claim: ExecutionClaim,
        key: str,
        digest: str,
        proposal: dict,
    ) -> str | None:
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            row = (
                (
                    await conn.execute(
                        select(s.interactions).where(
                            s.interactions.c.id == key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["proposal_hash"] != digest:
                    raise DomainError("interaction_conflict", "Approval proposal changed")
                return row["decision"]
            await conn.execute(
                insert(s.interactions).values(
                    id=key,
                    session_id=claim.admission.session_id,
                    admission_id=claim.admission.id,
                    proposal_hash=digest,
                    proposal=proposal,
                )
            )
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                "interaction.open",
                {
                    "id": key,
                    "admission_id": claim.admission.id,
                    "proposal_hash": digest,
                    "generation": claim.admission.generation,
                    "proposal": proposal,
                },
            )
            return None

    async def scope_granted(
        self,
        claim: ExecutionClaim,
        scope_key: str,
        revision: int,
        exclude_key: str | None = None,
    ) -> bool:
        """Reuse one approval for the same scope within the same admission.

        The per-call ``key = admission:call`` and its proposal hash stay intact;
        this only looks for an earlier approved interaction that the operator
        explicitly allowed to be reused under the same permission revision.
        """
        async with self.store.engine.connect() as conn:
            revoked = await conn.execute(
                select(s.events.c.payload).where(
                    s.events.c.session_id == claim.admission.session_id,
                    s.events.c.type == "approval.reuse_revoked",
                )
            )
            if any(
                row["payload"].get("admission_id") == claim.admission.id
                for row in revoked.mappings()
            ):
                return False
            query = select(s.interactions.c.id, s.interactions.c.proposal).where(
                s.interactions.c.admission_id == claim.admission.id,
                s.interactions.c.decision == "approve",
            )
            if exclude_key is not None:
                query = query.where(s.interactions.c.id != exclude_key)
            rows = await conn.execute(query)
        return any(
            row["proposal"].get("scope_key") == scope_key
            and row["proposal"].get("reusable") is True
            and row["proposal"].get("permission_revision") == revision
            for row in rows.mappings()
        )

    async def record_approval_event(self, claim, kind, scope_key, tool_name):
        async with self.store.transaction() as conn:
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                kind,
                {
                    "admission_id": claim.admission.id,
                    "scope_key": scope_key,
                    "tool_name": tool_name,
                },
            )

    async def revoke_reuse(self, session_id: str, admission_id: str):
        async with self.store.transaction() as conn:
            await self.store.append_event(
                conn,
                session_id,
                "approval.reuse_revoked",
                {"admission_id": admission_id},
            )

    async def decided_waiting(self) -> list[dict]:
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(
                select(s.interactions.c.admission_id, s.interactions.c.session_id)
                .join(s.admissions, s.admissions.c.id == s.interactions.c.admission_id)
                .where(
                    s.interactions.c.decision.is_not(None),
                    s.admissions.c.status == "waiting_approval",
                )
            )
            return [dict(row) for row in rows.mappings()]

    async def decide(
        self,
        key: str,
        session_id: str,
        generation: int,
        decision: str,
    ) -> str:
        async with self.store.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(
                            s.interactions.c.admission_id,
                            s.interactions.c.decision,
                            s.admissions.c.generation,
                            s.admissions.c.status,
                        )
                        .join(s.admissions, s.admissions.c.id == s.interactions.c.admission_id)
                        .where(
                            s.interactions.c.id == key,
                            s.interactions.c.session_id == session_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["generation"] != generation:
                raise DomainError(
                    "stale_approval",
                    "Approval is missing or belongs to another generation",
                )
            if row["decision"] is not None:
                raise DomainError("already_decided", "Approval has already been answered")
            if row["status"] != "waiting_approval":
                raise DomainError("not_waiting", "Execution is not waiting")
            await conn.execute(
                update(s.interactions)
                .where(
                    s.interactions.c.id == key,
                )
                .values(decision=decision)
            )
            await self.store.append_event(
                conn,
                session_id,
                "interaction.resolved",
                {"id": key, "decision": decision},
            )
            return row["admission_id"]
