"""Immutable runtime bindings; legacy sessions require an explicit operator adoption."""

from sqlalchemy import insert, select

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s


class PolicyLedger:
    def __init__(self, store):
        self.store = store

    async def get(self, session_id):
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.session_policies).where(
                            s.session_policies.c.session_id == session_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def assert_matches(self, session_id, snapshot):
        bound = await self.get(session_id)
        if bound is None:
            raise DomainError(
                "runtime_unbound",
                "Inspect and explicitly bind this legacy session before execution",
            )
        if bound["fingerprint"] != fingerprint(snapshot):
            raise DomainError(
                "runtime_mismatch",
                "Model, tool schema or runtime changed; restore configuration or fork the session",
            )

    async def adopt(self, session_id, snapshot, expected_fingerprint):
        digest = fingerprint(snapshot)
        if expected_fingerprint != digest:
            raise DomainError(
                "runtime_changed", "Current runtime differs from the reviewed fingerprint"
            )
        async with self.store.transaction() as conn:
            bound = (
                await conn.execute(
                    select(s.session_policies.c.fingerprint).where(
                        s.session_policies.c.session_id == session_id
                    )
                )
            ).scalar_one_or_none()
            if bound is not None:
                if bound != digest:
                    raise DomainError(
                        "immutable_runtime", "An existing runtime binding cannot be overwritten"
                    )
                return
            await conn.execute(
                insert(s.session_policies).values(
                    session_id=session_id,
                    snapshot=snapshot,
                    fingerprint=digest,
                )
            )
            await self.store.append_event(
                conn, session_id, "runtime.bound", {"fingerprint": digest}
            )
