"""Profile-local transactional business store."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import and_, func, insert, or_, select
from sqlalchemy.ext.asyncio import create_async_engine

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import Admission, DomainError, ExecutionClaim, Session
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.database import initialize_database
from deep_harness.server_config import number

LIVE_PAGE_SIZE = number("pagination.live_page_size")


def admission_from(row) -> Admission:
    return Admission(
        row["id"],
        row["session_id"],
        row["text"],
        row["status"],
        row["generation"],
        row["result"],
    )


class BusinessStore:
    def __init__(self, home: Path, profile_id: str):
        self.home = home
        self.profile_id = profile_id
        self.revision = 0
        self.changed = asyncio.Condition()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{(home / 'business.db').as_posix()}",
            connect_args={"timeout": number("storage.sqlite_busy_timeout_seconds")},
        )

    async def open(self) -> None:
        await asyncio.to_thread(self.home.mkdir, parents=True, exist_ok=True)
        await initialize_database(self.engine)
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        await self.engine.dispose()

    async def bind_workspace(self, workspace: Path) -> None:
        """Bind before any recovery can dispatch effects into a different directory."""
        canonical = str(await asyncio.to_thread(workspace.resolve, strict=True))
        async with self.transaction() as conn:
            previous = (
                await conn.execute(
                    select(s.profile_bindings.c.workspace).where(
                        s.profile_bindings.c.profile_id == self.profile_id,
                    )
                )
            ).scalar_one_or_none()
            if previous is None:
                await conn.execute(
                    insert(s.profile_bindings).values(
                        profile_id=self.profile_id,
                        workspace=canonical,
                    )
                )
            elif Path(previous) != Path(canonical):
                raise DomainError(
                    "workspace_mismatch",
                    f"Profile workspace is {previous}; use that workspace or a new profile",
                )

    async def list_sessions(
        self, principal: str, *, after: str = "", limit: int = number("pagination.page_size")
    ):
        if not 1 <= limit <= number("pagination.max_page_size"):
            raise DomainError("invalid_limit", "Session page size must be between 1 and 100")
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                select(s.sessions)
                .where(
                    s.sessions.c.profile_id == self.profile_id,
                    s.sessions.c.principal == principal,
                    s.sessions.c.id > after,
                )
                .order_by(s.sessions.c.id)
                .limit(limit + 1)
            )
            page = list(rows.mappings())
        return {
            "sessions": [self._session(row) for row in page[:limit]],
            "next_cursor": page[limit - 1]["id"] if len(page) > limit else None,
        }

    async def session_status(self, session_id: str, principal: str):
        session = await self.get_session(session_id, principal)
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                select(s.admissions)
                .where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status.in_(
                        (
                            "queued",
                            "started",
                            "waiting_approval",
                            "job_paused",
                            "unknown",
                            "interrupted",
                            "reconciled",
                        )
                    ),
                )
                .order_by(s.admissions.c.ordinal)
            )
            active = [admission_from(row) for row in rows.mappings()]
            pending = await conn.execute(
                select(
                    s.interactions.c.id,
                    s.interactions.c.admission_id,
                    s.interactions.c.proposal_hash,
                    s.interactions.c.proposal,
                    s.admissions.c.generation,
                )
                .join(s.admissions, s.admissions.c.id == s.interactions.c.admission_id)
                .where(
                    s.interactions.c.session_id == session_id,
                    s.interactions.c.decision.is_(None),
                    s.admissions.c.status == "waiting_approval",
                )
                .order_by(s.admissions.c.ordinal, s.interactions.c.id)
            )
            approvals = [dict(row) for row in pending.mappings()]
            latest = (
                (
                    await conn.execute(
                        select(s.admissions)
                        .where(s.admissions.c.session_id == session_id)
                        .order_by(s.admissions.c.ordinal.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            effect_rows = await conn.execute(
                select(s.effects)
                .join(s.admissions, s.admissions.c.id == s.effects.c.admission_id)
                .where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status.in_(
                        ("started", "waiting_approval", "unknown", "interrupted", "reconciled")
                    ),
                )
                .order_by(s.effects.c.id)
            )
            active_effects = [dict(row) for row in effect_rows.mappings()]
            lineage = (
                (
                    await conn.execute(
                        select(s.session_branches).where(
                            s.session_branches.c.session_id == session_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            image_context = await conn.scalar(
                select(s.events.c.payload)
                .where(
                    s.events.c.session_id == session_id,
                    s.events.c.type == "model.started",
                    s.events.c.payload["image_ids"].as_string().is_not(None),
                )
                .order_by(s.events.c.seq.desc())
                .limit(1)
            )
        return {
            "session": session,
            "image_context": image_context["image_ids"] if image_context else None,
            "active_admissions": active,
            "approvals": approvals,
            "latest_admission": admission_from(latest) if latest is not None else None,
            "active_effects": active_effects,
            "lineage": dict(lineage) if lineage else None,
        }

    @asynccontextmanager
    async def transaction(self):
        async with self.engine.connect() as conn:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        async with self.changed:
            self.revision += 1
            self.changed.notify_all()

    async def wait_for_change(self, revision: int) -> None:
        async with self.changed:
            try:
                async with asyncio.timeout(number("transport.event_keepalive_seconds")):
                    await self.changed.wait_for(lambda: self.revision != revision)
            except TimeoutError:
                return

    async def create_session(
        self,
        *,
        principal: str,
        request_id: str,
        prompt: str,
        tools: tuple[str, ...],
        engine: str = "standard",
        harness: str = "general",
        permission_mode: str = "ask",
        runtime_snapshot: dict | None = None,
    ) -> Session:
        tools = tuple(sorted(set(tools)))
        creation = {"prompt": prompt, "tools": tools, "engine": engine}
        if harness != "general":
            creation["harness"] = harness
        creation["permission_mode"] = permission_mode
        digest = fingerprint(creation)
        async with self.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.sessions).where(
                            s.sessions.c.profile_id == self.profile_id,
                            s.sessions.c.principal == principal,
                            s.sessions.c.request_id == request_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["payload_hash"] != digest:
                    raise DomainError("idempotency_conflict", "Creation request parameters changed")
                if runtime_snapshot is not None:
                    binding = (
                        await conn.execute(
                            select(s.session_policies.c.fingerprint).where(
                                s.session_policies.c.session_id == row["id"],
                            )
                        )
                    ).scalar_one_or_none()
                    if binding is None:
                        raise DomainError(
                            "runtime_unbound",
                            "Session has no runtime binding",
                        )
                    equivalent = {fingerprint(runtime_snapshot)}
                    if runtime_snapshot.get("coding_task") == {
                        "mode": "in_place", "verification_command": None,
                    }:
                        old_snapshot = dict(runtime_snapshot)
                        del old_snapshot["coding_task"]
                        equivalent.add(fingerprint(old_snapshot))
                    if binding not in equivalent:
                        raise DomainError(
                            "idempotency_conflict", "Creation runtime parameters changed"
                        )
                return self._session(row)
            values = dict(
                id=str(uuid4()),
                profile_id=self.profile_id,
                principal=principal,
                request_id=request_id,
                payload_hash=digest,
                prompt=prompt,
                tools=list(tools),
                engine=engine,
                harness=harness,
                permission_mode=permission_mode,
                permission_revision=0,
            )
            await conn.execute(insert(s.sessions).values(**values))
            if runtime_snapshot is not None:
                await conn.execute(
                    insert(s.session_policies).values(
                        session_id=values["id"],
                        snapshot=runtime_snapshot,
                        fingerprint=fingerprint(runtime_snapshot),
                    )
                )
            return self._session(values)

    def _session(self, row) -> Session:
        return Session(
            row["id"],
            row["profile_id"],
            row["principal"],
            row["prompt"],
            tuple(row["tools"]),
            row["engine"],
            row["harness"],
            row["permission_mode"],
            row.get("permission_revision", 0),
        )

    async def get_session(self, session_id: str, principal: str) -> Session:
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.sessions).where(
                            s.sessions.c.id == session_id,
                            s.sessions.c.profile_id == self.profile_id,
                            s.sessions.c.principal == principal,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DomainError("session_not_found", "Session is not accessible")
        return self._session(row)

    async def admit(
        self,
        session: Session,
        *,
        principal: str,
        submission_id: str,
        text: str,
        artifact_ids: tuple[str, ...] = (),
        image_ids: tuple[str, ...] = (),
    ) -> Admission:
        if session.profile_id != self.profile_id or session.principal != principal:
            raise DomainError("scope_mismatch", "Input does not belong to this profile/principal")
        payload = {"text": text}
        if artifact_ids:
            payload["artifact_ids"] = artifact_ids
        if image_ids:
            payload["image_ids"] = image_ids
        digest = fingerprint(payload)
        async with self.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.admissions).where(
                            s.admissions.c.session_id == session.id,
                            s.admissions.c.principal == principal,
                            s.admissions.c.submission_id == submission_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is not None:
                if row["payload_hash"] != digest:
                    raise DomainError("idempotency_conflict", "Submission payload changed")
                return admission_from(row)
            admission_id = str(uuid4())
            await conn.execute(
                insert(s.admissions).values(
                    id=admission_id,
                    session_id=session.id,
                    principal=principal,
                    submission_id=submission_id,
                    payload_hash=digest,
                    text=text,
                    status="queued",
                    generation=0,
                )
            )
            await conn.execute(
                insert(s.messages).values(
                    id=f"{admission_id}:user",
                    session_id=session.id,
                    admission_id=admission_id,
                    payload={
                        "id": f"{admission_id}:user",
                        "role": "user",
                        "content": text,
                        **({"image_ids": list(image_ids)} if image_ids else {}),
                    },
                )
            )
            if artifact_ids or image_ids:
                from deep_harness.infrastructure.artifacts import run_artifacts

                await conn.execute(
                    insert(run_artifacts),
                    [{"run_id": admission_id, "artifact_id": artifact_id}
                     for artifact_id in (*artifact_ids, *image_ids)],
                )
            await self.append_event(conn, session.id, "admission.accepted", {"id": admission_id})
        return Admission(admission_id, session.id, text, "queued", 0)

    async def admission_image_ids(self, admission):
        async with self.engine.connect() as conn:
            payload = await conn.scalar(
                select(s.messages.c.payload).where(
                    s.messages.c.id == f"{admission.id}:user",
                    s.messages.c.session_id == admission.session_id,
                )
            )
        return payload.get("image_ids", []) if payload else []

    async def queued_count(self):
        async with self.engine.connect() as conn:
            return await conn.scalar(
                select(func.count())
                .select_from(s.admissions)
                .where(s.admissions.c.status == "queued")
            )

    async def run_session(self, run_id):
        async with self.engine.connect() as conn:
            session_id = await conn.scalar(
                select(s.admissions.c.session_id).where(s.admissions.c.id == run_id)
            )
        if session_id is None:
            raise DomainError("admission_not_found", "Run is not accessible")
        return session_id

    async def find_submission(self, session_id, submission_id, principal="operator"):
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.admissions).where(
                            s.admissions.c.session_id == session_id,
                            s.admissions.c.submission_id == submission_id,
                            s.admissions.c.principal == principal,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return admission_from(row) if row is not None else None

    async def get_admission(self, admission_id: str, session_id: str) -> Admission:
        async with self.engine.connect() as conn:
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
        return admission_from(row)

    async def committed_message(self, message_id: str) -> dict | None:
        """Return an already committed message payload, if one exists.

        LangGraph replays a node from the start after an interrupt is resumed; the
        tools node uses this to reuse committed results instead of re-executing a
        tool call (which would also risk replaying a side effect).
        """
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    select(s.messages.c.payload).where(s.messages.c.id == message_id)
                )
            ).scalar_one_or_none()

    async def history(self, session_id: str, *, admission_id=None) -> list[dict]:
        query = (
            select(s.messages.c.payload)
            .join(s.admissions, s.admissions.c.id == s.messages.c.admission_id)
            .where(s.messages.c.session_id == session_id)
            .order_by(s.admissions.c.ordinal, s.messages.c.ordinal)
        )
        if admission_id is not None:
            query = query.where(s.messages.c.admission_id == admission_id)
        async with self.engine.connect() as conn:
            return list((await conn.execute(query)).scalars())

    async def live_history(self, session_id, *, after=None, before=None, limit=LIVE_PAGE_SIZE):
        """Tail or append-only cursor over all committed messages, including active turns."""
        if after is not None and before is not None:
            raise DomainError("invalid_cursor", "Provide one message cursor")
        query = (
            select(
                s.messages.c.ordinal.label("seq"),
                s.admissions.c.ordinal.label("turn"),
                s.messages.c.payload.label("message"),
            )
            .join(s.admissions, s.admissions.c.id == s.messages.c.admission_id)
            .where(s.messages.c.session_id == session_id)
        )
        if after is not None:
            query = query.where(s.messages.c.ordinal > after).order_by(s.messages.c.ordinal)
        else:
            if before is not None:
                query = query.where(s.messages.c.ordinal < before)
            query = query.order_by(s.messages.c.ordinal.desc())
        async with self.engine.connect() as conn:
            result = await conn.execute(query.limit(limit + 1))
            rows = [dict(row) for row in result.mappings()]
        visible = rows[:limit]
        if after is None:
            visible.reverse()
        return {"items": visible, "has_more": len(rows) > limit}

    async def history_page(
        self, session_id: str, *, after: str = "", limit: int = number("pagination.page_size")
    ):
        """Page committed completed turns in stable conversational order."""
        if not 1 <= limit <= number("pagination.max_page_size"):
            raise DomainError("invalid_limit", "History page size must be between 1 and 100")
        first = second = 0
        if after:
            parts = after.split(":")
            if len(parts) != 2 or any(
                len(part) > 19 or not part.isascii() or not part.isdecimal() for part in parts
            ):
                raise DomainError("invalid_cursor", "History cursor is invalid")
            first, second = map(int, parts)
            if first > 2**63 - 1 or second > 2**63 - 1:
                raise DomainError("invalid_cursor", "History cursor exceeds the sequence range")
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                select(
                    s.admissions.c.ordinal.label("turn_ordinal"),
                    s.messages.c.ordinal.label("message_ordinal"),
                    s.messages.c.payload,
                )
                .join(s.admissions, s.admissions.c.id == s.messages.c.admission_id)
                .where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status == "completed",
                    or_(
                        s.admissions.c.ordinal > first,
                        and_(
                            s.admissions.c.ordinal == first,
                            s.messages.c.ordinal > second,
                        ),
                    ),
                )
                .order_by(s.admissions.c.ordinal, s.messages.c.ordinal)
                .limit(limit + 1)
            )
            page = list(rows.mappings())
        visible = page[:limit]
        cursor = None
        if len(page) > limit:
            last = visible[-1]
            cursor = f"{last['turn_ordinal']}:{last['message_ordinal']}"
        return {"messages": [row["payload"] for row in visible], "next_cursor": cursor}

    async def search_history(
        self, session_id: str, query: str, *, limit: int = number("pagination.search_results")
    ):
        if (
            not query.strip()
            or len(query) > number("pagination.search_query_chars")
            or not 1 <= limit <= number("pagination.max_search_results")
        ):
            raise DomainError("invalid_search", "Search needs 1-200 characters and 1-50 results")
        phrase = '"' + query.strip().replace('"', '""') + '"'
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self.engine.connect() as conn:
            rows = await conn.exec_driver_sql(
                "WITH ranked_fts AS ("
                "SELECT m.payload, a.ordinal AS turn_ordinal, m.ordinal AS message_ordinal "
                "FROM messages_fts "
                "JOIN messages AS m ON m.ordinal = messages_fts.rowid "
                "JOIN admissions AS a ON a.id = m.admission_id "
                "WHERE messages_fts MATCH ? AND messages_fts.session_id = ? "
                "AND a.status = 'completed' "
                "ORDER BY a.ordinal, m.ordinal LIMIT ?"
                "), ranked_literal AS ("
                "SELECT m.payload, a.ordinal AS turn_ordinal, m.ordinal AS message_ordinal "
                "FROM admissions AS a INDEXED BY ix_harness_admissions_session_id_status_ordinal "
                "CROSS JOIN messages AS m INDEXED BY ix_harness_messages_admission_id_ordinal "
                "ON m.admission_id = a.id WHERE a.session_id = ? AND m.session_id = ? "
                "AND a.status = 'completed' "
                "AND CAST(json_extract(m.payload, '$.content') AS TEXT) LIKE ? ESCAPE '\\' "
                "ORDER BY a.ordinal, m.ordinal LIMIT ?"
                ") SELECT * FROM ranked_fts UNION SELECT * FROM ranked_literal "
                "ORDER BY turn_ordinal, message_ordinal LIMIT ?",
                (phrase, session_id, limit, session_id, session_id, f"%{escaped}%", limit, limit),
            )
            return [json.loads(row[0]) for row in rows]

    async def event_history(
        self, session_id: str, after: int = 0, *, tail=False, run_id=None
    ) -> list[dict]:
        order = s.events.c.seq.desc() if tail else s.events.c.seq
        query = select(s.events).where(s.events.c.session_id == session_id, s.events.c.seq > after)
        if run_id is not None:
            payload = s.events.c.payload
            event_id = payload["id"].as_string()
            query = query.where(or_(
                payload["admission_id"].as_string() == run_id,
                payload["run_id"].as_string() == run_id,
                event_id == run_id,
                event_id.startswith(run_id + ":", autoescape=True),
            ))
        async with self.engine.connect() as conn:
            rows = await conn.execute(
                query
                .order_by(order)
                .limit(
                    number("pagination.event_tail_size")
                    if tail
                    else number("pagination.event_batch_size")
                )
            )
            result = [dict(row) for row in rows.mappings()]
        if tail:
            result.reverse()
        return result

    async def append_event(self, conn, session_id: str, kind: str, payload: dict) -> None:
        await conn.execute(
            insert(s.events).values(
                session_id=session_id,
                type=kind,
                payload=payload,
            )
        )

    async def append_message(self, conn, claim: ExecutionClaim, payload: dict) -> None:
        existing = (
            await conn.execute(
                select(s.messages.c.payload).where(
                    s.messages.c.id == payload["id"],
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if fingerprint(existing) != fingerprint(payload):
                raise DomainError("message_conflict", "A committed message cannot be changed")
            return
        await conn.execute(
            insert(s.messages).values(
                id=payload["id"],
                session_id=claim.admission.session_id,
                admission_id=claim.admission.id,
                payload=payload,
            )
        )
