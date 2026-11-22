"""Immutable runtime bindings for sessions."""

from sqlalchemy import select

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
                "Session has no runtime binding",
            )
        if bound["fingerprint"] != fingerprint(snapshot):
            raise DomainError(
                "runtime_mismatch",
                "Model, tool schema or runtime changed; restore configuration or fork the session",
            )
