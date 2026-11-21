"""Checkpointed coordination of explicitly planned long tasks."""

import asyncio
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from deep_harness.application.capabilities import CORE_TOOLS, READ_ONLY_CORE
from deep_harness.application.config import ExecutionSettings
from deep_harness.application.workspace import PROJECT_PROMPT
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.jobs import JobLedger


class StagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=1600)
    acceptance: str = Field(min_length=1, max_length=600)
    group: int | None = Field(default=None, ge=0)
    read_only: bool = False
    verify: bool = False


class JobBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wall_seconds: int = Field(default=1800, ge=1, le=28_800)
    max_model_calls: int = Field(default=48, ge=1, le=1024)
    max_tool_calls: int = Field(default=192, ge=1, le=8192)
    max_run_tokens: int = Field(default=300_000, ge=100, le=10_000_000)


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=2400)
    engine: Literal["standard", "deep"] = "standard"
    stages: list[StagePlan] = Field(min_length=1, max_length=12)
    budget: JobBudget = Field(default_factory=JobBudget)
    parallelism: int = Field(default=1, ge=1, le=2)
    deadline_seconds: int | None = Field(default=None, ge=1, le=604_800)

    @model_validator(mode="after")
    def validate_plan(self):
        previous = -1
        counts = {}
        for stage in self.stages:
            group = previous + 1 if stage.group is None else stage.group
            if group < previous:
                raise ValueError("Stage groups must be in ascending order")
            previous = stage.group = group
            counts[group] = counts.get(group, 0) + 1
            if stage.read_only and stage.verify:
                raise ValueError("A command-verification stage cannot be read-only")
        if any(counts[stage.group] > 1 and not stage.read_only for stage in self.stages):
            raise ValueError("Stages in a shared group must be read-only")
        count = len(self.stages)
        limits = self.budget.model_dump()
        if (
            any(value < count for value in limits.values())
            or limits["max_run_tokens"] < count * 100
        ):
            raise ValueError("Budget is too small to assign to every stage")
        return self


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "incomplete"]
    summary: str = Field(min_length=1, max_length=1200)
    artifacts: list[str] = Field(max_length=16)
    next_step: str | None = Field(default=None, max_length=800)


def result_for(stage):
    if stage["accepted_evidence"] is not None:
        return {
            "status": "complete",
            "summary": stage["accepted_evidence"],
            "artifacts": [],
            "next_step": "",
        }
    if stage["admission_status"] != "completed":
        return None
    raw = stage["result"]
    if not isinstance(raw, str) or len(raw) > 16_000:
        return None
    value = raw.strip()
    if value.startswith("```json") and value.endswith("```"):
        value = value[7:-3].strip()
    try:
        return StageResult.model_validate_json(value).model_dump()
    except ValidationError:
        return None


class JobService:
    def __init__(self, app):
        self.app = app
        self.ledger = JobLedger(app.store)
        self.lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()

    def runtime_hash(self):
        return fingerprint(self.app.sessions.snapshot(CORE_TOOLS))

    async def create(self, plan):
        async with self.lock:
            job_id = await self.ledger.create(
                plan.request_id,
                plan.model_dump(),
                self.runtime_hash(),
                time.time() + plan.deadline_seconds if plan.deadline_seconds is not None else None,
            )
        self.kick(job_id)
        return await self.detail(job_id)

    def kick(self, job_id, *, wake_existing=False):
        if not self.app.closing:
            self.app.track(asyncio.create_task(self.advance(job_id, wake_existing=wake_existing)))

    async def after_session(self, session_id):
        owner = await self.ledger.owner(session_id)
        if owner is not None:
            self.kick(owner["job_id"])

    async def verified(self, stage):
        report = result_for(stage)
        if not report or report["status"] != "complete":
            return False
        if stage["accepted_evidence"] is not None or not stage["definition"]["verify"]:
            return True
        messages = await self.app.store.history(stage["session_id"])
        commands = {
            call["id"]
            for message in messages
            for call in message.get("tool_calls", [])
            if call.get("name") in {"bash", "run_command"}
        }
        outputs = []
        for message in messages:
            if message.get("tool_call_id") not in commands:
                continue
            try:
                value = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and type(value.get("exit_code")) is int:
                outputs.append(value["exit_code"])
        return bool(outputs) and all(code == 0 for code in outputs)

    async def detail(self, job_id):
        job = await self.ledger.get(job_id)
        used = {"wall_seconds": 0.0, "max_model_calls": 0, "max_tool_calls": 0, "max_run_tokens": 0}
        for stage in job["stages"]:
            stage["report"] = result_for(stage)
            stage["verified"] = await self.verified(stage)
            stage["evidence"] = None
            if stage["admission_id"]:
                entry = await self.ledger.evidence(stage["admission_id"])
                stage["evidence"] = entry
                used["wall_seconds"] += stage["active_seconds"]
                used["max_model_calls"] += entry["model_calls"]
                used["max_tool_calls"] += entry["tool_calls"]
                used["max_run_tokens"] += entry["tokens"]
        job["used"] = used
        job["remaining"] = {key: max(0, value - used[key]) for key, value in job["budget"].items()}
        return job

    async def command(self, job_id, action, *, position=None, evidence=None):
        async with self.lock:
            job = await self.ledger.get(job_id)
            if job["status"] in {"completed", "cancelled", "cancelling"}:
                return await self.detail(job_id)
            if action == "pause":
                await self.ledger.set_status(
                    job_id, "paused", "Active stages pause at the next safe execution boundary"
                )
            elif action == "cancel":
                await self.ledger.set_status(job_id, "cancelling", "Waiting for active stages")
            elif action == "accept":
                if (
                    job["status"] != "blocked"
                    or position is None
                    or not evidence
                    or not evidence.strip()
                ):
                    raise DomainError(
                        "job_not_reviewable", "A blocked stage needs concrete evidence"
                    )
                stage = next((item for item in job["stages"] if item["position"] == position), None)
                if not stage or stage["admission_status"] not in {
                    "reconciled",
                    "interrupted",
                    "completed",
                }:
                    raise DomainError("job_not_reviewable", "Reconcile unknown effects first")
                if await self.verified(stage) or any(
                    effect["status"] == "dispatched"
                    for effect in (await self.ledger.evidence(stage["admission_id"]))["effects"]
                ):
                    raise DomainError(
                        "job_not_reviewable", "Stage already verified or needs reconciliation"
                    )
                await self.ledger.accept(job_id, position, evidence.strip())
            elif action == "resume":
                if job["deadline"] is not None and job["deadline"] <= time.time():
                    raise DomainError("job_deadline", "The long-task deadline has elapsed")
                unsafe = any(
                    stage["admission_status"]
                    in {"unknown", "interrupted", "reconciled", "cancelled"}
                    and stage["accepted_evidence"] is None
                    for stage in job["stages"]
                )
                if unsafe:
                    raise DomainError("job_blocked", "Review the stopped stage before continuing")
                await self.ledger.set_status(job_id, "running")
        self.kick(job_id, wake_existing=action == "resume")
        return await self.detail(job_id)

    async def advance(self, job_id, *, wake_existing=False):
        async with self.lock:
            if self.app.closing:
                return
            job = await self.ledger.get(job_id)
            if job["status"] == "cancelling":
                for stage in job["stages"]:
                    if stage["admission_status"] in {"queued", "waiting_approval", "job_paused"}:
                        try:
                            await self.app.executions.cancel_pending(
                                stage["session_id"], stage["admission_id"]
                            )
                        except DomainError as exc:
                            if exc.code != "execution_busy":
                                raise
                current = await self.ledger.get(job_id)
                if not any(stage["admission_status"] == "started" for stage in current["stages"]):
                    await self.ledger.set_status(job_id, "cancelled")
                return
            if job["status"] not in {"running", "paused"}:
                return
            pending = []
            for stage in job["stages"]:
                if await self.verified(stage):
                    continue
                status = stage["admission_status"]
                if status in {"unknown", "interrupted", "reconciled", "cancelled", "completed"}:
                    await self.ledger.set_status(
                        job_id, "blocked", f"Stage {stage['position'] + 1} needs review ({status})"
                    )
                    return
                pending.append(stage)
            if not pending:
                await self.ledger.set_status(job_id, "completed")
                return
            if job["status"] == "paused":
                return
            if job["deadline"] is not None and time.time() >= job["deadline"]:
                await self.ledger.set_status(job_id, "blocked", "Long-task deadline has elapsed")
                return
            if job["runtime_hash"] != self.runtime_hash():
                await self.ledger.set_status(job_id, "blocked", "Runtime configuration has changed")
                return
            group = pending[0]["definition"]["group"]
            current = [stage for stage in pending if stage["definition"]["group"] == group]
            active = sum(stage["admission_id"] is not None for stage in current)
            for stage in current:
                if stage["admission_id"] is not None:
                    if wake_existing:
                        self.app.schedule(stage["session_id"])
                elif active < job["parallelism"]:
                    await self.enqueue(job, stage)
                    active += 1

    async def enqueue(self, job, stage):
        definition = stage["definition"]
        position = stage["position"]
        limits = self.app.limits.model_dump()
        count = len(job["stages"])
        for key, value in job["budget"].items():
            limits[key] = min(limits[key], value // count + int(position < value % count))
        limits = ExecutionSettings.model_validate(limits).model_dump()
        prior = []
        for item in job["stages"]:
            if item["definition"]["group"] >= definition["group"]:
                continue
            report = result_for(item)
            prior.append(
                {
                    "stage": item["position"] + 1,
                    "admission_id": item["admission_id"],
                    "summary": report["summary"][:250],
                    "artifacts": [value[:100] for value in report["artifacts"][:2]],
                }
            )
        prompt = (
            f"{PROJECT_PROMPT}\n"
            "You execute one stage of a long task. Treat prior results as evidence. "
            "Return JSON only with status (complete or incomplete), summary, "
            "artifacts (list of strings) and next_step (string or null). "
            "Mark complete only with evidence."
        )
        if stage["session_id"] is None:
            if definition["read_only"]:
                tools = tuple(sorted(READ_ONLY_CORE))
            else:
                tools = tuple(sorted(CORE_TOOLS))
            session = await self.app.store.create_session(
                principal="operator",
                request_id=f"job-stage:{job['id']}:{position}",
                prompt=prompt,
                tools=tools,
                engine=job["engine"],
                permission_mode="ask",
                runtime_snapshot=self.app.sessions.snapshot(tools, engine=job["engine"]),
            )
            await self.ledger.attach(job["id"], position, session.id, limits)
        else:
            session = await self.app.store.get_session(stage["session_id"], "operator")
        handoff = json.dumps(prior, ensure_ascii=False)
        await self.app.store.admit(
            session,
            principal="operator",
            submission_id="stage",
            text=(
                f"Job objective: {job['goal']}\n"
                f"Stage: {definition['title']}\n{definition['goal']}\n"
                f"Acceptance: {definition['acceptance']}\n"
                f"Prior stage evidence: {handoff}"
            ),
        )
        self.app.schedule(session.id)
