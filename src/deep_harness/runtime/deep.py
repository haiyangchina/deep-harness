"""Deep Agents graph guarded by Harness admission, approval and receipt ledgers."""

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware import FilesystemMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import ValidationError
from sqlalchemy import select

from deep_harness.application.coding_evidence import coding_evidence
from deep_harness.application.harness_spec import spec_for
from deep_harness.application.tools import ToolGateway, tool_definitions
from deep_harness.application.workspace import CODING_RESEARCH_TOOLS, CODING_TOOLS, READ_TOOLS
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, ToolCall
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.branches import BranchLedger
from deep_harness.infrastructure.coding_workspace import CodingWorkspace
from deep_harness.infrastructure.delegations import DelegationLedger
from deep_harness.infrastructure.delivery import TaskDelivery
from deep_harness.infrastructure.task_workspaces import TaskWorkspaces, finish_io
from deep_harness.runtime.agent import (
    filesystem_middleware,
    register_profile,
    specialist_guide,
    system_prompt_for,
)
from deep_harness.runtime.approval_checkpoint import (
    approval_ids,
    checkpoint_interrupts,
    interrupt_outcome,
    pause_at_boundary,
    resume_command,
)
from deep_harness.runtime.coding_verification import verify_coding_task
from deep_harness.runtime.guard import HarnessGuardMiddleware
from deep_harness.runtime.harnesses import HarnessReport, parse_report
from deep_harness.runtime.images import user_message
from deep_harness.runtime.inference import normalized_usage, usage_totals
from deep_harness.runtime.models import LangChainModel, to_langchain
from deep_harness.server_config import number


class HarnessRuntime:
    def __init__(
        self,
        *,
        session,
        claim,
        scope,
        model,
        store,
        executions,
        receipts,
        saver,
        limits,
        mcp=None,
        mcp_bindings=None,
        web_client=None,
        record_messages=True,
        delegation_id=None,
        model_lock=None,
        pause_probe=None,
        tool_lock=None,
        writer_lock=None,
        background_launcher=None,
        executor=None,
        artifacts=None,
        coding_workspace=None,
        verification_command=None,
    ):
        if not isinstance(model, LangChainModel):
            raise DomainError("deep_model", "Deep engine requires a LangChain chat model")
        self.session = session
        self.claim = claim
        self.store = store
        self.artifacts = artifacts
        self.executions = executions
        self.receipts = receipts
        self.limits = limits
        self.request_timeout = model.request_timeout
        self.record_messages = record_messages
        self.delegation_id = delegation_id
        self.model_lock = model_lock or asyncio.Lock()
        self.pause_probe = pause_probe
        self.tool_lock = tool_lock or asyncio.Lock()
        self.writer_lock = writer_lock or asyncio.Lock()
        self.background_launcher = background_launcher
        self.scope = scope
        self.model = model
        self.saver = saver
        self.mcp = mcp
        self.coding_workspace = coding_workspace
        self.verification_command = verification_command
        if self.coding_workspace is None and session.harness == "coding" and (
            not scope.server_mode or scope.task_workspace
        ):
            self.coding_workspace = CodingWorkspace(scope, claim.admission.id)
        self.tool_specs = tool_definitions(
            session.tools, mcp_bindings
        )
        self.gateway = ToolGateway(
            scope,
            session,
            receipts,
            claim,
            mcp=mcp,
            mcp_bindings=mcp_bindings,
            web_client=web_client,
            executor=executor,
            coding_workspace=self.coding_workspace,
        )
        self.delegations = DelegationLedger(store, executions)
        params = model.model._get_ls_params()
        provider, model_name = params.get("ls_provider"), params.get("ls_model_name")
        if not provider or not model_name:
            raise DomainError("deep_model", "Model provider identity is required")
        register_profile(provider, model_name)
        backend = StateBackend()
        prompt = system_prompt_for(session.prompt, session.harness)
        if verification_command:
            prompt += (
                "\nThe operator selected this final verification command. Run it from the "
                "workspace root after edits with purpose=verify: "
                + json.dumps(verification_command, ensure_ascii=False)
            )
        if "project_instructions" in session.tools:
            prompt += (
                "\nUse project_instructions for affected paths before editing. "
                "apply_patch accepts a single file or a batch. To delete a UTF-8 file, "
                "supply its hash and a full deletion patch with +++ /dev/null. "
                "Run a focused, separate bash command with purpose=verify after all "
                "edits. Do not report verified completion without that evidence."
            )
        if session.harness == "coding" and "delegate_task" in session.tools:
            prompt += (
                "\nUse synchronous read-only research specialists for investigation and review "
                "when their findings are needed in this turn. Use a synchronous coding specialist "
                "for a bounded implementation with concrete acceptance criteria. Use kind=review "
                "for an independent read-only review of the resulting diff. Finish "
                "implementation before assigning review or running acceptance tests. "
                "For independent implementations in an isolated task, set isolate=true and "
                "merge their registered patches using merge_delegations when granted. "
                "Use background only for independent research that may finish after this turn."
            )
        self.graph = create_deep_agent(
            model=model.model,
            tools=[self._tool(spec) for spec in self.tool_specs],
            system_prompt=prompt,
            backend=backend,
            middleware=[
                filesystem_middleware(backend),
                HarnessGuardMiddleware(self),
            ],
            subagents=[],
            checkpointer=saver,
            name="deep-harness-unified",
        )
        self.config = {
            "configurable": {"thread_id": session.id},
            "recursion_limit": 2 * limits.max_model_calls + 4,
        }

    @staticmethod
    def message_from_receipt(saved):
        return AIMessage(
            id=saved["id"],
            content=saved["content"],
            tool_calls=saved.get("tool_calls", []),
            additional_kwargs=saved.get("provider_extensions", {}),
            response_metadata=saved.get("response_metadata", {}),
        )

    def _tool(self, spec):
        original = spec["name"]
        name = original

        async def invoke(runtime: ToolRuntime, **arguments):
            await pause_at_boundary(self.pause_probe, self.claim.admission.generation)
            if original == "delegate_task" and arguments.get("share_images") is None:
                arguments.pop("share_images", None)
            if original == "delegate_task" and arguments.get("isolate") is None:
                arguments.pop("isolate", None)
            raw_id = (
                runtime.tool_call_id or fingerprint({"name": original, "arguments": arguments})[:32]
            )
            call = ToolCall(
                f"{self.delegation_id}:{raw_id}" if self.delegation_id else raw_id,
                original,
                arguments,
            )
            try:
                await self.executions.reserve_tools(
                    self.claim, (call.id,), self.limits.max_tool_calls
                )
                self.gateway.validate(call)
                existing = await self._tool_message(call)
                if existing is not None:
                    return existing
                if original == "delegate_task":
                    isolated = bool(arguments.get("isolate"))
                    if isolated and (
                        not self.scope.task_workspace or arguments["kind"] != "coding"
                        or arguments.get("background")
                    ):
                        raise DomainError(
                            "delegation_mode", "Isolated implementation requires a managed task"
                        )
                    if arguments["kind"] == "review" and self.session.harness != "coding":
                        raise DomainError("delegation_kind", "Review requires a coding task")
                    if self.session.harness != "general" and arguments["kind"] not in spec_for(
                        self.session.harness
                    ).subagents:
                        raise DomainError("delegation_kind", "Specialist is outside this harness")
                    if self.session.harness == "coding" and arguments.get("background") and (
                        arguments["kind"] in {"coding", "review"}
                    ):
                        raise DomainError(
                            "delegation_mode",
                            "Use synchronous specialists for implementation and review.",
                        )
                    async with self.tool_lock:
                        delegation = await self.delegations.begin(self.claim, call)
                    if self.coding_workspace is not None and not isolated and (
                        arguments["kind"] in {"coding", "review"}
                    ):
                        async with self.writer_lock:
                            result = await self._delegate(call, delegation)
                    else:
                        result = await self._delegate(call, delegation)
                elif original == "check_delegation":
                    row = await self.delegations.get(self.session.id, arguments["delegation_id"])
                    result = json.dumps(
                        {
                            key: row[key]
                            for key in (
                                "id",
                                "kind",
                                "mode",
                                "status",
                                "result",
                                "child_session_id",
                                "child_admission_id",
                                "stop_requested",
                            )
                        },
                        ensure_ascii=False,
                    )
                else:
                    writer = (
                        self.coding_workspace is not None
                        and self.delegation_id is None
                        and original in {
                            "write", "edit", "apply_patch", "bash", "merge_delegations",
                        }
                    )
                    async with (
                        self.writer_lock if writer else nullcontext(),
                        self.tool_lock,
                    ):
                        operation = await self._merge_call(call) if original == (
                            "merge_delegations"
                        ) else call
                        required = self.gateway.approval_reason(operation) is not None
                        if required and not await self._approval(operation):
                            result = "Tool denied by user."
                        else:
                            try:
                                result = await self.gateway.execute(operation)
                            except DomainError as exc:
                                if exc.code != "approval_missing":
                                    raise
                                result = (
                                    await self.gateway.execute(operation)
                                    if await self._approval(operation)
                                    else "Tool denied by user."
                                )
            except (ValidationError, OSError, UnicodeError) as exc:
                result = f"Tool error: {type(exc).__name__}: {exc}"
            except DomainError as exc:
                if exc.code in {
                    "unknown_effect",
                    "lease_lost",
                    "sandbox_state_lost",
                    "effect_conflict",
                    "budget_exhausted",
                }:
                    raise
                result = f"Tool refused [{exc.code}]: {exc}"
            if self.delegation_id:
                await self.delegations.save_tool(self.claim, self.delegation_id, call, result)
            else:
                async with self.store.transaction() as conn:
                    await self.executions.assert_claim(conn, self.claim)
                    await self.store.append_message(
                        conn,
                        self.claim,
                        {
                            "id": f"{self.claim.admission.id}:tool:{call.id}",
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": result,
                        },
                    )
            return result

        return StructuredTool.from_function(
            coroutine=invoke,
            name=name,
            description=spec["description"],
            args_schema=spec["parameters"],
        )

    async def _merge_call(self, call):
        if self.session.harness != "coding" or not self.scope.task_workspace:
            raise DomainError("delegation_mode", "Merging requires an isolated coding task")
        manager = TaskWorkspaces(self.scope)
        files = []
        for identifier in call.arguments["delegation_ids"]:
            row = await self.delegations.get(self.session.id, identifier)
            if (
                row["kind"] != "coding" or row["status"] != "completed"
                or not (manager.home(identifier) / "workspace.json").is_file()
            ):
                raise DomainError("delegation_conflict", "No isolated implementation is available")
            try:
                proposal = json.loads(row["result"])["apply_patch"]["files"]
            except (ValueError, TypeError, KeyError) as exc:
                raise DomainError(
                    "delegation_conflict", "The child has no mergeable patch"
                ) from exc
            files.extend(proposal)
        operation = ToolCall(call.id, "apply_patch", {"files": files})
        self.gateway.validate(operation)
        return operation

    async def _tool_message(self, call):
        if self.delegation_id:
            return await self.delegations.tool_result(self.delegation_id, call)
        async with self.store.engine.connect() as conn:
            row = await conn.execute(
                select(s.messages.c.payload).where(
                    s.messages.c.id == f"{self.claim.admission.id}:tool:{call.id}"
                )
            )
            payload = row.scalar_one_or_none()
            return payload["content"] if payload is not None else None

    async def _delegate(self, call, delegation):
        image_ids = (
            await self.store.admission_image_ids(self.claim.admission)
            if call.arguments.get("share_images") else []
        )
        if call.arguments.get("background"):
            if self.background_launcher is None:
                raise DomainError("delegation_unavailable", "Background launcher is unavailable")
            handle = await self.background_launcher(
                self.session, self.claim, call, delegation, self.gateway.mcp_bindings, image_ids
            )
            return json.dumps(handle, ensure_ascii=False)
        if delegation["status"] == "completed":
            return delegation["result"]
        kind = call.arguments["kind"]
        isolated = None
        child_scope = self.scope
        if call.arguments.get("isolate"):
            manager = TaskWorkspaces(self.scope)
            async with self.writer_lock:
                task = await finish_io(manager.open, delegation["id"], "copy")
            isolated = TaskDelivery(manager, task)
            child_scope = manager.scope_for(task)
        guide = specialist_guide(kind)
        if kind == "coding":
            allowed = CODING_TOOLS
        elif self.session.harness == "coding":
            allowed = CODING_RESEARCH_TOOLS
        else:
            allowed = {
                *READ_TOOLS, *(name for name in self.session.tools if name.startswith("mcp__"))
            }
        child_tools = tuple(sorted(allowed.intersection(self.session.tools)))
        if isolated:
            child_tools = tuple(name for name in child_tools if name != "bash")
        child_session = replace(
            self.session,
            prompt=f"{self.session.prompt}\n\n{guide}",
            tools=child_tools,
            harness="general",
        )
        child = HarnessRuntime(
            session=child_session,
            claim=self.claim,
            scope=child_scope,
            model=self.model,
            store=self.store,
            executions=self.executions,
            receipts=self.receipts,
            saver=self.saver,
            limits=self.limits,
            mcp=self.mcp,
            mcp_bindings=self.gateway.mcp_bindings,
            web_client=self.gateway.web_client,
            record_messages=False,
            delegation_id=delegation["id"],
            model_lock=self.model_lock,
            pause_probe=self.pause_probe,
            executor=self.gateway.executor,
            tool_lock=None if isolated else self.tool_lock,
            writer_lock=self.writer_lock,
            artifacts=self.artifacts,
            coding_workspace=None if isolated else self.coding_workspace,
            verification_command=self.verification_command,
        )
        child.config["configurable"]["thread_id"] = (
            f"{self.session.id}:delegate:{self.claim.admission.id}:{call.id}"
        )
        checkpoint = await child.graph.aget_state(child.config)
        review = None
        if kind == "review" and self.coding_workspace is not None:
            review = await asyncio.to_thread(
                self.coding_workspace.review_snapshot, delegation["id"]
            )
        if checkpoint.values and not checkpoint.next:
            result = checkpoint.values["messages"][-1].content
            result = await self.specialist_result(review, isolated, result)
            await self.delegations.finish(self.claim, delegation["id"], result)
            return result
        task_text = call.arguments["task"]
        if review is not None and not checkpoint.values:
            task_text += "\nObserved working-tree diff for review:\n" + review["diff"]
        value = (
            (resume_command(checkpoint) if checkpoint_interrupts(checkpoint) else None)
            if checkpoint.next
            else {"messages": [to_langchain({
                "id": f"{call.id}:task",
                "role": "user",
                "content": task_text,
                "image_ids": image_ids,
            })]}
        )
        result = await child.graph.ainvoke(
            value,
            config=child.config,
            durability="sync",
        )
        if pending := result.get("__interrupt__"):
            approvals = [item for item in pending if not item.value.get("job_pause")]
            if approvals:
                interrupt({
                    "generation": self.claim.admission.generation,
                    "interaction_ids": approval_ids(approvals, self.claim.admission.generation),
                })
            else:
                interrupt({"generation": self.claim.admission.generation, "job_pause": True})
        answer = await self.specialist_result(review, isolated, result["messages"][-1].content)
        await self.delegations.finish(self.claim, delegation["id"], answer)
        return answer

    async def specialist_result(self, review, isolated, answer):
        if isolated is not None:
            proposal = await asyncio.to_thread(isolated.merge_proposal)
            return json.dumps(
                {
                    "delegation_id": isolated.task.task_id, "summary": answer,
                    "apply_patch": {"files": proposal} if proposal else None,
                },
                ensure_ascii=False,
            )
        if review is None:
            return answer
        current = await asyncio.to_thread(self.coding_workspace.revision)
        revision = review["revision"]
        status = "unavailable" if not revision or not current else (
            "current" if revision == current else "stale"
        )
        return json.dumps(
            {"status": status, "revision": revision, "findings": answer}, ensure_ascii=False
        )

    async def _approval(self, call):
        key = f"{self.claim.admission.id}:{call.id}"
        digest = fingerprint({"name": call.name, "arguments": call.arguments})
        scope_key, _, can_reuse = self.gateway.approval_scope(call)
        if can_reuse and await self.receipts.scope_granted(
            self.claim, scope_key, self.session.permission_revision, exclude_key=key
        ):
            return True
        proposal = {"tool_name": call.name, "arguments": call.arguments}
        proposal.update(
            reason=self.gateway.approval_reason(call),
            permission_mode=self.session.permission_mode,
            permission_revision=self.session.permission_revision,
            scope_key=scope_key,
            reusable=can_reuse,
        )
        decision = await self.gateway.interaction(call, key, digest, proposal)
        if decision is None:
            interrupt(
                {
                    "id": key,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                    "proposal_hash": digest,
                    "generation": self.claim.admission.generation,
                }
            )
            decision = await self.gateway.interaction(call, key, digest, proposal)
        if decision is None:
            raise DomainError("approval_missing", "No durable approval decision exists")
        return decision == "approve"

    async def _repair_report(self, answer, evidence):
        limit = number("harness_report.repair_input_chars")
        prompt = (
            "Reformat the previous coding answer into the supplied JSON schema. "
            "Return only a JSON object. Use the same language. Use receipts as evidence. "
            "Do not claim any action beyond the previous answer and receipts. "
            + json.dumps(HarnessReport.model_json_schema(), ensure_ascii=False)
        )
        messages = [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "answer": str(answer)[:limit],
                        "receipts_excerpt": json.dumps(evidence, ensure_ascii=False)[:limit],
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        input_bytes = len(prompt.encode()) + len(messages[0]["content"].encode())
        model_budget = self.model.context_budget_bytes
        if len(prompt) + len(messages[0]["content"]) > self.limits.max_context_characters or (
            model_budget is not None and input_bytes > model_budget
        ):
            return None
        digest = fingerprint({"prompt": prompt, "messages": messages})
        key = f"{self.claim.admission.id}:coding:report:{digest[:32]}"
        async with self.model_lock:
            saved = await self.receipts.model_result(key, digest)
            if saved is None:
                totals = await usage_totals(self.store, self.session.id, self.claim.admission.id)
                if (
                    totals["model_calls"] >= self.limits.max_model_calls
                    or totals["total_tokens"] >= self.limits.max_run_tokens
                ):
                    return None
                await pause_at_boundary(self.pause_probe, self.claim.admission.generation)
                try:
                    async with asyncio.timeout(
                        min(self.request_timeout, number("harness_report.repair_timeout_seconds"))
                    ):
                        saved = await self.model.complete(prompt, messages, ())
                except Exception:
                    # Only the provider request is optional. Ledger failures still propagate.
                    saved = {"content": "", "tool_calls": []}
                saved["usage"] = normalized_usage(saved, prompt, messages)
                await self.receipts.save_model(
                    self.claim, key, digest, saved, append_to_history=False
                )
            if (await usage_totals(self.store, self.session.id, self.claim.admission.id))[
                "total_tokens"
            ] > self.limits.max_run_tokens:
                return None
        if saved.get("tool_calls"):
            return None
        return saved.get("content")

    async def _coding_report(self, answer):
        messages = await self.store.history(self.session.id, admission_id=self.claim.admission.id)
        files, commands, last_write = coding_evidence(messages)
        evidence = {
            "files": files,
            "commands": [
                {
                    "receipt_id": command["receipt_id"],
                    "command": str(
                        command["arguments"].get("command") or command["arguments"].get("argv")
                    )[:300],
                    "exit_code": command["exit_code"],
                    "purpose": command["arguments"].get("purpose", "general"),
                    "after_file_edits": command["batch"] > last_write,
                }
                for command in commands
            ],
        }
        evidence["delegations"] = await self.delegations.admission_summary(self.claim)
        state = await self.graph.aget_state(self.config)
        evidence["plan"] = state.values.get("todos", [])
        if self.coding_workspace is not None:
            workspace = await asyncio.to_thread(self.coding_workspace.finish)
            evidence["workspace"] = {**workspace, "isolated": self.scope.task_workspace}
            verifier = await self.receipts.latest_verification(
                self.claim, self.verification_command
            )
            verification_status = "not_run"
            if verifier:
                current = workspace["revision"]
                if (
                    current is None
                    or workspace["status"] != "observed"
                    or not verifier["before_revision"]
                    or not verifier["after_revision"]
                ):
                    verification_status = "unavailable"
                elif not current == verifier["after_revision"] == verifier["before_revision"]:
                    verification_status = "stale"
                else:
                    verification_status = "passed" if verifier["exit_code"] == 0 else "failed"
            evidence["verification"] = {
                "status": verification_status, "receipt": verifier,
                "required_command": self.verification_command,
            }
        report_status = "valid"
        try:
            report = json.loads(parse_report(answer))
        except DomainError:
            repaired = await self._repair_report(
                answer, {key: evidence[key][-16:] for key in ("files", "commands")}
            )
            try:
                report = json.loads(parse_report(repaired))
                report_status = "repaired"
            except DomainError:
                report = HarnessReport(
                    summary="Coding report could not be formatted.",
                    evidence=[],
                    changes=[],
                    tests=[],
                    limitations=[],
                ).model_dump()
                report_status = "unavailable"
        report["tests"] = [
            f"{row['command']} (exit {row['exit_code']}, receipt {row['receipt_id']})"
            for row in evidence["commands"]
            if row["purpose"] in {"reproduce", "verify"} and type(row["exit_code"]) is int
        ][-number("harness_report.tests_max_length") :]
        if self.coding_workspace is not None and workspace["status"] == "observed":
            report["changes"] = [
                f"{row['status']}: {row['path']}" for row in workspace["files"]
            ][:number("harness_report.changes_max_length")]
        elif files:
            report["changes"] = [f"{row['path']} (SHA-256 {row['sha256']})" for row in files][
                -number("harness_report.changes_max_length") :
            ]
        return json.dumps(
            {**report, "report_status": report_status, "execution_evidence": evidence},
            ensure_ascii=False,
        )

    async def run(self, *, resume=False):
        checkpoint = await self.graph.aget_state(self.config)
        history = checkpoint.values.get("messages", [])
        admission_message_id = f"{self.claim.admission.id}:user"
        if self.coding_workspace is not None and not any(
            message.id == admission_message_id for message in history
        ):
            await asyncio.to_thread(self.coding_workspace.begin)
        if (
            self.session.harness == "coding"
            and not checkpoint.next
            and history
            and isinstance(history[-1], AIMessage)
            and not history[-1].tool_calls
            and any(message.id == admission_message_id for message in history)
        ):
            answer = history[-1].content
        else:
            if self.session.harness == "coding" and await self.executions.report_pending(
                self.claim.admission
            ):
                raise DomainError("harness_output", "Coding graph checkpoint is unavailable")
            if resume:
                value = resume_command(checkpoint)
            else:
                inherited = (
                    []
                    if checkpoint.values
                    else await BranchLedger(self.store).seed(self.session.id)
                )
                value = {
                    "messages": [
                        *(to_langchain(message) for message in inherited),
                        to_langchain(await user_message(self)),
                    ]
                }
            result = await self.graph.ainvoke(value, config=self.config, durability="sync")
            if result.get("__interrupt__"):
                return interrupt_outcome(result["__interrupt__"], self.claim.admission.generation)
            answer = result["messages"][-1].content
        if self.session.harness == "coding":
            if pending := await verify_coding_task(self):
                return pending
            await self.executions.report_checkpoint(self.claim)
            answer = await self._coding_report(answer)
        elif self.session.harness != "general":
            answer = parse_report(answer)
        return {"status": "completed", "result": answer}


def build_readonly_probe(model, checkpointer):
    """A virtual-filesystem SDK smoke graph, with no host tool access."""
    backend = StateBackend()
    return create_deep_agent(
        model=model,
        system_prompt="You are a read-only research assistant. Do not execute host commands.",
        backend=backend,
        middleware=[
            FilesystemMiddleware(backend=backend, tools=["read_file", "ls", "glob", "grep"])
        ],
        checkpointer=checkpointer,
        name="deep-harness-deep-readonly-probe",
    )
