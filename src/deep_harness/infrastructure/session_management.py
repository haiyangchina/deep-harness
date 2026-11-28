"""Profile-local presentation metadata and durable deletion journal."""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from time import time_ns

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    update,
)

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s

metadata = MetaData()
management = Table(
    "session_management", metadata,
    Column("session_id", String, primary_key=True),
    Column("title", String),
    Column("pin", Integer, nullable=False, default=0),
    Column("revision", Integer, nullable=False, default=0),
    Column("order_revision", Integer, nullable=False, default=0),
    Column("deletion", String),
    Column("request_hash", String, unique=True),
    Column("cleanup", JSON),
)


def deleting_sessions():
    return select(management.c.session_id).where(management.c.deletion.is_not(None))


async def assert_request_available(conn, principal, request_id):
    key = fingerprint([principal, request_id])
    retired = await conn.scalar(
        select(management.c.session_id).where(management.c.request_hash == key)
    )
    if retired is not None:
        raise DomainError("session_deleted", "This request belongs to a deleted session")


class SessionManagement:
    def __init__(self, store):
        self.store = store

    async def row(self, conn, session_id):
        return (await conn.execute(
            select(management).where(management.c.session_id == session_id)
        )).mappings().first()

    def item(self, session, info):
        pin = info.get("pin") or 0
        return {
            **session,
            "title": info.get("title"),
            "pinned": bool(pin),
            "pinned_at": datetime.fromtimestamp(pin / 1e9, UTC).isoformat() if pin else None,
            "metadata_revision": info.get("revision") or 0,
        }

    async def presentation(self, session_id):
        async with self.store.engine.connect() as conn:
            return self.item({}, await self.row(conn, session_id) or {})

    async def page(self, principal, after, limit):
        pin = func.coalesce(management.c.pin, 0)
        query = (select(*s.sessions.c, management.c.title, management.c.pin, management.c.revision)
                 .outerjoin(management, s.sessions.c.id == management.c.session_id)
                 .where(s.sessions.c.profile_id == self.store.profile_id,
                        s.sessions.c.principal == principal, management.c.deletion.is_(None)))
        async with self.store.engine.connect() as conn:
            await conn.exec_driver_sql("BEGIN")
            revision = await conn.scalar(select(func.coalesce(func.sum(
                management.c.order_revision
            ), 0)))
            if after:
                try:
                    epoch, previous_pin, previous_id = json.loads(after)
                    if type(epoch) is not int or type(previous_pin) is not int or not isinstance(
                        previous_id, str
                    ):
                        raise ValueError
                except (ValueError, TypeError) as exc:
                    raise DomainError("invalid_cursor", "Invalid session list cursor") from exc
                if epoch != revision:
                    raise DomainError("session_list_changed", "Reload the session list")
                query = query.where((pin < previous_pin) | (
                    (pin == previous_pin) & (s.sessions.c.id > previous_id)
                ))
            rows = (await conn.execute(query.order_by(pin.desc(), s.sessions.c.id)
                                       .limit(limit + 1))).mappings().all()
        items = [self.item(asdict(self.store._session(row)), row) for row in rows[:limit]]
        cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            cursor = json.dumps([revision, last["pin"] or 0, last["id"]], separators=(",", ":"))
        return {"sessions": items, "next_cursor": cursor}

    async def change(self, session_id, expected, changes):
        async with self.store.transaction() as conn:
            session = await self.accessible(conn, session_id)
            row = await self.row(conn, session_id)
            values = dict(row or {"session_id": session_id, "revision": 0,
                                  "order_revision": 0, "pin": 0, "title": None})
            if values["revision"] != expected:
                raise DomainError("session_metadata_conflict", "Reload the session and try again")
            title = changes.get("title", values["title"])
            pinned = changes.get("pinned", bool(values["pin"]))
            pin_changed = pinned != bool(values["pin"])
            if title != values["title"] or pin_changed:
                values["title"] = title
                values["revision"] += 1
                if pin_changed:
                    values["pin"] = time_ns() if pinned else 0
                    values["order_revision"] += 1
                await self.save(conn, session_id, row, values)
            return self.item(asdict(self.store._session(session)), values)

    async def accessible(self, conn, session_id):
        row = (await conn.execute(select(s.sessions).where(
            s.sessions.c.id == session_id, s.sessions.c.profile_id == self.store.profile_id,
            s.sessions.c.principal == "operator", s.sessions.c.id.not_in(deleting_sessions()),
        ))).mappings().first()
        if row is None:
            raise DomainError("session_not_found", "Session is not accessible")
        return row

    async def save(self, conn, session_id, row, values):
        if row is None:
            await conn.execute(insert(management).values(session_id=session_id, **{
                key: value for key, value in values.items() if key != "session_id"
            }))
        else:
            await conn.execute(update(management).where(management.c.session_id == session_id)
                               .values(**values))

    async def deletion(self, session_id):
        async with self.store.engine.connect() as conn:
            row = await self.row(conn, session_id)
            if row is None or row["deletion"] is None:
                raise DomainError("deletion_not_found", "Session deletion has not been requested")
            return row

    async def pending(self):
        async with self.store.engine.connect() as conn:
            return (await conn.execute(select(management.c.session_id).where(
                management.c.deletion == "deleting"
            ))).scalars().all()

    async def cleanup_queue(self):
        async with self.store.engine.connect() as conn:
            rows = (await conn.execute(select(management).where(
                management.c.deletion.in_(("deleting", "cleanup_required"))
            ))).mappings().all()
            return [self.deletion_state(row) for row in rows]

    @staticmethod
    def deletion_state(row):
        result = {"session_id": row["session_id"], "status": row["deletion"]}
        if row["deletion"] == "cleanup_required":
            result["reason"] = row["cleanup"].get("reason", "cleanup_failed")
        return result

    async def other_deletion(self, session_id):
        async with self.store.engine.connect() as conn:
            return await conn.scalar(select(management.c.session_id).where(
                management.c.session_id != session_id,
                management.c.deletion.in_(("deleting", "cleanup_required")),
            ).limit(1)) is not None

    async def begin(self, session_id, verify_workspace, safe_reads):
        from deep_harness.infrastructure.artifacts import artifacts, run_artifacts

        async with self.store.transaction() as conn:
            row = await self.row(conn, session_id)
            if row is not None and row["deletion"] is not None:
                if row["deletion"] == "cleanup_required":
                    await self.save(conn, session_id, row, {"deletion": "deleting"})
                    return "deleting"
                return row["deletion"]
            session = await self.accessible(conn, session_id)
            admissions = select(s.admissions.c.id).where(s.admissions.c.session_id == session_id)
            checks = (
                (select(s.admissions.c.id).where(
                    s.admissions.c.session_id == session_id,
                    s.admissions.c.status == "started",
                ), "此会话仍在执行，请先停止运行"),
                (select(s.job_stage_attempts.c.session_id).where(
                    s.job_stage_attempts.c.session_id == session_id,
                ), "此会话由长任务阶段管理，无法单独删除"),
                (select(s.session_branches.c.session_id).where(
                    s.session_branches.c.parent_id == session_id,
                ), "请先删除依赖此会话的分支"),
                (select(s.delegations.c.id).where(
                    s.delegations.c.session_id == session_id,
                    s.delegations.c.child_session_id.is_not(None),
                ), "请先处理并删除关联的子会话"),
                (select(s.delegations.c.id).where(
                    (s.delegations.c.session_id == session_id) |
                    (s.delegations.c.child_session_id == session_id),
                    s.delegations.c.status.in_((
                        "queued", "running", "waiting_approval", "unknown",
                    )),
                ), "请先结束或核对关联的子任务"),
            )
            for query, reason in checks:
                if await conn.scalar(query.limit(1)) is not None:
                    raise DomainError("session_deletion_blocked", reason)
            await self.verify_dispatched(conn, admissions, safe_reads)
            runs = (await conn.execute(admissions)).scalars().all()
            children = (await conn.execute(select(s.delegations.c.id).where(
                s.delegations.c.session_id == session_id
            ))).scalars().all()
            tasks = [session_id, *children]
            await verify_workspace(tasks)
            has_artifacts = (await conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
            )).scalar()
            owned, shared = [], {}
            if has_artifacts:
                candidates = (await conn.execute(select(artifacts.c.id).where(
                    artifacts.c.session_id == session_id
                ))).scalars().all()
                for artifact_id in candidates:
                    owner = await conn.scalar(select(s.admissions.c.session_id).join(
                        run_artifacts, run_artifacts.c.run_id == s.admissions.c.id,
                    ).where(run_artifacts.c.artifact_id == artifact_id,
                            s.admissions.c.session_id != session_id).limit(1))
                    if owner is None:
                        owned.append(artifact_id)
                    else:
                        shared[artifact_id] = owner
            await self.save(conn, session_id, row, {
                "title": None, "pin": 0, "deletion": "deleting",
                "order_revision": (row["order_revision"] if row else 0) + 1,
                "request_hash": fingerprint([session["principal"], session["request_id"]]),
                "cleanup": {"runs": runs, "tasks": tasks, "owned": owned, "shared": shared,
                            "has_artifacts": bool(has_artifacts)},
            })
            return "deleting"

    async def verify_dispatched(self, conn, admissions, safe_reads):
        pending = (await conn.execute(select(s.effects).where(
            s.effects.c.admission_id.in_(admissions), s.effects.c.status == "dispatched",
        ))).mappings().all()
        if not pending:
            return
        receipts = (await conn.execute(select(s.model_receipts).where(
            s.model_receipts.c.admission_id.in_({row["admission_id"] for row in pending}),
        ))).mappings().all()
        proved_reads = set()
        for receipt in receipts:
            for call in receipt["response"].get("tool_calls") or ():
                if call.get("name") in safe_reads and isinstance(call.get("args"), dict):
                    key = f"{receipt['admission_id']}:{call['id']}"
                    proved_reads.add((key, fingerprint({
                        "name": call["name"], "arguments": call["args"],
                    })))
        if any((row["id"], row["proposal_hash"]) not in proved_reads for row in pending):
            raise DomainError(
                "session_deletion_reconciliation_required", "请先核对结果不明的工具操作",
            )

    async def finish(self, session_id, plan):
        from deep_harness.infrastructure.artifacts import artifacts, run_artifacts

        async with self.store.transaction() as conn:
            if plan["has_artifacts"]:
                for artifact_id, owner in plan["shared"].items():
                    await conn.execute(update(artifacts).where(artifacts.c.id == artifact_id)
                                       .values(session_id=owner, run_id=None))
                await conn.execute(delete(run_artifacts).where(
                    run_artifacts.c.run_id.in_(select(s.admissions.c.id).where(
                        s.admissions.c.session_id == session_id
                    )) | run_artifacts.c.artifact_id.in_(plan["owned"])
                ))
                await conn.execute(delete(artifacts).where(artifacts.c.session_id == session_id))
            await remove_session_rows(conn, session_id)
            await self.save(conn, session_id, True, {"deletion": "deleted", "cleanup": None})

    async def needs_cleanup(self, session_id, reason):
        async with self.store.transaction() as conn:
            row = await self.row(conn, session_id)
            if row["deletion"] == "deleting":
                await self.save(conn, session_id, row, {
                    "deletion": "cleanup_required", "cleanup": {**row["cleanup"], "reason": reason},
                })


async def remove_session_rows(conn, session_id):
    admissions = select(s.admissions.c.id).where(s.admissions.c.session_id == session_id)
    interactions = select(s.interactions.c.id).where(s.interactions.c.session_id == session_id)
    delegations = select(s.delegations.c.id).where(s.delegations.c.session_id == session_id)
    for table, condition in (
        (s.approval_decisions, s.approval_decisions.c.interaction_id.in_(interactions)),
        (s.delegation_tool_receipts, s.delegation_tool_receipts.c.delegation_id.in_(delegations)),
        (s.approval_waits, s.approval_waits.c.admission_id.in_(admissions)),
        (s.approval_revocations, s.approval_revocations.c.admission_id.in_(admissions)),
        (s.tool_attempts, s.tool_attempts.c.admission_id.in_(admissions)),
        (s.model_receipts, s.model_receipts.c.admission_id.in_(admissions)),
        (s.effects, s.effects.c.admission_id.in_(admissions)),
        (s.resolutions, s.resolutions.c.admission_id.in_(admissions)),
    ):
        await conn.execute(delete(table).where(condition))
    await conn.execute(update(s.delegations).where(s.delegations.c.child_session_id == session_id)
                       .values(child_session_id=None, child_admission_id=None))
    await conn.exec_driver_sql("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
    for table in (
        s.memory_candidates, s.curator_runs, s.context_archives, s.context_heads,
        s.session_branches, s.session_policies, s.delegations, s.interactions,
        s.events, s.messages, s.admissions,
    ):
        await conn.execute(delete(table).where(table.c.session_id == session_id))
    await conn.execute(delete(s.sessions).where(s.sessions.c.id == session_id))
