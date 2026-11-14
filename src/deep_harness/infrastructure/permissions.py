"""Audited idle-only permission changes, separate from immutable model snapshots."""

from sqlalchemy import select, update

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s


async def change_permissions(store, session_id, mode, expected_revision):
    if mode not in {"ask", "auto", "full"}:
        raise DomainError("permission_mode", "Unknown permission mode")
    if not isinstance(expected_revision, int) or expected_revision < 0:
        raise DomainError("permission_revision", "Expected revision must be a nonnegative integer")
    async with store.transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(s.sessions).where(
                        s.sessions.c.id == session_id,
                        s.sessions.c.profile_id == store.profile_id,
                        s.sessions.c.principal == "operator",
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise DomainError("session_not_found", "Session is not accessible")
        revision = row["permission_revision"]
        if row["permission_mode"] == mode and revision in {
            expected_revision,
            expected_revision + 1,
        }:
            return store._session(row)
        if revision != expected_revision:
            raise DomainError("stale_permissions", "Permissions changed; refresh before retrying")
        pending = (
            await conn.execute(
                select(s.admissions.c.id)
                .where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status.in_(
                        {
                            "queued",
                            "started",
                            "waiting_approval",
                            "unknown",
                            "interrupted",
                            "reconciled",
                        }
                    ),
                )
                .limit(1)
            )
        ).first()
        children = (
            await conn.execute(
                select(s.delegations.c.id)
                .where(
                    s.delegations.c.session_id == session_id,
                    s.delegations.c.status.not_in({"completed", "failed", "cancelled", "stopped"}),
                )
                .limit(1)
            )
        ).first()
        if pending or children:
            raise DomainError("permissions_busy", "Finish pending work before changing permissions")
        await conn.execute(
            update(s.sessions)
            .where(s.sessions.c.id == session_id)
            .values(
                permission_mode=mode,
                permission_revision=revision + 1,
            )
        )
        await store.append_event(
            conn,
            session_id,
            "permissions.changed",
            {
                "previous": row["permission_mode"],
                "mode": mode,
                "revision": revision + 1,
            },
        )
        return store._session({**row, "permission_mode": mode, "permission_revision": revision + 1})
