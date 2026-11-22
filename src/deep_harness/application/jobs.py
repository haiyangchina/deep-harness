"""Checkpointed coordination of explicitly planned long tasks."""

import asyncio
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from deep_harness.application.capabilities import CORE_TOOLS, READ_ONLY_CORE
from deep_harness.application.coding_evidence import coding_evidence
from deep_harness.application.config import ExecutionSettings
from deep_harness.application.workspace import PROJECT_PROMPT
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.jobs import JobLedger
from deep_harness.server_config import number


class ConfiguredStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent", "verify", "tool"]
    model_id: str
    tools: tuple[str, ...] = ()
    source: int | None = Field(default=None, ge=0)
    schema_: dict | None = Field(default=None, alias="schema")
    instructions: str = ""


class StagePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=number("stage.title_max_length"))
    goal: str = Field(min_length=1, max_length=number("stage.goal_max_length"))
    acceptance: str = Field(min_length=1, max_length=number("stage.acceptance_max_length"))
    group: int | None = Field(default=None, ge=0)
    read_only: bool = False
    verify: bool = False
    verification_command: str | None = Field(
        default=None, min_length=1, max_length=number("shell.command_max_length")
    )
    step: ConfiguredStep | None = None


class JobBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wall_seconds: int = Field(
        default=number("job_budget.wall_seconds"), ge=1, le=number("job_budget.wall_seconds_max")
    )
    max_model_calls: int = Field(
        default=number("job_budget.max_model_calls"),
        ge=1,
        le=number("job_budget.max_model_calls_max"),
    )
    max_tool_calls: int = Field(
        default=number("job_budget.max_tool_calls"),
        ge=1,
        le=number("job_budget.max_tool_calls_max"),
    )
    max_run_tokens: int = Field(
        default=number("job_budget.max_run_tokens"),
        ge=number("job_budget.max_run_tokens_min"),
        le=number("job_budget.max_run_tokens_max"),
    )


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=number("jobs.request_id_max_length"))
    goal: str = Field(min_length=1, max_length=number("jobs.goal_max_length"))
    engine: Literal["standard", "deep"] = "standard"
    stages: list[StagePlan] = Field(min_length=1, max_length=number("jobs.stages_max_length"))
    budget: JobBudget = Field(default_factory=JobBudget)
    parallelism: int = Field(
        default=number("jobs.parallelism"), ge=1, le=number("jobs.parallelism_max")
    )
    deadline_seconds: int | None = Field(default=None, ge=1, le=number("jobs.deadline_seconds_max"))

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
            if stage.verification_command is not None and not stage.verify:
                raise ValueError("A verification command requires command verification")
            if stage.read_only and stage.verify:
                raise ValueError("A command-verification stage cannot be read-only")
        if any(counts[stage.group] > 1 and not stage.read_only for stage in self.stages):
            raise ValueError("Stages in a shared group must be read-only")
        count = len(self.stages)
        limits = self.budget.model_dump()
        if any(value < count for value in limits.values()) or limits[
            "max_run_tokens"
        ] < count * number("execution.max_run_tokens_min"):
            raise ValueError("Budget is too small to assign to every stage")
        return self


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "incomplete"]
    summary: str = Field(min_length=1, max_length=number("stage_report.summary_max_length"))
    artifacts: list[str] = Field(max_length=number("stage_report.artifacts_max_length"))
    next_step: str | None = Field(
        default=None, max_length=number("stage_report.next_step_max_length")
    )


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
    if not isinstance(raw, str) or len(raw) > number("stage_report.max_chars"):
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
            definition = plan.model_dump()
            for stage in definition["stages"]:
                if stage["verification_command"] is None:
                    stage.pop("verification_command")
                if stage["step"] is None:
                    stage.pop("step")
                else:
                    stage["step"]["schema"] = stage["step"].pop("schema_")
            job_id = await self.ledger.create(
                plan.request_id,
                definition,
                await self.planned_runtime(definition["stages"]),
                time.time() + plan.deadline_seconds if plan.deadline_seconds is not None else None,
            )
        self.kick(job_id)
        return await self.detail(job_id)

    async def planned_runtime(self, stages):
        if not any(stage.get("step") for stage in stages):
            return self.runtime_hash()
        if self.app.models is None or any(not stage.get("step") for stage in stages):
            raise DomainError("workflow_config", "Configured steps require a server workflow")
        snapshots = []
        for stage in stages:
            step = stage["step"]
            tools = tuple(step["tools"])
            snapshots.append(self.app.sessions.snapshot(
                tools, mcp_catalog=await self.app.sessions.catalog_for(tools),
                model_id=step["model_id"],
            ))
        return fingerprint(snapshots)

    def kick(self, job_id, *, wake_existing=False):
        if not self.app.closing:
            self.app.track(asyncio.create_task(self.advance(job_id, wake_existing=wake_existing)))

    async def after_session(self, session_id):
        owner = await self.ledger.owner(session_id)
        if owner is not None:
            self.kick(owner["job_id"])

    async def verified(self, stage):
        report = result_for(stage)
        return self.accepted(stage, report, await self.verification(stage) if report else None)

    @staticmethod
    def accepted(stage, report, evidence):
        if not report or report["status"] != "complete":
            return False
        if stage["accepted_evidence"] is not None or not stage["definition"]["verify"]:
            return True
        return (
            evidence is not None
            and type(evidence["exit_code"]) is int
            and evidence["exit_code"] == 0
        )

    async def verification(self, stage):
        if not stage["admission_id"] or not stage["definition"]["verify"]:
            return None
        messages = await self.app.store.history(
            stage["session_id"], admission_id=stage["admission_id"]
        )
        files, commands, last_write = coding_evidence(messages)
        expected = stage["definition"].get("verification_command")
        # The most recent command must be the verifier. Other shell operations
        # can also mutate the workspace even when they supplied no file receipt.
        if not commands:
            return None
        last = commands[-1]
        if last["batch"] <= last_write:
            return None
        if expected is not None and (
            last["tool"] != "bash" or last["arguments"].get("command") != expected
        ):
            return None
        if last["arguments"].get("purpose") == "reproduce":
            return None
        if sum(command["batch"] == last["batch"] for command in commands) > 1:
            return None
        return {**last, "files": files}

    async def detail(self, job_id):
        job = await self.ledger.get(job_id)
        used = {"wall_seconds": 0.0, "max_model_calls": 0, "max_tool_calls": 0, "max_run_tokens": 0}
        for stage in job["stages"]:
            stage["report"] = result_for(stage)
            stage["verification"] = await self.verification(stage)
            stage["verified"] = self.accepted(stage, stage["report"], stage["verification"])
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
                            await self.app.finish_sandbox(stage["admission_id"])
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
            try:
                runtime_hash = await self.planned_runtime(
                    [stage["definition"] for stage in job["stages"]]
                )
            except DomainError as exc:
                await self.ledger.set_status(job_id, "blocked", str(exc))
                return
            if job["runtime_hash"] != runtime_hash:
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
                    try:
                        await self.enqueue(job, stage)
                    except DomainError as exc:
                        await self.ledger.set_status(job_id, "blocked", str(exc))
                        return
                    active += 1

    async def enqueue(self, job, stage):
        definition = stage["definition"]
        position = stage["position"]
        limits = self.app.limits.model_dump()
        count = len(job["stages"])
        for key, value in job["budget"].items():
            limits[key] = min(limits[key], value // count + int(position < value % count))
        limits = ExecutionSettings.model_validate(limits).model_dump()
        if definition.get("step"):
            await self.enqueue_configured(job, stage, limits)
            return
        prior = []
        for item in job["stages"]:
            if item["definition"]["group"] >= definition["group"]:
                continue
            report = result_for(item)
            prior.append(
                {
                    "stage": item["position"] + 1,
                    "admission_id": item["admission_id"],
                    "summary": report["summary"][: number("stage_report.handoff_summary_chars")],
                    "artifacts": [
                        value[: number("stage_report.handoff_artifact_chars")]
                        for value in report["artifacts"][: number("stage_report.handoff_artifacts")]
                    ],
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
        verification = ""
        if definition["verify"]:
            verification = (
                "Run the verification command as a separate, final tool call after all edits. "
                "When available, set bash purpose to verify. "
            )
            if command := definition.get("verification_command"):
                verification += f"Use exactly this bash command: {command}"
        if stage["admission_id"] is not None:
            self.app.schedule(session.id)
            return
        await self.app.store.admit(
            session,
            principal="operator",
            submission_id="stage",
            text=(
                f"Job objective: {job['goal']}\n"
                f"Stage: {definition['title']}\n{definition['goal']}\n"
                f"Acceptance: {definition['acceptance']}\n"
                f"Prior stage evidence: {handoff}\n"
                f"{verification}"
            ),
        )
        self.app.schedule(session.id)

    async def enqueue_configured(self, job, stage, limits):
        definition = stage["definition"]
        step = definition["step"]
        data = None
        if step["source"] is not None:
            prior = result_for(job["stages"][step["source"]])
            if not prior or not prior["artifacts"]:
                raise DomainError("workflow_input", "Previous step produced no structured output")
            data = prior["artifacts"][0]
        prompt = step["instructions"] or "Carry out the configured workflow step."
        if step["kind"] == "agent":
            prompt += (
                "\nReturn JSON with status (complete/incomplete), summary, artifacts "
                "(first item must be a JSON-encoded value for the next step), and next_step."
            )
        if stage["session_id"] is None:
            session = await self.app.create_session(
                f"job-stage:{job['id']}:{stage['position']}", prompt, tuple(step["tools"]),
                model_id=step["model_id"],
            )
            await self.ledger.attach(job["id"], stage["position"], session.id, limits)
        else:
            session = await self.app.store.get_session(stage["session_id"], "operator")
        if step["kind"] == "agent":
            text = f"Goal: {job['goal']}\nStep: {definition['title']}"
            if data is not None:
                text += f"\nUntrusted previous output: {data}"
        else:
            text = data or ""
        await self.app.store.admit(
            session, principal="operator", submission_id="stage", text=text
        )
        self.app.schedule(session.id)
