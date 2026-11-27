"""Receipts for model responses, effects and human decisions."""

from sqlalchemy import func, insert, select, update

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

    async def has_dispatched_effects(self, claim):
        async with self.store.engine.connect() as conn:
            return await conn.scalar(
                select(s.effects.c.id).where(
                    s.effects.c.admission_id == claim.admission.id,
                    s.effects.c.status == "dispatched",
                ).limit(1)
            ) is not None

    async def latest_non_read_tool_effect(self, claim, local_reads):
        payload = s.events.c.payload
        async with self.store.engine.connect() as conn:
            return (
                await conn.execute(
                    select(
                        s.effects.c.id,
                        s.effects.c.proposal_hash,
                        s.effects.c.status,
                        s.effects.c.result,
                    )
                    .join(s.events, payload["effect_id"].as_string() == s.effects.c.id)
                    .where(
                        s.effects.c.admission_id == claim.admission.id,
                        s.events.c.session_id == claim.admission.session_id,
                        s.events.c.type == "tool.started",
                        payload["tool"].as_string().not_in(sorted(local_reads)),
                    )
                    .order_by(s.events.c.seq.desc())
                    .limit(1)
                )
            ).mappings().first()

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

    async def dispatch_effect(self, claim: ExecutionClaim, key: str, *, approval=None, audit=None):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            if approval is not None:
                digest, scope_key, revision, reusable = approval
                row = (
                    await conn.execute(
                        select(s.interactions.c.proposal_hash, s.interactions.c.decision).where(
                            s.interactions.c.id == key
                        )
                    )
                ).first()
                if row is not None and row.proposal_hash != digest:
                    raise DomainError("interaction_conflict", "Approval proposal changed")
                if row is not None and row.decision == "approve":
                    source = "direct"
                elif (
                    row is None
                    and reusable
                    and await self._scope_granted(conn, claim, scope_key, revision, key)
                ):
                    source = "reuse"
                else:
                    raise DomainError("approval_missing", "This operation needs a durable approval")
                audit = (source, scope_key, audit[2])
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
            if audit is not None:
                source, scope_key, tool_name = audit
                kind = {"reuse": "approval.reused", "full": "approval.exempt"}.get(
                    source, "approval.dispatched"
                )
                await self.store.append_event(
                    conn,
                    claim.admission.session_id,
                    kind,
                    {
                        "admission_id": claim.admission.id,
                        "effect_id": key,
                        "scope_key": scope_key,
                        "tool_name": tool_name,
                        "source": source,
                    },
                )

    async def save_effect(self, claim: ExecutionClaim, key: str, result: str, *, verification=None):
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
            if verification is not None:
                await self.store.append_event(
                    conn,
                    claim.admission.session_id,
                    "coding.verification",
                    {"id": key, **verification},
                )

    async def latest_verification(self, claim: ExecutionClaim, command=None):
        filters = []
        if command is not None:
            filters = [
                s.events.c.payload["command"].as_string() == command,
                s.events.c.payload["cwd"].as_string() == ".",
            ]
        async with self.store.engine.connect() as conn:
            return await conn.scalar(
                select(s.events.c.payload)
                .where(
                    s.events.c.session_id == claim.admission.session_id,
                    s.events.c.type == "coding.verification",
                    *filters,
                    s.events.c.payload["id"].as_string().startswith(
                        claim.admission.id + ":", autoescape=True
                    ),
                )
                .order_by(s.events.c.seq.desc())
                .limit(1)
            )

    async def interaction(
        self,
        claim: ExecutionClaim,
        key: str,
        digest: str,
        proposal: dict,
        *,
        before_open=None,
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
            if before_open is not None:
                await before_open()
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
            return await self._scope_granted(conn, claim, scope_key, revision, exclude_key)

    async def _scope_granted(self, conn, claim, scope_key, revision, exclude_key):
        if await conn.scalar(
            select(s.approval_revocations.c.admission_id).where(
                s.approval_revocations.c.admission_id == claim.admission.id
            )
        ):
            return False
        query = select(s.interactions.c.id).where(
            s.interactions.c.admission_id == claim.admission.id,
            s.interactions.c.decision == "approve",
            s.interactions.c.proposal["scope_key"].as_string() == scope_key,
            s.interactions.c.proposal["reusable"].as_boolean().is_(True),
            s.interactions.c.proposal["permission_revision"].as_integer() == revision,
        )
        if exclude_key is not None:
            query = query.where(s.interactions.c.id != exclude_key)
        return await conn.scalar(query.limit(1)) is not None

    async def revoke_reuse(self, session_id: str, admission_id: str):
        async with self.store.transaction() as conn:
            await conn.execute(
                insert(s.approval_revocations)
                .prefix_with("OR IGNORE")
                .values(admission_id=admission_id)
            )
            await self.store.append_event(
                conn,
                session_id,
                "approval.reuse_revoked",
                {"admission_id": admission_id},
            )

    async def decided_waiting(self, session_id=None) -> list[dict]:
        current = func.json_each(s.approval_waits.c.interaction_ids).table_valued("value")
        decided_count = (
            select(func.count())
            .select_from(current.join(s.interactions, s.interactions.c.id == current.c.value))
            .where(
                s.interactions.c.decision.is_not(None),
                s.interactions.c.admission_id == s.approval_waits.c.admission_id,
            )
            .correlate(s.approval_waits)
            .scalar_subquery()
        )
        query = (
            select(
                s.admissions.c.id.label("admission_id"),
                s.admissions.c.session_id,
                s.approval_waits.c.generation,
            )
            .join(
                s.approval_waits,
                (s.approval_waits.c.admission_id == s.admissions.c.id)
                & (s.approval_waits.c.generation == s.admissions.c.generation),
            )
            .where(
                s.admissions.c.status == "waiting_approval",
                func.json_array_length(s.approval_waits.c.interaction_ids) > 0,
                func.json_array_length(s.approval_waits.c.interaction_ids) == decided_count,
            )
        )
        if session_id is not None:
            query = query.where(s.admissions.c.session_id == session_id)
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(query)
            return [dict(row) for row in rows.mappings()]

    async def verify_proposal(self, admission_id, interaction_id, digest):
        async with self.store.engine.connect() as conn:
            expected = await conn.scalar(
                select(s.interactions.c.proposal_hash).where(
                    s.interactions.c.admission_id == admission_id,
                    s.interactions.c.id == interaction_id,
                )
            )
        if expected != digest:
            raise DomainError("stale_approval", "Approval proposal does not match the selected run")

    async def dispatched_tool(self, admission_id, effect_id):
        async with self.store.engine.connect() as conn:
            proposal = await conn.scalar(
                select(s.interactions.c.proposal).join(
                    s.effects, s.interactions.c.id == s.effects.c.id
                ).where(
                    s.effects.c.id == effect_id,
                    s.effects.c.admission_id == admission_id,
                    s.effects.c.status == "dispatched",
                )
            )
        if proposal is None:
            raise DomainError("effect_not_found", "Effect is not available for status lookup")
        return proposal["tool_name"]

    async def used_sandbox(self, admission_id):
        async with self.store.engine.connect() as conn:
            tool = await conn.scalar(
                select(s.effects.c.id).join(
                    s.interactions, s.interactions.c.id == s.effects.c.id
                ).where(
                    s.effects.c.admission_id == admission_id,
                    s.effects.c.status == "succeeded",
                    s.interactions.c.proposal["tool_name"].as_string() == "bash",
                ).limit(1)
            )
        return tool is not None

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
                            s.approval_decisions.c.generation.label("decided_generation"),
                        )
                        .join(s.admissions, s.admissions.c.id == s.interactions.c.admission_id)
                        .outerjoin(
                            s.approval_decisions,
                            s.approval_decisions.c.interaction_id == s.interactions.c.id,
                        )
                        .where(
                            s.interactions.c.id == key,
                            s.interactions.c.session_id == session_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is not None and row["decided_generation"] == generation:
                if row["decision"] != decision:
                    raise DomainError("approval_conflict", "Approval decision cannot be changed")
                return row["admission_id"]
            if row is None or row["generation"] != generation or row["decision"] is not None:
                raise DomainError(
                    "stale_approval",
                    "Approval is missing or belongs to another generation",
                )
            if row["status"] != "waiting_approval":
                raise DomainError("not_waiting", "Execution is not waiting")
            current = await conn.scalar(
                select(s.approval_waits.c.interaction_ids).where(
                    s.approval_waits.c.admission_id == row["admission_id"],
                    s.approval_waits.c.generation == generation,
                )
            )
            if current is None or key not in current:
                raise DomainError("stale_approval", "Approval is not a verified current pause")
            await conn.execute(
                update(s.interactions)
                .where(
                    s.interactions.c.id == key,
                )
                .values(decision=decision)
            )
            await conn.execute(
                insert(s.approval_decisions).values(interaction_id=key, generation=generation)
            )
            await self.store.append_event(
                conn,
                session_id,
                "interaction.resolved",
                {"id": key, "decision": decision},
            )
            return row["admission_id"]
