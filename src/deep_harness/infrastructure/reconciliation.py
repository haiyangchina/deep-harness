"""Operator-evidenced reconciliation never grants permission to replay an effect."""

from sqlalchemy import insert, select, update

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.server_config import number


async def reconcile(store, session_id, admission_id, generation, evidence, effects):
    if not evidence.strip() or len(evidence) > number("reconciliation.evidence_max_length"):
        raise DomainError(
            "invalid_evidence", "Provide evidence within the configured character limit"
        )
    digest = fingerprint({"generation": generation, "evidence": evidence, "effects": effects})
    async with store.transaction() as conn:
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
        previous = (
            await conn.execute(
                select(s.resolutions.c.request_hash).where(
                    s.resolutions.c.admission_id == admission_id,
                )
            )
        ).scalar_one_or_none()
        if previous is not None:
            if previous != digest:
                raise DomainError(
                    "resolution_conflict", "Reconciliation evidence cannot be rewritten"
                )
            return
        if row["generation"] != generation or row["status"] not in {"unknown", "interrupted"}:
            raise DomainError("stale_resolution", "Admission is not uncertain at this generation")
        pending = (
            (
                await conn.execute(
                    select(s.effects).where(
                        s.effects.c.admission_id == admission_id,
                        s.effects.c.status == "dispatched",
                    )
                )
            )
            .mappings()
            .all()
        )
        reviewed = {effect["effect_id"]: effect for effect in effects}
        if len(reviewed) != len(effects) or set(reviewed) != {effect["id"] for effect in pending}:
            raise DomainError(
                "incomplete_resolution", "Review every dispatched effect exactly once"
            )
        for effect in pending:
            decision = reviewed[effect["id"]]
            if decision["proposal_hash"] != effect["proposal_hash"] or decision["outcome"] not in {
                "applied",
                "not_applied",
            }:
                raise DomainError("stale_resolution", "Effect fingerprint or outcome is invalid")
            await conn.execute(
                update(s.effects)
                .where(s.effects.c.id == effect["id"])
                .values(
                    status=f"confirmed_{decision['outcome']}",
                    result=evidence,
                )
            )
        await conn.execute(
            insert(s.resolutions).values(
                admission_id=admission_id,
                request_hash=digest,
                evidence=evidence,
                effects=effects,
            )
        )
        await conn.execute(
            update(s.admissions)
            .where(s.admissions.c.id == admission_id)
            .values(
                status="reconciled",
                owner=None,
                generation=generation + 1,
                result=evidence,
            )
        )
        await store.append_event(
            conn,
            session_id,
            "execution.reconciled",
            {
                "admission_id": admission_id,
                "evidence": evidence,
                "effects": effects,
            },
        )
