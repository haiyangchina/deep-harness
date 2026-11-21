"""Deep Agents graph guarded by Harness admission, approval and receipt ledgers."""

import asyncio
import json
from dataclasses import replace

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware import FilesystemMiddleware
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import ValidationError
from sqlalchemy import select

from deep_harness.application.tools import ToolGateway, tool_definitions
from deep_harness.application.workspace import CODING_TOOLS, READ_TOOLS
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, ToolCall
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.branches import BranchLedger
from deep_harness.infrastructure.delegations import DelegationLedger
from deep_harness.runtime.approval_checkpoint import (
    approval_ids,
    interrupt_outcome,
    pause_at_boundary,
    resume_command,
)
from deep_harness.runtime.context import summarize
from deep_harness.runtime.harnesses import harness_prompt, parse_report
from deep_harness.runtime.inference import normalized_usage, usage_totals
from deep_harness.runtime.models import LangChainModel, model_errors, to_langchain


class ReceiptedModel(AgentMiddleware):
    def __init__(self, runtime):
        self.runtime = runtime

    async def awrap_model_call(self, request, handler):
        async with self.runtime.model_lock:
            await pause_at_boundary(
                self.runtime.pause_probe, self.runtime.claim.admission.generation
            )
            return await self._call(request, handler)

    async def _call(self, request, handler):
        owner = self.runtime
        prompt = request.system_message.model_dump(mode="json") if request.system_message else None
        request, messages = self._project(request, prompt)
        digest = fingerprint({"prompt": prompt, "messages": messages, "tools": owner.tool_specs})
        key = f"{owner.claim.admission.id}:deep:model:{digest[:32]}"
        saved = await owner.receipts.model_result(key, digest)
        if saved is not None:
            return ModelResponse(result=[owner.message_from_receipt(saved)])
        totals = await usage_totals(owner.store, owner.session.id, owner.claim.admission.id)
        if totals["model_calls"] >= owner.limits.max_model_calls:
            raise DomainError("budget_exhausted", "Model call limit reached")
        if totals["total_tokens"] >= owner.limits.max_run_tokens:
            raise DomainError("budget_exhausted", "Per-run token budget exhausted")
        async with owner.store.transaction() as conn:
            await owner.executions.assert_claim(conn, owner.claim)
            await owner.store.append_event(conn, owner.session.id, "model.started", {"id": key})
        with model_errors():
            async with asyncio.timeout(owner.request_timeout):
                response = await handler(request)
        if len(response.result) != 1 or not isinstance(response.result[0], AIMessage):
            raise DomainError("model_response", "Deep model returned an unsupported response")
        answer = response.result[0]
        canonical = {
            "id": key,
            "role": "assistant",
            "content": answer.content,
            "tool_calls": answer.tool_calls,
            "provider_extensions": answer.additional_kwargs,
            "response_metadata": answer.response_metadata,
            "usage": normalized_usage(
                {"content": answer.content, "usage": answer.usage_metadata},
                owner.session.prompt,
                messages,
            ),
        }
        await owner.receipts.save_model(
            owner.claim,
            key,
            digest,
            canonical,
            append_to_history=owner.record_messages,
        )
        if (await usage_totals(owner.store, owner.session.id, owner.claim.admission.id))[
            "total_tokens"
        ] > owner.limits.max_run_tokens:
            raise DomainError("budget_exhausted", "Per-run token budget exceeded by model output")
        answer.id = key
        return response

    def _project(self, request, prompt):
        owner = self.runtime
        budget = getattr(owner.model, "context_budget_bytes", None)
        original = request.messages
        messages = [message.model_dump(mode="json") for message in original]
        fixed = json.dumps(
            {"prompt": prompt, "tools": request.tools}, ensure_ascii=False, default=str
        )

        char_limit = owner.limits.max_context_characters - len(fixed)
        byte_limit = budget - len(fixed.encode()) if budget is not None else None

        def fits(chars, size):
            return chars <= char_limit and (byte_limit is None or size <= byte_limit)

        rendered = [json.dumps(row, ensure_ascii=False, default=str) for row in messages]
        chars = size = 2  # JSON array brackets
        first_turn = None
        for index in range(len(original) - 1, -1, -1):
            separator = 2 if index + 1 < len(original) else 0
            chars += len(rendered[index]) + separator
            size += len(rendered[index].encode()) + separator
            if not fits(chars, size):
                break
            if index == 0:
                return request, messages
            previous = original[index - 1]
            if (
                isinstance(original[index], HumanMessage)
                and isinstance(previous, AIMessage)
                and not previous.tool_calls
            ):
                first_turn = index
        if first_turn is not None:
            retained = original[first_turn:]
            retained_payload = messages[first_turn:]
            user_history = [
                {"role": "user", "content": message.content}
                for message in original[:first_turn]
                if isinstance(message, HumanMessage)
            ]
            summary = summarize("", user_history, owner.limits.summary_characters)
            if summary:
                note = HumanMessage(
                    id=f"deep-context:{fingerprint(summary)}",
                    content=f"Archived user inputs (historical context):\n{summary}",
                )
                projected = [note.model_dump(mode="json"), *retained_payload]
                history = json.dumps(projected, ensure_ascii=False, default=str)
                if fits(len(history), len(history.encode())):
                    return request.override(messages=[note, *retained]), projected
            return request.override(messages=retained), retained_payload
        raise DomainError(
            "context_budget_exhausted",
            "Deep current turn, tool outputs or schemas exceed the configured context budget",
        )


class DeepRuntime:
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
        background_launcher=None,
    ):
        if not isinstance(model, LangChainModel):
            raise DomainError("deep_model", "Deep engine requires a LangChain chat model")
        self.session = session
        self.claim = claim
        self.store = store
        self.executions = executions
        self.receipts = receipts
        self.limits = limits
        self.request_timeout = model.request_timeout
        self.record_messages = record_messages
        self.delegation_id = delegation_id
        self.model_lock = model_lock or asyncio.Lock()
        self.pause_probe = pause_probe
        self.tool_lock = tool_lock or asyncio.Lock()
        self.background_launcher = background_launcher
        self.scope = scope
        self.model = model
        self.saver = saver
        self.mcp = mcp
        self.tool_specs = tool_definitions(session.tools, mcp_bindings)
        self.gateway = ToolGateway(
            scope,
            session,
            receipts,
            claim,
            mcp=mcp,
            mcp_bindings=mcp_bindings,
            web_client=web_client,
        )
        self.delegations = DelegationLedger(store, executions)
        params = model.model._get_ls_params()
        provider, model_name = params.get("ls_provider"), params.get("ls_model_name")
        if not provider or not model_name:
            raise DomainError("deep_model", "Model provider identity is required")
        register_harness_profile(
            f"{provider}:{model_name}",
            HarnessProfile(
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
                excluded_tools=frozenset({"read_file"}),
                excluded_middleware=frozenset({"SummarizationMiddleware"}),
            ),
        )
        backend = StateBackend()
        self.graph = create_deep_agent(
            model=model.model,
            tools=[self._tool(spec) for spec in self.tool_specs],
            system_prompt=harness_prompt(session.prompt, session.harness),
            backend=backend,
            middleware=[
                FilesystemMiddleware(backend=backend, tools=["read_file"]),
                ReceiptedModel(self),
            ],
            subagents=[],
            checkpointer=saver,
            name="deep-harness-deep",
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
        name = f"harness_{original}" if original == "read_file" else original

        async def invoke(runtime: ToolRuntime, **arguments):
            await pause_at_boundary(self.pause_probe, self.claim.admission.generation)
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
                    async with self.tool_lock:
                        delegation = await self.delegations.begin(self.claim, call)
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
                    async with self.tool_lock:
                        required = self.gateway.approval_reason(call) is not None
                        if required and not await self._approval(call):
                            result = "Tool denied by user."
                        else:
                            try:
                                result = await self.gateway.execute(call)
                            except DomainError as exc:
                                if exc.code != "approval_missing":
                                    raise
                                result = (
                                    await self.gateway.execute(call)
                                    if await self._approval(call)
                                    else "Tool denied by user."
                                )
            except (ValidationError, OSError, UnicodeError) as exc:
                result = f"Tool error: {type(exc).__name__}: {exc}"
            except DomainError as exc:
                if exc.code in {
                    "unknown_effect",
                    "lease_lost",
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
        if call.arguments.get("background"):
            if self.background_launcher is None:
                raise DomainError("delegation_unavailable", "Background launcher is unavailable")
            handle = await self.background_launcher(
                self.session, self.claim, call, delegation, self.gateway.mcp_bindings
            )
            return json.dumps(handle, ensure_ascii=False)
        if delegation["status"] == "completed":
            return delegation["result"]
        kind = call.arguments["kind"]
        guide = {
            "research": (
                "Research this bounded question using only the granted sources. "
                "State uncertainty clearly and cite paths or source identifiers."
            ),
            "coding": (
                "Complete this bounded coding task using only granted tools. "
                "Inspect before editing, request approval for changes and commands, "
                "and report actual changes and test results."
            ),
        }[kind]
        allowed = (
            CODING_TOOLS
            if kind == "coding"
            else {*READ_TOOLS, *(name for name in self.session.tools if name.startswith("mcp__"))}
        )
        child_tools = tuple(sorted(allowed.intersection(self.session.tools)))
        child_session = replace(
            self.session,
            prompt=f"{self.session.prompt}\n\n{guide}",
            tools=child_tools,
            harness="general",
        )
        child = DeepRuntime(
            session=child_session,
            claim=self.claim,
            scope=self.scope,
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
            tool_lock=self.tool_lock,
        )
        child.config["configurable"]["thread_id"] = (
            f"{self.session.id}:delegate:{self.claim.admission.id}:{call.id}"
        )
        checkpoint = await child.graph.aget_state(child.config)
        if checkpoint.values and not checkpoint.next:
            result = checkpoint.values["messages"][-1].content
            await self.delegations.finish(self.claim, delegation["id"], result)
            return result
        value = (
            resume_command(checkpoint)
            if checkpoint.next
            else {"messages": [HumanMessage(id=f"{call.id}:task", content=call.arguments["task"])]}
        )
        result = await child.graph.ainvoke(
            value,
            config=child.config,
            durability="sync",
        )
        if pending := result.get("__interrupt__"):
            interrupt(
                {
                    **pending[0].value,
                    "interaction_ids": approval_ids(pending, self.claim.admission.generation),
                }
            )
        answer = result["messages"][-1].content
        await self.delegations.finish(self.claim, delegation["id"], answer)
        return answer

    async def _approval(self, call):
        key = f"{self.claim.admission.id}:{call.id}"
        digest = fingerprint({"name": call.name, "arguments": call.arguments})
        scope_key, _, can_reuse = self.gateway.approval_scope(call)
        if can_reuse and await self.receipts.scope_granted(
            self.claim, scope_key, self.session.permission_revision, exclude_key=key
        ):
            return True
        proposal = {"tool_name": call.name, "arguments": call.arguments}
        if self.session.permission_mode != "legacy":
            proposal.update(
                reason=self.gateway.approval_reason(call),
                permission_mode=self.session.permission_mode,
                permission_revision=self.session.permission_revision,
                scope_key=scope_key,
                reusable=can_reuse,
            )
        decision = await self.receipts.interaction(self.claim, key, digest, proposal)
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
            decision = await self.receipts.interaction(self.claim, key, digest, proposal)
        if decision is None:
            raise DomainError("approval_missing", "No durable approval decision exists")
        return decision == "approve"

    async def run(self, *, resume=False):
        if resume:
            value = resume_command(await self.graph.aget_state(self.config))
        else:
            checkpoint = await self.graph.aget_state(self.config)
            inherited = (
                [] if checkpoint.values else await BranchLedger(self.store).seed(self.session.id)
            )
            value = {
                "messages": [
                    *(to_langchain(message) for message in inherited),
                    HumanMessage(
                        id=f"{self.claim.admission.id}:user", content=self.claim.admission.text
                    ),
                ]
            }
        result = await self.graph.ainvoke(value, config=self.config, durability="sync")
        if result.get("__interrupt__"):
            return interrupt_outcome(result["__interrupt__"], self.claim.admission.generation)
        answer = result["messages"][-1].content
        if self.session.harness != "general":
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
