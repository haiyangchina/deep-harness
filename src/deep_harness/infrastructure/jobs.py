"""Durable long-task plans and the association between stages and admissions."""

from uuid import uuid4

from sqlalchemy import and_, func, insert, select, update

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s


class JobLedger:
    def __init__(self, store):
        self.store = store

    async def create(self, request_id, definition, runtime_hash, deadline):
        digest = fingerprint(definition)
        async with self.store.transaction() as conn:
            existing = (
                (await conn.execute(select(s.jobs).where(s.jobs.c.request_id == request_id)))
                .mappings()
                .first()
            )
            if existing:
                if existing["request_hash"] != digest:
                    raise DomainError("idempotency_conflict", "Long-task plan changed")
                return existing["id"]
            job_id = str(uuid4())
            await conn.execute(
                insert(s.jobs).values(
                    id=job_id,
                    request_id=request_id,
                    request_hash=digest,
                    goal=definition["goal"],
                    engine=definition["engine"],
                    budget=definition["budget"],
                    parallelism=definition["parallelism"],
                    runtime_hash=runtime_hash,
                    status="running",
                    deadline=deadline,
                )
            )
            await conn.execute(
                insert(s.job_stages),
                [
                    {"job_id": job_id, "position": position, "definition": stage}
                    for position, stage in enumerate(definition["stages"])
                ],
            )
            return job_id

    async def list(self, *, recoverable_only=False):
        query = select(
            s.jobs.c.id, s.jobs.c.goal, s.jobs.c.status, s.jobs.c.notice, s.jobs.c.deadline
        )
        if recoverable_only:
            query = query.where(s.jobs.c.status.in_(("running", "paused", "cancelling")))
        else:
            query = query.order_by(s.jobs.c.id).limit(100)
        async with self.store.engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(query)).mappings()]

    async def get(self, job_id):
        stages = s.job_stages
        attempts = s.job_stage_attempts
        admissions = s.admissions
        async with self.store.engine.connect() as conn:
            job = (
                (await conn.execute(select(s.jobs).where(s.jobs.c.id == job_id))).mappings().first()
            )
            if job is None:
                raise DomainError("job_not_found", "Long task does not exist")
            rows = await conn.execute(
                select(
                    stages.c.position,
                    stages.c.definition,
                    stages.c.accepted_evidence,
                    attempts.c.session_id,
                    attempts.c.limits,
                    admissions.c.id.label("admission_id"),
                    admissions.c.status.label("admission_status"),
                    admissions.c.result,
                    admissions.c.active_seconds,
                )
                .select_from(
                    stages.outerjoin(
                        attempts,
                        and_(
                            attempts.c.job_id == stages.c.job_id,
                            attempts.c.position == stages.c.position,
                        ),
                    ).outerjoin(
                        admissions,
                        and_(
                            admissions.c.session_id == attempts.c.session_id,
                            admissions.c.submission_id == "stage",
                        ),
                    )
                )
                .where(stages.c.job_id == job_id)
                .order_by(stages.c.position)
            )
            result = dict(job)
            result["stages"] = [dict(row) for row in rows.mappings()]
            return result

    async def set_status(self, job_id, status, notice=None):
        async with self.store.transaction() as conn:
            await conn.execute(
                update(s.jobs).where(s.jobs.c.id == job_id).values(status=status, notice=notice)
            )

    async def pause_running(self):
        async with self.store.transaction() as conn:
            await conn.execute(
                update(s.jobs)
                .where(s.jobs.c.status == "running")
                .values(status="paused", notice="Paused when the backend shut down")
            )

    async def attach(self, job_id, position, session_id, limits):
        async with self.store.transaction() as conn:
            await conn.execute(
                insert(s.job_stage_attempts).values(
                    job_id=job_id,
                    position=position,
                    attempt=1,
                    session_id=session_id,
                    limits=limits,
                )
            )

    async def paused(self, job_id):
        async with self.store.engine.connect() as conn:
            status = await conn.scalar(select(s.jobs.c.status).where(s.jobs.c.id == job_id))
            return status != "running"

    async def deadline(self, job_id):
        async with self.store.engine.connect() as conn:
            return await conn.scalar(select(s.jobs.c.deadline).where(s.jobs.c.id == job_id))

    async def owner(self, session_id):
        async with self.store.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(s.job_stage_attempts).where(
                            s.job_stage_attempts.c.session_id == session_id
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def accept(self, job_id, position, evidence):
        async with self.store.transaction() as conn:
            await conn.execute(
                update(s.job_stages)
                .where(s.job_stages.c.job_id == job_id, s.job_stages.c.position == position)
                .values(accepted_evidence=evidence)
            )

    async def evidence(self, admission_id):
        async with self.store.engine.connect() as conn:
            effects = await conn.execute(
                select(s.effects.c.id, s.effects.c.status)
                .where(s.effects.c.admission_id == admission_id)
                .order_by(s.effects.c.id)
            )
            totals = await conn.execute(
                select(
                    func.count().label("model_calls"),
                    func.coalesce(
                        func.sum(s.model_receipts.c.response["usage"]["total_tokens"].as_integer()),
                        0,
                    ).label("tokens"),
                ).where(s.model_receipts.c.admission_id == admission_id)
            )
            tools = await conn.scalar(
                select(func.count())
                .select_from(s.tool_attempts)
                .where(s.tool_attempts.c.admission_id == admission_id)
            )
            return {
                **totals.mappings().one(),
                "tool_calls": tools,
                "effects": [dict(row) for row in effects.mappings()],
            }
