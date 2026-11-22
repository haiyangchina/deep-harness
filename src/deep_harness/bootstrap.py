"""Composition root: one owner per profile, persistent checkpoints, request-independent work."""

import asyncio
import logging
import time
from contextlib import AsyncExitStack, nullcontext
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from deep_harness.application.capabilities import (
    HARNESSES,
    READ_ONLY_HARNESSES,
    derive_grants,
)
from deep_harness.application.config import ExecutionSettings
from deep_harness.application.curation import CurationService, auto_curation_enabled
from deep_harness.application.jobs import JobService
from deep_harness.application.lifecycle import drain_tasks, mutation
from deep_harness.application.scopes import ProfileResolver
from deep_harness.application.session_runtime import SessionRuntime
from deep_harness.application.tools import TOOL_ARGUMENTS
from deep_harness.application.workspace import CODING_TOOLS, MUTATING_TOOLS, READ_TOOLS
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.archives import create_archive, restore_archive
from deep_harness.infrastructure.artifacts import ArtifactStore
from deep_harness.infrastructure.delegations import DelegationLedger
from deep_harness.infrastructure.executions import ExecutionLedger
from deep_harness.infrastructure.knowledge import KnowledgeStore, render_prompt
from deep_harness.infrastructure.leases import acquire_lease
from deep_harness.infrastructure.permissions import change_permissions
from deep_harness.infrastructure.receipts import ReceiptLedger
from deep_harness.infrastructure.reconciliation import reconcile
from deep_harness.infrastructure.store import BusinessStore
from deep_harness.infrastructure.web import WebClient
from deep_harness.infrastructure.web.settings import WebSettings
from deep_harness.runtime.deep import HarnessRuntime
from deep_harness.runtime.mcp import McpRegistry
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.runtime.standard import StandardRuntime
from deep_harness.runtime.workflow import WorkflowModel
from deep_harness.server_config import number

SHUTDOWN_GRACE_SECONDS = number("lifecycle.shutdown_grace_seconds")


logger = logging.getLogger(__name__)


class ProfileApplication:
    def __init__(
        self,
        scope,
        model,
        *,
        recover_queued=True,
        execution_limits=None,
        mcp_objects=None,
        web_settings=None,
        web_client=None,
        models=None,
        run_slots=None,
        connectors=None,
        executor=None,
        upload_limit=None,
    ):
        self.scope = scope
        self.model = model
        self.models = models
        self.run_slots = run_slots
        self.executor = executor
        self.owner = str(uuid4())
        self.store = BusinessStore(scope.home, scope.profile_id)
        self.artifacts = ArtifactStore(self.store, upload_limit) if upload_limit else None
        self.executions = ExecutionLedger(self.store)
        self.jobs = JobService(self)
        self.delegations = DelegationLedger(self.store, self.executions)
        self.receipts = ReceiptLedger(self.store, self.executions)
        self.knowledge = KnowledgeStore(self.store)
        self.curator = CurationService(self.store, model)
        self.auto_curate = False
        self.mcp = McpRegistry(scope, objects=mcp_objects, connectors=connectors)
        self.web = web_client or WebClient(web_settings or WebSettings())
        self.limits = execution_limits or ExecutionSettings()
        self.sessions = SessionRuntime(
            self.store, scope, model, self.limits, self.mcp, self.web.settings, models
        )
        self.resources = AsyncExitStack()
        self.tasks: set[asyncio.Task] = set()
        self.drains: dict[str, asyncio.Task] = {}
        self.wakeups: set[str] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.saver = None
        self.closing = False
        self.closed = False
        self.recover_queued = recover_queued
        self.control = asyncio.Lock()
        self.maintenance = False
        self.mutations: set[asyncio.Task] = set()
        self.active: dict[str, asyncio.Task] = {}

    async def start(self):
        lease = await acquire_lease(self.scope)
        self.resources.callback(lease.release)
        self.resources.push_async_callback(self.mcp.close)
        self.resources.push_async_callback(self.web.close)
        try:
            await asyncio.to_thread(self.scope.home.mkdir, parents=True, exist_ok=True)
            self.auto_curate = auto_curation_enabled(self.scope.home)
            lock = FileLock(self.scope.home / "owner.lock", thread_local=False)
            try:
                await asyncio.to_thread(lock.acquire, timeout=0)
            except Timeout as exc:
                raise DomainError(
                    "owner_exists", "Another process already owns this profile"
                ) from exc
            self.resources.callback(lock.release)
            self.resources.push_async_callback(self.store.close)
            await self.store.open()
            await self.store.bind_workspace(self.scope.workspace)
            if self.artifacts is not None:
                await self.artifacts.open()
            self.saver = await self.resources.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(self.scope.home / "checkpoints.db"))
            )
            await self.saver.setup()
            queued = await self.executions.recover()
            await self.delegations.reconcile_background()
            for session_id in queued if self.recover_queued else ():
                self.schedule(session_id)
            decided = await self.receipts.decided_waiting() if self.recover_queued else ()
            for pending in decided:
                self.schedule(pending["session_id"])
            if self.recover_queued:
                for job in await self.jobs.ledger.list(recoverable_only=True):
                    self.jobs.kick(job["id"], wake_existing=True)
        except BaseException:
            await self.resources.aclose()
            raise
        return self

    async def close(self, grace_seconds=SHUTDOWN_GRACE_SECONDS):
        async with self.control:
            if self.closed:
                return
            self.closing = True
        async with self.jobs.lock:
            await self.jobs.ledger.pause_running()
        await drain_tasks(self.tasks | self.mutations, grace_seconds)
        await drain_tasks(self.tasks, 0)
        await self.resources.aclose()
        self.closed = True

    @mutation
    async def create_session(
        self,
        request_id: str,
        prompt: str,
        tools: tuple[str, ...] | None = None,
        engine: str = "standard",
        harness: str = "general",
        permission_mode: str = "ask",
        extensions: tuple[str, ...] = (),
        mcp_tools: tuple[str, ...] = (),
        full_access_acknowledged: bool = False,
        model_id: str | None = None,
    ):
        if permission_mode not in {"ask", "auto", "full"}:
            raise DomainError("permission_mode", "Unknown permission mode")
        if permission_mode == "full" and not full_access_acknowledged:
            raise DomainError(
                "full_access_acknowledgement_required",
                "Full access requires an explicit risk acknowledgement",
            )
        if engine not in {"standard", "deep"}:
            raise DomainError("unknown_engine", "Requested engine is not supported")
        if harness not in HARNESSES:
            raise DomainError("unknown_harness", "Requested Deep harness is not supported")
        if engine != "deep" and harness != "general":
            raise DomainError("unknown_harness", "Specialized harness requires Deep engine")
        if tools is None:
            tools = derive_grants(harness, extensions, mcp_tools)
        if engine != "deep" and set(tools) & {"delegate_task", "check_delegation"}:
            raise DomainError("tool_denied", "Delegation is available only in Deep sessions")
        if harness in READ_ONLY_HARNESSES and set(tools) & MUTATING_TOOLS:
            raise DomainError("tool_denied", "Research harness cannot receive mutating tools")
        if any(name not in TOOL_ARGUMENTS and not name.startswith("mcp__") for name in tools):
            raise DomainError("unknown_tool", "Requested tool is not supported")
        knowledge = await self.knowledge.snapshot()
        frozen_knowledge = knowledge if any(knowledge.values()) else None
        frozen_tools = tuple(sorted(set(tools)))
        if self.scope.server_mode:
            denied = {"write", "edit", "apply_patch", "save_memory", "delegate_task"}
            if self.scope.server_mode != "sandbox":
                denied.add("bash")
            if set(frozen_tools) & denied or permission_mode != "ask":
                raise DomainError(
                    "tool_denied", "Tool or permission mode is unavailable on the server"
                )
        mcp_catalog = await self.sessions.catalog_for(frozen_tools)
        snapshot_fields = {
            "engine": engine,
            "harness": harness,
            "knowledge": frozen_knowledge,
            "mcp_catalog": mcp_catalog,
            "model_id": model_id,
        }
        return await self.store.create_session(
            principal="operator",
            request_id=request_id,
            prompt=render_prompt(prompt, knowledge),
            tools=frozen_tools,
            engine=engine,
            harness=harness,
            permission_mode=permission_mode,
            runtime_snapshot=self.sessions.snapshot(frozen_tools, **snapshot_fields),
        )

    @mutation
    async def submit(
        self, session_id: str, submission_id: str, text: str, *, artifact_ids=()
    ):
        if self.closing:
            raise DomainError("draining", "Application is closing")
        session = await self.store.get_session(session_id, "operator")
        if await self.jobs.ledger.owner(session_id) is not None:
            raise DomainError("job_stage_managed", "Submit long-task stages from the job page")
        await self.sessions.validate(session)
        if artifact_ids:
            if self.artifacts is None:
                raise DomainError("artifact_not_found", "Attachments are unavailable")
            text = await self.artifacts.input_with_attachments(session_id, text, artifact_ids)
        admission = await self.store.admit(
            session,
            principal="operator",
            submission_id=submission_id,
            text=text,
            artifact_ids=artifact_ids,
        )
        self.schedule(session_id)
        return admission

    def schedule(self, session_id: str):
        if self.closing:
            return
        self.wakeups.add(session_id)
        if session_id not in self.drains:
            task = asyncio.create_task(self.drain_scheduled(session_id))
            self.drains[session_id] = task
            self.track(task)

    @mutation
    async def publish_attachment(self, session_id, chunks, name):
        await self.store.get_session(session_id, "operator")
        return await self.artifacts.publish(session_id, chunks, name)

    async def drain_scheduled(self, session_id: str):
        try:
            while session_id in self.wakeups and not self.closing:
                self.wakeups.discard(session_id)
                await self.drain(session_id)
        finally:
            self.wakeups.discard(session_id)
            self.drains.pop(session_id, None)
            if not self.closing:
                await self.jobs.after_session(session_id)

    @mutation
    async def create_job(self, plan):
        return await self.jobs.create(plan)

    @mutation
    async def command_job(self, job_id, action, *, position=None, evidence=None):
        return await self.jobs.command(job_id, action, position=position, evidence=evidence)

    def track(self, task: asyncio.Task):
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self._report_task)

    @staticmethod
    def _report_task(task):
        if not task.cancelled() and task.exception() is not None:
            logger.error("Session scheduler failed", exc_info=task.exception())

    async def wait_idle(self):
        while self.tasks:
            results = await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    raise result

    async def list_delegations(self, session_id):
        await self.store.get_session(session_id, "operator")
        return await self.delegations.list(session_id)

    async def get_delegation(self, session_id, delegation_id):
        await self.store.get_session(session_id, "operator")
        return await self.delegations.get(session_id, delegation_id)

    async def launch_background(self, session, claim, call, delegation, mcp_bindings):
        kind = call.arguments["kind"]
        allowed = (
            CODING_TOOLS
            if kind == "coding"
            else {*READ_TOOLS, *(name for name in session.tools if name.startswith("mcp__"))}
        )
        tools = tuple(sorted(allowed.intersection(session.tools)))
        parent = await self.sessions.policies.get(session.id)
        snapshot = self.sessions.snapshot(
            tools, engine="deep", harness=kind, mcp_catalog=mcp_bindings,
            model_id=parent["snapshot"].get("model_id") if parent else None,
        )
        handle = await self.delegations.spawn_background(
            claim,
            delegation["id"],
            prompt=session.prompt,
            tools=tools,
            harness=kind,
            permission_mode=session.permission_mode,
            snapshot=snapshot,
            task=call.arguments["task"],
        )
        self.schedule(handle["session_id"])
        return handle

    @mutation
    async def steer_delegation(self, session_id, delegation_id, submission_id, text):
        row = await self.get_delegation(session_id, delegation_id)
        if row["mode"] != "background" or not row["child_session_id"]:
            raise DomainError("delegation_not_background", "Delegation has no background session")
        if row["stop_requested"]:
            raise DomainError("delegation_stopped", "Background delegation has been stopped")
        child = await self.store.get_session(row["child_session_id"], "operator")
        await self.sessions.validate(child)
        admission = await self.store.admit(
            child, principal="operator", submission_id=submission_id, text=text
        )
        if admission.status == "queued":
            await self.delegations.mark_steered(row["id"], admission.id)
        self.schedule(child.id)
        return admission

    @mutation
    async def stop_delegation(self, session_id, delegation_id):
        await self.store.get_session(session_id, "operator")
        row = await self.delegations.request_stop(session_id, delegation_id)
        child_id = row["child_session_id"]
        if child_id:
            status = await self.store.session_status(child_id, "operator")
            for admission in status["active_admissions"]:
                if admission.status == "started":
                    task = self.active.get(admission.id)
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                elif admission.status in {"queued", "waiting_approval"}:
                    await self.executions.cancel_pending(child_id, admission.id)
            await self.delegations.reconcile_background()
        return await self.get_delegation(session_id, delegation_id)

    def runtime(
        self,
        session,
        claim,
        mcp_bindings=None,
        *,
        stage_limits=None,
        job_id=None,
        model_id=None,
        workflow_step=None,
    ):
        runtime_type = HarnessRuntime if session.engine == "deep" else StandardRuntime
        kwargs = dict(
            session=session,
            claim=claim,
            scope=self.scope,
            model=self.sessions.select_model(model_id, session.tools),
            store=self.store,
            executions=self.executions,
            receipts=self.receipts,
            saver=self.saver,
            limits=stage_limits or self.limits,
            mcp=self.mcp,
            mcp_bindings=mcp_bindings,
            web_client=self.web,
        )
        if self.executor is not None:
            kwargs["executor"] = self.executor
        if workflow_step is not None and workflow_step["kind"] != "agent":
            kwargs["model"] = WorkflowModel(workflow_step)
        if runtime_type is HarnessRuntime and stage_limits is None:
            kwargs["background_launcher"] = self.launch_background
        if stage_limits is not None:
            kwargs["model_lock"] = self.jobs.model_lock
            kwargs["pause_probe"] = lambda: self.jobs.ledger.paused(job_id)
        if self.models is not None:
            kwargs["model_lock"] = self.models.slots[model_id or self.models.default]
        return runtime_type(**kwargs)

    async def drain(self, session_id: str):
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            session = await self.store.get_session(session_id, "operator")
            try:
                await self.sessions.validate(session)
            except DomainError as exc:
                logger.warning("Session %s paused: %s", session.id, exc.code)
                return
            while not self.closing:
                async with self.run_slots or nullcontext():
                    if self.closing:
                        return
                    status = await self.drain_next(session)
                if status != "completed":
                    return

    async def drain_next(self, session):
        paused = await self.executions.paused_job_admission(session.id)
        ready = await self.receipts.decided_waiting(session.id) if paused is None else ()
        if paused is not None:
            claim = await self.executions.resume(paused, session.id, self.owner, job_paused=True)
        elif ready:
            pending = ready[0]
            claim = await self.executions.resume(
                pending["admission_id"], session.id, self.owner,
                expected_generation=pending["generation"],
            )
        else:
            claim = await self.executions.claim(session.id, self.owner)
        if claim is not None:
            return await self.execute(session, claim, resume=bool(ready) or paused is not None)

    async def execute(self, session, claim, *, resume=False):
        self.active[claim.admission.id] = asyncio.current_task()
        clock = asyncio.get_running_loop().time
        started = clock()
        result = None
        try:
            await self.delegations.settle_background(session.id, "running", None)
            binding = await self.sessions.policies.get(session.id)
            mcp_bindings = binding["snapshot"].get("mcp_bindings") if binding else None
            stage = await self.jobs.ledger.owner(session.id)
            limits = ExecutionSettings.model_validate(stage["limits"]) if stage else self.limits
            spent = await self.executions.active_seconds(claim.admission.id)
            remaining = limits.wall_seconds - spent - (clock() - started)
            if stage is not None:
                deadline = await self.jobs.ledger.deadline(stage["job_id"])
                if deadline is not None:
                    remaining = min(remaining, deadline - time.time())
            if remaining <= 0:
                raise DomainError("budget_exhausted", "Cumulative active execution time exhausted")
            async with asyncio.timeout(remaining):
                runtime = self.runtime(
                    session,
                    claim,
                    mcp_bindings,
                    stage_limits=limits if stage else None,
                    job_id=stage["job_id"] if stage else None,
                    model_id=binding["snapshot"].get("model_id") if binding else None,
                    workflow_step=stage["definition"].get("step") if stage else None,
                )
                result = await runtime.run(resume=resume)
        except asyncio.CancelledError:
            await self.executions.settle(claim, "unknown", elapsed=clock() - started)
            await self.delegations.settle_background(session.id, "unknown", None)
            raise
        except Exception as exc:
            # Persist a useful category, never provider exception text containing credentials.
            category = exc.code if isinstance(exc, DomainError) else type(exc).__name__
            if isinstance(exc, DomainError):
                logger.warning("Execution stopped [%s]", category)
            else:
                logger.error(
                    "Execution stopped admission=%s generation=%s category=%s",
                    claim.admission.id,
                    claim.admission.generation,
                    category,
                )
            status = (
                "interrupted"
                if category
                in {
                    "budget_exhausted",
                    "context_budget_exhausted",
                    "harness_output",
                    "empty_model_response",
                    "ollama_response_error",
                    "model_connection_failed",
                    "model_request_failed",
                    "sandbox_state_lost",
                }
                else "unknown"
            )
            failure = f"Execution stopped [{category}]"
            if category in {
                "budget_exhausted",
                "ollama_response_error",
                "model_connection_failed",
                "model_request_failed",
            } and isinstance(exc, DomainError):
                failure += f": {exc}"
            if category == "unknown_effect" and isinstance(exc, DomainError):
                failure += f": {exc}"
            if category == "context_budget_exhausted":
                failure += (
                    ": Input, tool schemas or history exceed the context budget. "
                    "Use smaller reads/input or a new session; for Ollama configure "
                    "--context-window (model.context_window_tokens in TOML) within "
                    "the model and memory limits."
                )
            if category == "empty_model_response":
                failure += (
                    ": The model produced tokens but no readable answer. Thinking/"
                    "reasoning output is not supported by this adapter; set "
                    "[model] reasoning = false (the default) or use a non-thinking "
                    "model, then retry in a new session."
                )
            await self.executions.settle(claim, status, failure, elapsed=clock() - started)
            await self.delegations.settle_background(session.id, status, failure)
            return status
        finally:
            self.active.pop(claim.admission.id, None)
            if result is None or result["status"] not in {"waiting_approval", "job_paused"}:
                await self.finish_sandbox(claim.admission.id)
        await self.executions.settle(
            claim,
            result["status"],
            result["result"],
            interactions=result.get("interactions", ()),
            elapsed=clock() - started,
        )
        await self.delegations.settle_background(session.id, result["status"], result["result"])
        if self.artifacts is not None and result["status"] == "completed" and result["result"]:
            try:
                await self.artifacts.result(claim.admission, result["result"])
            except (DomainError, OSError):
                logger.warning("Result artifact was not published for %s", claim.admission.id)
        if result["status"] == "completed" and self.auto_curate:
            self.track(asyncio.create_task(self._curate_completed(session.id)))
        return result["status"]

    async def finish_sandbox(self, run_id):
        if self.executor is not None:
            try:
                await self.executor.finish(run_id)
            except (OSError, TimeoutError):
                logger.warning("Could not confirm sandbox cleanup for %s", run_id)

    async def _curate_completed(self, session_id):
        try:
            await self.curator.propose(session_id)
        # Isolate optional curator failures; provider exception text may contain secrets.
        except Exception as exc:  # health: allow BLE001 -- log only the exception class
            logger.warning("Automatic curation for %s failed: %s", session_id, type(exc).__name__)

    @mutation
    async def approve(
        self,
        session_id: str,
        interaction_id: str,
        generation: int,
        decision: str,
    ):
        if decision not in {"approve", "deny"}:
            raise DomainError("invalid_decision", "Decision must be approve or deny")
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            session = await self.store.get_session(session_id, "operator")
            await self.sessions.validate(session)
            admission_id = await self.receipts.decide(
                interaction_id,
                session_id,
                generation,
                decision,
            )
            self.schedule(session_id)
        return admission_id

    @mutation
    async def set_permissions(
        self, session_id, mode, expected_revision, full_access_acknowledged=False
    ):
        if self.scope.server_mode and mode != "ask":
            raise DomainError("permission_mode", "The server uses explicit approval")
        if await self.jobs.ledger.owner(session_id) is not None:
            raise DomainError(
                "job_stage_managed", "Permissions are fixed for job stages"
            )
        status = await self.store.session_status(session_id, "operator")
        if status["active_admissions"]:
            raise DomainError("permissions_busy", "Finish pending work before changing permissions")
        if mode == "full" and not full_access_acknowledged:
            raise DomainError(
                "full_access_acknowledgement_required",
                "Full access requires an explicit risk acknowledgement",
            )
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            return await change_permissions(self.store, session_id, mode, expected_revision)

    @mutation
    async def revoke_reuse(self, session_id, admission_id):
        await self.store.get_session(session_id, "operator")
        admission = await self.store.get_admission(admission_id, session_id)
        await self.receipts.revoke_reuse(session_id, admission.id)
        return await self.sessions.status(session_id)

    @mutation
    async def fork_session(self, session_id, request_id):
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            session = await self.store.get_session(session_id, "operator")
            bound = await self.sessions.policies.get(session_id)
            knowledge = bound["snapshot"].get("knowledge") if bound else None
            catalog = await self.sessions.catalog_for(session.tools)
            return await self.sessions.branches.fork(
                session,
                request_id,
                self.sessions.snapshot(
                    session.tools,
                    engine=session.engine,
                    harness=session.harness,
                    knowledge=knowledge,
                    mcp_catalog=catalog,
                    model_id=bound["snapshot"].get("model_id") if bound else None,
                ),
            )

    @mutation
    async def cancel(self, session_id, admission_id):
        await self.store.get_session(session_id, "operator")
        admission = await self.store.get_admission(admission_id, session_id)
        task = self.active.get(admission_id)
        if admission.status == "started" and task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=number("lifecycle.cancel_wait_seconds"))
            return await self.store.get_admission(admission_id, session_id)
        result = await self.executions.cancel_pending(session_id, admission_id)
        await self.finish_sandbox(admission_id)
        if result.status == "cancelled":
            self.schedule(session_id)
        return result

    @mutation
    async def backup(self, destination):
        async with self.control:
            if self.maintenance:
                raise DomainError("maintenance", "A profile backup is in progress")
            self.maintenance = True
            others = self.mutations - {asyncio.current_task()}
        try:
            if others:
                _, pending = await asyncio.wait(
                    others, timeout=number("lifecycle.backup_mutation_drain_seconds")
                )
                if pending:
                    raise DomainError(
                        "maintenance_busy", "Profile mutations did not finish in time"
                    )
            clock = asyncio.get_running_loop().time
            deadline = clock() + number("lifecycle.backup_execution_drain_seconds")
            while running := {task for task in self.tasks if not task.done()}:
                _, pending = await asyncio.wait(running, timeout=max(0, deadline - clock()))
                if pending:
                    raise DomainError("maintenance_busy", "Profile execution did not drain in time")
            archive = asyncio.create_task(
                asyncio.to_thread(create_archive, self.scope, destination)
            )
            try:
                return await asyncio.shield(archive)
            except asyncio.CancelledError:
                await archive
                raise
        finally:
            self.maintenance = False

    @mutation
    async def reconcile(self, session_id, admission_id, generation, evidence, effects):
        await self.store.get_session(session_id, "operator")
        await reconcile(self.store, session_id, admission_id, generation, evidence, effects)
        return await self.sessions.status(session_id)

    @mutation
    async def install_skill(self, document):
        return await self.knowledge.add_skill(document)

    @mutation
    async def disable_skill(self, name):
        await self.knowledge.disable_skill(name)

    @mutation
    async def add_memory(self, content, source="operator"):
        return await self.knowledge.remember(content, source)

    @mutation
    async def forget_memory(self, memory_id):
        await self.knowledge.forget(memory_id)

    @mutation
    async def curate(self, session_id):
        await self.store.get_session(session_id, "operator")
        return await self.curator.propose(session_id)

    @mutation
    async def decide_memory_candidate(self, candidate_id, decision):
        return await self.curator.ledger.decide(candidate_id, decision)


class ProjectHost:
    def __init__(
        self,
        state_dir: Path,
        workspace: Path,
        model_factory=None,
        *,
        recover_queued=True,
        execution_limits=None,
        mcp_objects=None,
        web_settings=None,
        web_clients=None,
        models=None,
        run_slots=None,
        server_mode=None,
        protected_paths=(),
        connectors=None,
        executor=None,
        upload_limit=None,
    ):
        self.profiles = ProfileResolver(state_dir, workspace)
        self.model_factory = model_factory or (lambda scope: DiagnosticModel())
        self.recover_queued = recover_queued
        self.execution_limits = execution_limits
        self.mcp_objects = mcp_objects or {}
        self.web_settings = web_settings
        self.web_clients = web_clients or {}
        self.models = models
        self.run_slots = run_slots
        self.server_mode = server_mode
        self.protected_paths = protected_paths
        self.connectors = connectors
        self.executor = executor
        self.upload_limit = upload_limit
        self.apps: dict[str, ProfileApplication] = {}
        self.lock = asyncio.Lock()

    async def application(self, profile_id: str) -> ProfileApplication:
        async with self.lock:
            if profile_id not in self.apps:
                scope = replace(
                    self.profiles.resolve(profile_id),
                    server_mode=self.server_mode, protected_paths=self.protected_paths,
                    executor_revision=fingerprint({
                        "image": self.executor.image_id,
                        "settings": self.executor.settings.model_dump(),
                    }) if self.executor is not None else None,
                )
                self.apps[profile_id] = await ProfileApplication(
                    scope,
                    self.model_factory(scope),
                    recover_queued=self.recover_queued,
                    execution_limits=self.execution_limits,
                    mcp_objects=self.mcp_objects.get(profile_id),
                    web_settings=self.web_settings,
                    web_client=self.web_clients.get(profile_id),
                    models=self.models,
                    run_slots=self.run_slots,
                    connectors=self.connectors,
                    executor=self.executor,
                    upload_limit=self.upload_limit,
                ).start()
            return self.apps[profile_id]

    async def close(self, grace_seconds=SHUTDOWN_GRACE_SECONDS):
        for app in self.apps.values():
            await app.close(grace_seconds=grace_seconds)
        self.apps.clear()

    async def restore(self, profile_id, source):
        scope = self.profiles.resolve(profile_id)
        lease = await acquire_lease(scope)
        try:
            return await asyncio.to_thread(restore_archive, scope, source)
        finally:
            lease.release()
