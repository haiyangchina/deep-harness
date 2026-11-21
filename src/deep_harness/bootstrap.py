"""Composition root: one owner per profile, persistent checkpoints, request-independent work."""

import asyncio
import logging
from contextlib import AsyncExitStack
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
from deep_harness.application.lifecycle import drain_tasks, mutation
from deep_harness.application.scopes import ProfileResolver
from deep_harness.application.session_runtime import SessionRuntime
from deep_harness.application.tools import TOOL_ARGUMENTS
from deep_harness.application.workspace import CODING_TOOLS, MUTATING_TOOLS, READ_TOOLS
from deep_harness.domain.models import DomainError, ExecutionClaim
from deep_harness.infrastructure.archives import create_archive, restore_archive
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
from deep_harness.runtime.approval_checkpoint import approval_ids, checkpoint_interrupts
from deep_harness.runtime.deep import DeepRuntime
from deep_harness.runtime.mcp import McpRegistry
from deep_harness.runtime.models import DiagnosticModel
from deep_harness.runtime.standard import StandardRuntime

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
    ):
        self.scope = scope
        self.model = model
        self.owner = str(uuid4())
        self.store = BusinessStore(scope.home, scope.profile_id)
        self.executions = ExecutionLedger(self.store)
        self.delegations = DelegationLedger(self.store, self.executions)
        self.receipts = ReceiptLedger(self.store, self.executions)
        self.knowledge = KnowledgeStore(self.store)
        self.curator = CurationService(self.store, model)
        self.auto_curate = False
        self.mcp = McpRegistry(scope, objects=mcp_objects)
        self.web = web_client or WebClient(web_settings or WebSettings())
        self.limits = execution_limits or ExecutionSettings()
        self.sessions = SessionRuntime(
            self.store, scope, model, self.limits, self.mcp, self.web.settings
        )
        self.resources = AsyncExitStack()
        self.tasks: set[asyncio.Task] = set()
        self.drains: dict[str, asyncio.Task] = {}
        self.wakeups: set[str] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.saver = None
        self.closing = False
        self.recover_queued = recover_queued
        self.control = asyncio.Lock()
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
            self.saver = await self.resources.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(self.scope.home / "checkpoints.db"))
            )
            await self.saver.setup()
            queued = await self.executions.recover()
            await self.delegations.reconcile_background()
            if self.recover_queued:
                await self.restore_waits()
            for session_id in queued if self.recover_queued else ():
                self.schedule(session_id)
            decided = await self.receipts.decided_waiting() if self.recover_queued else ()
            for pending in decided:
                session = await self.store.get_session(pending["session_id"], "operator")
                await self.resume_decided(session, pending["admission_id"], pending["generation"])
        except BaseException:
            await self.resources.aclose()
            raise
        return self

    async def restore_waits(self):
        for admission in await self.executions.unbound_waits():
            session = await self.store.get_session(admission.session_id, "operator")
            try:
                await self.sessions.validate(session)
                binding = await self.sessions.policies.get(session.id)
                runtime = self.runtime(
                    session,
                    ExecutionClaim(admission, self.owner),
                    binding["snapshot"].get("mcp_bindings"),
                )
                state = await runtime.graph.aget_state(runtime.config)
                ids = approval_ids(checkpoint_interrupts(state), admission.generation)
                await self.executions.bind_wait(admission, ids)
                if not ids:
                    logger.warning("No verified approval checkpoint for %s", admission.id)
            except Exception as exc:
                logger.warning(
                    "Approval checkpoint for %s unavailable [%s]", admission.id, type(exc).__name__
                )

    async def close(self, grace_seconds=5):
        async with self.control:
            self.closing = True
        await drain_tasks(self.tasks, grace_seconds)
        await self.resources.aclose()

    @mutation
    async def create_session(
        self,
        request_id: str,
        prompt: str,
        tools: tuple[str, ...] | None = None,
        engine: str = "standard",
        harness: str = "general",
        permission_mode: str = "legacy",
        extensions: tuple[str, ...] = (),
        mcp_tools: tuple[str, ...] = (),
        full_access_acknowledged: bool = False,
    ):
        if permission_mode not in {"legacy", "ask", "auto", "full"}:
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
            tools = derive_grants(engine, harness, extensions, mcp_tools)
        if engine != "deep" and set(tools) & {"delegate_task", "check_delegation"}:
            raise DomainError("tool_denied", "Delegation is available only in Deep sessions")
        if harness in READ_ONLY_HARNESSES and set(tools) & MUTATING_TOOLS:
            raise DomainError("tool_denied", "Research harness cannot receive mutating tools")
        if any(name not in TOOL_ARGUMENTS and not name.startswith("mcp__") for name in tools):
            raise DomainError("unknown_tool", "Requested tool is not supported")
        knowledge = await self.knowledge.snapshot()
        frozen_knowledge = knowledge if any(knowledge.values()) else None
        frozen_tools = tuple(sorted(set(tools)))
        mcp_catalog = await self.sessions.catalog_for(frozen_tools)
        return await self.store.create_session(
            principal="operator",
            request_id=request_id,
            prompt=render_prompt(prompt, knowledge),
            tools=frozen_tools,
            engine=engine,
            harness=harness,
            permission_mode=permission_mode,
            runtime_snapshot=self.sessions.snapshot(
                frozen_tools,
                engine=engine,
                harness=harness,
                knowledge=frozen_knowledge,
                mcp_catalog=mcp_catalog,
            ),
        )

    @mutation
    async def submit(self, session_id: str, submission_id: str, text: str):
        if self.closing:
            raise DomainError("draining", "Application is closing")
        session = await self.store.get_session(session_id, "operator")
        await self.sessions.validate(session)
        admission = await self.store.admit(
            session,
            principal="operator",
            submission_id=submission_id,
            text=text,
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

    async def drain_scheduled(self, session_id: str):
        try:
            while session_id in self.wakeups and not self.closing:
                self.wakeups.discard(session_id)
                await self.drain(session_id)
        finally:
            self.wakeups.discard(session_id)
            self.drains.pop(session_id, None)

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
        snapshot = self.sessions.snapshot(
            tools, engine="deep", harness=kind, mcp_catalog=mcp_bindings
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

    def runtime(self, session, claim, mcp_bindings=None):
        runtime_type = DeepRuntime if session.engine == "deep" else StandardRuntime
        kwargs = dict(
            session=session,
            claim=claim,
            scope=self.scope,
            model=self.model,
            store=self.store,
            executions=self.executions,
            receipts=self.receipts,
            saver=self.saver,
            limits=self.limits,
            mcp=self.mcp,
            mcp_bindings=mcp_bindings,
            web_client=self.web,
        )
        if runtime_type is DeepRuntime:
            kwargs["background_launcher"] = self.launch_background
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
                claim = await self.executions.claim(session_id, self.owner)
                if claim is None:
                    return
                status = await self.execute(session, claim)
                if status != "completed":
                    return

    async def execute(self, session, claim, *, resume=False):
        self.active[claim.admission.id] = asyncio.current_task()
        try:
            await self.delegations.settle_background(session.id, "running", None)
            binding = await self.sessions.policies.get(session.id)
            mcp_bindings = binding["snapshot"].get("mcp_bindings") if binding else None
            async with asyncio.timeout(self.limits.wall_seconds):
                result = await self.runtime(session, claim, mcp_bindings).run(resume=resume)
        except asyncio.CancelledError:
            await self.executions.settle(claim, "unknown")
            await self.delegations.settle_background(session.id, "unknown", None)
            raise
        except Exception as exc:
            # Persist a useful category, never provider exception text containing credentials.
            category = exc.code if isinstance(exc, DomainError) else type(exc).__name__
            if isinstance(exc, DomainError):
                logger.warning("Execution stopped [%s]", category)
            else:
                logger.exception("Execution stopped; preserving uncertain outcomes")
            status = (
                "interrupted"
                if category
                in {
                    "budget_exhausted",
                    "context_budget_exhausted",
                    "harness_output",
                    "empty_model_response",
                }
                else "unknown"
            )
            failure = f"Execution stopped [{category}]"
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
            await self.executions.settle(claim, status, failure)
            await self.delegations.settle_background(session.id, status, failure)
            return status
        finally:
            self.active.pop(claim.admission.id, None)
        await self.executions.settle(
            claim, result["status"], result["result"], interactions=result.get("interactions", ())
        )
        await self.delegations.settle_background(session.id, result["status"], result["result"])
        if result["status"] == "completed" and self.auto_curate:
            self.track(asyncio.create_task(self._curate_completed(session.id)))
        return result["status"]

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
            claim = await self.executions.resume(
                admission_id, session_id, self.owner, expected_generation=generation
            )
            if claim is not None:
                self.track(asyncio.create_task(self._resume(session, claim)))
        return admission_id

    async def resume_decided(self, session, admission_id, generation):
        try:
            await self.sessions.validate(session)
        except DomainError as exc:
            logger.warning("Approval recovery for %s paused: %s", session.id, exc.code)
            return
        claim = await self.executions.resume(
            admission_id, session.id, self.owner, expected_generation=generation
        )
        if claim is not None:
            self.track(asyncio.create_task(self._resume(session, claim)))

    @mutation
    async def bind_runtime(self, session_id, expected_fingerprint):
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            session = await self.store.get_session(session_id, "operator")
            bound = await self.sessions.policies.get(session_id)
            knowledge = bound["snapshot"].get("knowledge") if bound else None
            catalog = await self.sessions.catalog_for(session.tools)
            await self.sessions.policies.adopt(
                session_id,
                self.sessions.snapshot(
                    session.tools,
                    engine=session.engine,
                    harness=session.harness,
                    knowledge=knowledge,
                    mcp_catalog=catalog,
                    version=bound["snapshot"].get("version", 1) if bound else 2,
                ),
                expected_fingerprint,
            )
            await self.restore_waits()
            for pending in await self.receipts.decided_waiting():
                if pending["session_id"] == session_id:
                    await self.resume_decided(
                        session, pending["admission_id"], pending["generation"]
                    )
        self.schedule(session_id)
        return await self.sessions.status(session_id)

    @mutation
    async def set_permissions(
        self, session_id, mode, expected_revision, full_access_acknowledged=False
    ):
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
                ),
            )

    @mutation
    async def cancel(self, session_id, admission_id):
        await self.store.get_session(session_id, "operator")
        admission = await self.store.get_admission(admission_id, session_id)
        task = self.active.get(admission_id)
        if admission.status == "started" and task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return await self.store.get_admission(admission_id, session_id)
        result = await self.executions.cancel_pending(session_id, admission_id)
        if result.status == "cancelled":
            self.schedule(session_id)
        return result

    @mutation
    async def backup(self, destination):
        await self.wait_idle()
        return await asyncio.to_thread(create_archive, self.scope, destination)

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

    async def _resume(self, session, claim):
        async with self.locks.setdefault(session.id, asyncio.Lock()):
            status = await self.execute(session, claim, resume=True)
        if status == "completed":
            self.schedule(session.id)


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
    ):
        self.profiles = ProfileResolver(state_dir, workspace)
        self.model_factory = model_factory or (lambda scope: DiagnosticModel())
        self.recover_queued = recover_queued
        self.execution_limits = execution_limits
        self.mcp_objects = mcp_objects or {}
        self.web_settings = web_settings
        self.web_clients = web_clients or {}
        self.apps: dict[str, ProfileApplication] = {}
        self.lock = asyncio.Lock()

    async def application(self, profile_id: str) -> ProfileApplication:
        async with self.lock:
            if profile_id not in self.apps:
                scope = self.profiles.resolve(profile_id)
                self.apps[profile_id] = await ProfileApplication(
                    scope,
                    self.model_factory(scope),
                    recover_queued=self.recover_queued,
                    execution_limits=self.execution_limits,
                    mcp_objects=self.mcp_objects.get(profile_id),
                    web_settings=self.web_settings,
                    web_client=self.web_clients.get(profile_id),
                ).start()
            return self.apps[profile_id]

    async def close(self, grace_seconds=5):
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
