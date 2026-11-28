"""Fork only the completed prefix; never clone checkpoints, approvals or uncertain effects."""

from uuid import uuid4

from sqlalchemy import insert, select

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.artifacts import run_artifacts
from deep_harness.infrastructure.session_management import assert_request_available


class BranchLedger:
    def __init__(self, store):
        self.store = store

    async def fork(self, source, request_id, snapshot):
        digest = fingerprint({"kind": "fork", "source_id": source.id, "runtime": snapshot})
        async with self.store.transaction() as conn:
            await assert_request_available(conn, source.principal, request_id)
            previous = (
                (
                    await conn.execute(
                        select(s.sessions).where(
                            s.sessions.c.profile_id == source.profile_id,
                            s.sessions.c.principal == source.principal,
                            s.sessions.c.request_id == request_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if previous is not None:
                if previous["payload_hash"] != digest:
                    raise DomainError("idempotency_conflict", "Fork request parameters changed")
                return self.store._session(previous)
            values = dict(
                id=str(uuid4()),
                profile_id=source.profile_id,
                principal=source.principal,
                request_id=request_id,
                payload_hash=digest,
                prompt=source.prompt,
                tools=list(source.tools),
                engine=source.engine,
                harness=source.harness,
                permission_mode=source.permission_mode,
                permission_revision=0,
            )
            await conn.execute(insert(s.sessions).values(**values))
            await conn.execute(
                insert(s.session_policies).values(
                    session_id=values["id"],
                    snapshot=snapshot,
                    fingerprint=fingerprint(snapshot),
                )
            )
            rows = (
                (
                    await conn.execute(
                        select(s.admissions)
                        .where(
                            s.admissions.c.session_id == source.id,
                        )
                        .order_by(s.admissions.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            copied = []
            for row in rows:
                if row["status"] != "completed":
                    break
                copied.append(await self._copy_turn(conn, values["id"], row))
            await conn.execute(
                insert(s.session_branches).values(
                    session_id=values["id"],
                    parent_id=source.id,
                    copied_admissions=copied,
                )
            )
            await self.store.append_event(
                conn,
                values["id"],
                "session.forked",
                {
                    "parent_id": source.id,
                    "copied_turns": len(copied),
                },
            )
            return self.store._session(values)

    async def _copy_turn(self, conn, target_id, original):
        admission_id = str(uuid4())
        await conn.execute(
            insert(s.admissions).values(
                id=admission_id,
                session_id=target_id,
                principal=original["principal"],
                submission_id=f"inherited:{original['id']}",
                payload_hash=original["payload_hash"],
                text=original["text"],
                status="completed",
                generation=0,
                result=original["result"],
            )
        )
        messages = (
            (
                await conn.execute(
                    select(s.messages.c.payload)
                    .where(
                        s.messages.c.admission_id == original["id"],
                    )
                    .order_by(s.messages.c.ordinal)
                )
            )
            .scalars()
            .all()
        )
        for message in messages:
            if image_ids := message.get("image_ids"):
                await conn.execute(
                    insert(run_artifacts),
                    [{"run_id": admission_id, "artifact_id": image_id} for image_id in image_ids],
                )
            payload = {**message, "id": f"{target_id}:inherited:{message['id']}"}
            await conn.execute(
                insert(s.messages).values(
                    id=payload["id"],
                    session_id=target_id,
                    admission_id=admission_id,
                    payload=payload,
                )
            )
        return admission_id

    async def seed(self, session_id):
        async with self.store.engine.connect() as conn:
            inherited = (
                await conn.execute(
                    select(s.session_branches.c.copied_admissions).where(
                        s.session_branches.c.session_id == session_id,
                    )
                )
            ).scalar_one_or_none()
            if not inherited:
                return []
            messages = await conn.execute(
                select(s.messages.c.payload)
                .join(
                    s.admissions,
                    s.admissions.c.id == s.messages.c.admission_id,
                )
                .where(
                    s.messages.c.session_id == session_id,
                    s.admissions.c.id.in_(inherited),
                )
                .order_by(s.admissions.c.ordinal, s.messages.c.ordinal)
            )
            return list(messages.scalars())
