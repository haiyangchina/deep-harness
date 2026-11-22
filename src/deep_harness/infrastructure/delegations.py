"""Durable identity and results for tool-mediated Deep specialist tasks."""

from uuid import uuid4

from sqlalchemy import func, insert, select, update

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.server_config import number


class DelegationLedger:
    def __init__(self, store, executions):
        self.store = store
        self.executions = executions

    async def begin(self, claim, call):
        identifier = f"{claim.admission.id}:delegate:{call.id}"
        digest = fingerprint(call.arguments)
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            previous = (
                (await conn.execute(select(s.delegations).where(s.delegations.c.id == identifier)))
                .mappings()
                .first()
            )
            if previous is not None:
                if previous["task_hash"] != digest:
                    raise DomainError("delegation_conflict", "Delegated task changed during replay")
                return dict(previous)
            mode = "background" if call.arguments.get("background") else "sync"
            if mode == "background":
                count = await conn.execute(
                    select(func.count())
                    .select_from(s.delegations)
                    .where(
                        s.delegations.c.admission_id == claim.admission.id,
                        s.delegations.c.mode == "background",
                    )
                )
                if count.scalar_one() >= number("delegation.max_per_admission"):
                    raise DomainError("delegation_limit", "At most four background tasks per turn")
            await conn.execute(
                insert(s.delegations).values(
                    id=identifier,
                    session_id=claim.admission.session_id,
                    admission_id=claim.admission.id,
                    parent_tool_call_id=call.id,
                    kind=call.arguments["kind"],
                    mode=mode,
                    task_hash=digest,
                    status="running",
                )
            )
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                "delegation.started",
                {"id": identifier, "kind": call.arguments["kind"], "mode": mode},
            )
        return {"id": identifier, "status": "running", "mode": mode}

    async def spawn_background(
        self,
        claim,
        identifier,
        *,
        prompt,
        tools,
        harness,
        snapshot,
        task,
        permission_mode="ask",
    ):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            row = (
                (await conn.execute(select(s.delegations).where(s.delegations.c.id == identifier)))
                .mappings()
                .one()
            )
            if row["mode"] != "background":
                raise DomainError("delegation_conflict", "Delegation is not a background task")
            if row["child_session_id"]:
                return {
                    "delegation_id": identifier,
                    "session_id": row["child_session_id"],
                    "admission_id": row["child_admission_id"],
                }
            session_id, admission_id = str(uuid4()), str(uuid4())
            creation = {"prompt": prompt, "tools": tools, "engine": "deep", "harness": harness}
            await conn.execute(
                insert(s.sessions).values(
                    id=session_id,
                    profile_id=self.store.profile_id,
                    principal="operator",
                    request_id=f"background:{identifier}",
                    payload_hash=fingerprint(creation),
                    prompt=prompt,
                    tools=list(tools),
                    engine="deep",
                    harness=harness,
                    permission_mode=permission_mode,
                )
            )
            await conn.execute(
                insert(s.session_policies).values(
                    session_id=session_id,
                    snapshot=snapshot,
                    fingerprint=fingerprint(snapshot),
                )
            )
            await conn.execute(
                insert(s.admissions).values(
                    id=admission_id,
                    session_id=session_id,
                    principal="operator",
                    submission_id="initial",
                    payload_hash=fingerprint({"text": task}),
                    text=task,
                    status="queued",
                    generation=0,
                )
            )
            await conn.execute(
                insert(s.messages).values(
                    id=f"{admission_id}:user",
                    session_id=session_id,
                    admission_id=admission_id,
                    payload={"id": f"{admission_id}:user", "role": "user", "content": task},
                )
            )
            await conn.execute(
                update(s.delegations)
                .where(s.delegations.c.id == identifier)
                .values(
                    child_session_id=session_id,
                    child_admission_id=admission_id,
                    status="queued",
                )
            )
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                "delegation.launched",
                {"id": identifier, "child_session_id": session_id},
            )
            await self.store.append_event(
                conn, session_id, "admission.accepted", {"id": admission_id}
            )
        return {"delegation_id": identifier, "session_id": session_id, "admission_id": admission_id}

    async def get(self, session_id, identifier):
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.delegations).where(
                            s.delegations.c.session_id == session_id,
                            s.delegations.c.id == identifier,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise DomainError("delegation_not_found", "Delegation is not accessible")
        return dict(row)

    async def child_owner(self, child_session_id):
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.delegations).where(
                            s.delegations.c.child_session_id == child_session_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def stop_requested(self, child_session_id):
        row = await self.child_owner(child_session_id)
        return bool(row and row["stop_requested"])

    async def request_stop(self, session_id, identifier):
        async with self.store.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.delegations).where(
                            s.delegations.c.session_id == session_id,
                            s.delegations.c.id == identifier,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["mode"] != "background":
                raise DomainError("delegation_not_found", "Background delegation is not accessible")
            if not row["stop_requested"]:
                await conn.execute(
                    update(s.delegations)
                    .where(s.delegations.c.id == identifier)
                    .values(stop_requested=1)
                )
                await self.store.append_event(
                    conn, session_id, "delegation.stop_requested", {"id": identifier}
                )
        return dict(row)

    async def settle_background(self, child_session_id, status, result):
        async with self.store.transaction() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.delegations).where(
                            s.delegations.c.child_session_id == child_session_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return
            admissions = (
                (
                    await conn.execute(
                        select(s.admissions.c.status, s.admissions.c.result)
                        .where(s.admissions.c.session_id == child_session_id)
                        .order_by(s.admissions.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            blocking = next(
                (
                    item
                    for item in admissions
                    if item["status"]
                    in {"unknown", "interrupted", "waiting_approval", "started", "reconciled"}
                ),
                None,
            )
            current = blocking or (admissions[-1] if admissions else None)
            if current is not None:
                status, result = current["status"], current["result"]
            if status == "started":
                status = "running"
            mapped = "stopped" if row["stop_requested"] and status == "cancelled" else status
            if row["status"] == mapped and row["result"] == result:
                return
            await conn.execute(
                update(s.delegations)
                .where(s.delegations.c.id == row["id"])
                .values(status=mapped, result=result)
            )
            await self.store.append_event(
                conn, row["session_id"], f"delegation.{mapped}", {"id": row["id"]}
            )

    async def mark_steered(self, identifier, admission_id):
        async with self.store.transaction() as conn:
            row = (
                (await conn.execute(select(s.delegations).where(s.delegations.c.id == identifier)))
                .mappings()
                .one()
            )
            if row["stop_requested"]:
                raise DomainError("delegation_stopped", "Background delegation has been stopped")
            existing = (
                await conn.execute(
                    select(s.events.c.seq).where(
                        s.events.c.session_id == row["session_id"],
                        s.events.c.type == "delegation.steered",
                        s.events.c.payload["admission_id"].as_string() == admission_id,
                    )
                )
            ).first()
            if existing:
                return
            if row["status"] not in {"queued", "running", "waiting_approval"}:
                await conn.execute(
                    update(s.delegations)
                    .where(s.delegations.c.id == identifier)
                    .values(status="queued", result=None)
                )
            await self.store.append_event(
                conn,
                row["session_id"],
                "delegation.steered",
                {"id": identifier, "admission_id": admission_id},
            )

    async def reconcile_background(self):
        async with self.store.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            s.delegations.c.id,
                            s.delegations.c.session_id,
                            s.delegations.c.status,
                            s.delegations.c.child_session_id,
                            s.admissions.c.status.label("parent_status"),
                        )
                        .join(s.admissions, s.admissions.c.id == s.delegations.c.admission_id)
                        .where(s.delegations.c.mode == "background")
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            if row["child_session_id"]:
                await self.settle_background(row["child_session_id"], "queued", None)
            elif row["parent_status"] != "started" and row["status"] != "unknown":
                async with self.store.transaction() as conn:
                    await conn.execute(
                        update(s.delegations)
                        .where(s.delegations.c.id == row["id"])
                        .values(status="unknown")
                    )
                    await self.store.append_event(
                        conn, row["session_id"], "delegation.unknown", {"id": row["id"]}
                    )

    async def finish(self, claim, identifier, result):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            row = (
                (await conn.execute(select(s.delegations).where(s.delegations.c.id == identifier)))
                .mappings()
                .one()
            )
            if row["status"] == "completed":
                if row["result"] != result:
                    raise DomainError("delegation_conflict", "Delegation result changed")
                return
            await conn.execute(
                update(s.delegations)
                .where(s.delegations.c.id == identifier)
                .values(status="completed", result=result)
            )
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                "delegation.completed",
                {"id": identifier},
            )

    async def tool_result(self, identifier, call):
        key = f"{identifier}:tool:{call.id}"
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.delegation_tool_receipts).where(
                            s.delegation_tool_receipts.c.id == key
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        if row["name"] != call.name or row["arguments_hash"] != fingerprint(call.arguments):
            raise DomainError("delegation_conflict", "Subagent tool call changed during replay")
        return row["result"]

    async def save_tool(self, claim, identifier, call, result):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            await conn.execute(
                insert(s.delegation_tool_receipts).values(
                    id=f"{identifier}:tool:{call.id}",
                    delegation_id=identifier,
                    name=call.name,
                    arguments_hash=fingerprint(call.arguments),
                    result=result,
                )
            )

    async def list(self, session_id):
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(
                select(s.delegations)
                .where(s.delegations.c.session_id == session_id)
                .order_by(s.delegations.c.id)
            )
            return [dict(row) for row in rows.mappings()]
