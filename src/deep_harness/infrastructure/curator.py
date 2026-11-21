"""Durable memory candidates with an atomic human decision."""

from datetime import UTC, datetime

from sqlalchemy import insert, select, update

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.server_config import number

RECENT_USER_TURNS = number("curation.max_recent_user_turns")


class CuratorLedger:
    def __init__(self, store):
        self.store = store

    async def completed_user_texts(self, session_id, limit=RECENT_USER_TURNS):
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(
                select(s.admissions.c.text)
                .where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status == "completed",
                )
                .order_by(s.admissions.c.ordinal.desc())
                .limit(limit)
            )
            return list(reversed(rows.scalars().all()))

    async def get_run(self, run_id):
        async with self.store.engine.connect() as conn:
            run = (
                (
                    await conn.execute(
                        select(s.curator_runs).where(
                            s.curator_runs.c.id == run_id,
                            s.curator_runs.c.profile_id == self.store.profile_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if run is None:
                return None
            rows = await conn.execute(
                select(s.memory_candidates)
                .where(s.memory_candidates.c.run_id == run_id)
                .order_by(s.memory_candidates.c.ordinal)
            )
            return {"run": dict(run), "candidates": [dict(row) for row in rows.mappings()]}

    async def save(self, run_id, session_id, history_hash, model_hash, contents, usage):
        existing = await self.get_run(run_id)
        if existing is not None:
            return existing
        now = datetime.now(UTC).isoformat()
        async with self.store.transaction() as conn:
            await conn.execute(
                insert(s.curator_runs).values(
                    id=run_id,
                    profile_id=self.store.profile_id,
                    session_id=session_id,
                    history_hash=history_hash,
                    model_hash=model_hash,
                    usage=usage,
                    created_at=now,
                )
            )
            for number, content in enumerate(contents):
                await conn.execute(
                    insert(s.memory_candidates).values(
                        id=fingerprint({"run": run_id, "index": number}),
                        run_id=run_id,
                        profile_id=self.store.profile_id,
                        session_id=session_id,
                        ordinal=number,
                        content=content,
                        status="pending",
                        created_at=now,
                    )
                )
            await self.store.append_event(
                conn, session_id, "curator.proposed", {"run_id": run_id, "count": len(contents)}
            )
        return await self.get_run(run_id)

    async def list_candidates(self, session_id=None):
        async with self.store.engine.connect() as conn:
            query = select(s.memory_candidates).where(
                s.memory_candidates.c.profile_id == self.store.profile_id,
                s.memory_candidates.c.status == "pending",
            )
            if session_id:
                query = query.where(s.memory_candidates.c.session_id == session_id)
            rows = await conn.execute(query.order_by(s.memory_candidates.c.created_at.desc()))
            return [dict(row) for row in rows.mappings()]

    async def decide(self, candidate_id, decision):
        if decision not in {"accept", "reject"}:
            raise DomainError("invalid_decision", "Candidate decision must be accept or reject")
        async with self.store.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.memory_candidates).where(
                            s.memory_candidates.c.id == candidate_id,
                            s.memory_candidates.c.profile_id == self.store.profile_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise DomainError("candidate_not_found", "Memory candidate is not accessible")
            if row["status"] != "pending":
                raise DomainError("already_decided", "Memory candidate was already reviewed")
            if decision == "accept":
                content = row["content"]
                source = f"curator:{row['session_id']}"
                await conn.execute(
                    insert(s.memories).values(
                        id=candidate_id,
                        profile_id=self.store.profile_id,
                        content=content,
                        source=source,
                        created_at=datetime.now(UTC).isoformat(),
                        active=1,
                        content_hash=fingerprint({"content": content, "source": source}),
                    )
                )
            await conn.execute(
                update(s.memory_candidates)
                .where(s.memory_candidates.c.id == candidate_id)
                .values(status="accepted" if decision == "accept" else "rejected")
            )
            await self.store.append_event(
                conn,
                row["session_id"],
                "curator.decided",
                {"candidate_id": candidate_id, "decision": decision},
            )
        return {"candidate_id": candidate_id, "decision": decision}
