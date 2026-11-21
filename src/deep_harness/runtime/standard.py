"""A single explicit LangGraph loop using project-owned receipts and approvals."""

from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError
from sqlalchemy import select

from deep_harness.application.tools import ToolGateway, tool_definitions
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, ToolCall
from deep_harness.infrastructure import schema as s
from deep_harness.infrastructure.branches import BranchLedger
from deep_harness.runtime.approval_checkpoint import approval_ids, resume_command
from deep_harness.runtime.context import ContextProjector
from deep_harness.runtime.inference import Inference, usage_totals


def append_unique(current: list[dict], incoming: list[dict]) -> list[dict]:
    """Only append stable IDs. A replay cannot rewrite an existing message."""
    result = list(current)
    known = {message["id"]: message for message in current}
    for message in incoming:
        existing = known.get(message["id"])
        if existing is not None and fingerprint(existing) != fingerprint(message):
            raise DomainError("message_conflict", "Graph replay changed committed content")
        if existing is None:
            result.append(message)
            known[message["id"]] = message
    return result


class State(TypedDict):
    messages: Annotated[list[dict], append_unique]
    admission_id: str
    step: int
    tool_count: int


class StandardRuntime:
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
    ):
        self.session = session
        self.claim = claim
        self.model = model
        self.store = store
        self.executions = executions
        self.receipts = receipts
        self.limits = limits
        self.tool_specs = tool_definitions(session.tools, mcp_bindings)
        if hasattr(model, "with_tool_specs"):
            self.model = model.with_tool_specs(self.tool_specs)
        self.context = ContextProjector(store, executions, limits)
        self.gateway = ToolGateway(
            scope,
            session,
            receipts,
            claim,
            mcp=mcp,
            mcp_bindings=mcp_bindings,
            web_client=web_client,
        )
        graph = StateGraph(State)
        graph.add_node("model", self._model)
        graph.add_node("tools", self._tools)
        graph.add_edge(START, "model")
        graph.add_conditional_edges("model", self._next)
        graph.add_edge("tools", "model")
        self.graph = graph.compile(checkpointer=saver)
        self.config = {
            "configurable": {"thread_id": session.id},
            "recursion_limit": 2 * limits.max_model_calls + 4,
        }

    async def _model(self, state: State) -> dict:
        if state["step"] >= self.limits.max_model_calls:
            raise DomainError("budget_exhausted", "Model call limit reached")
        key = f"{state['admission_id']}:model:{state['step']}"
        projected = await self.context.project(
            self.claim,
            self.session.prompt,
            self.tool_specs,
            state["messages"],
            max_context_bytes=getattr(self.model, "context_budget_bytes", None),
        )
        digest = fingerprint(
            {
                "prompt": self.session.prompt,
                "messages": projected,
                "tools": self.tool_specs,
            }
        )
        response = await self.receipts.model_result(key, digest)
        if response is None:
            async with self.store.transaction() as conn:
                await self.executions.assert_claim(conn, self.claim)
            response = await Inference(self).invoke(key, projected)
            response = {**response, "id": key}
            await self.receipts.save_model(self.claim, key, digest, response)
        totals = await usage_totals(self.store, self.session.id, self.claim.admission.id)
        if totals["total_tokens"] > self.limits.max_run_tokens:
            raise DomainError("budget_exhausted", "Per-run token budget exceeded by model output")
        return {"messages": [response], "step": state["step"] + 1}

    def _next(self, state: State):
        return "tools" if state["messages"][-1].get("tool_calls") else END

    async def _approval(self, call: ToolCall) -> bool:
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
        # An interrupt resume replays this node from the beginning. Keep waiting for
        # the durable decision instead of turning a still-pending approval into a
        # permanent "Tool refused" tool result: on the next resume the same tool call
        # would then produce different content and trip message_conflict.
        for _ in range(16):
            decision = await self.receipts.interaction(self.claim, key, digest, proposal)
            if decision is not None:
                return decision == "approve"
            interrupt(
                {
                    "id": key,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                    "proposal_hash": digest,
                    "generation": self.claim.admission.generation,
                }
            )
            # Only the durable decision is authoritative; a raw resume payload is not.
        raise DomainError("approval_missing", "No durable approval decision exists")

    async def _tools(self, state: State) -> dict:
        results = []
        count = state.get("tool_count", 0)
        await self.executions.reserve_tools(
            self.claim,
            (raw["id"] for raw in state["messages"][-1]["tool_calls"]),
            self.limits.max_tool_calls,
        )
        for raw in state["messages"][-1]["tool_calls"]:
            call = ToolCall(raw["id"], raw["name"], raw["args"])
            message_id = f"{self.claim.admission.id}:tool:{call.id}"
            # An approval resume replays this node from the start. Reuse the committed
            # result so no tool (especially a write) runs twice and a replay cannot
            # change a message that is already durable.
            committed = await self.store.committed_message(message_id)
            if committed is not None:
                results.append(committed)
                continue
            try:
                self.gateway.validate(call)
                approval_required = self.gateway.approval_reason(call) is not None
                approved = not approval_required or await self._approval(call)
                content = "Tool denied by user."
                if approved:
                    try:
                        content = await self.gateway.execute(call)
                    except DomainError as exc:
                        if exc.code != "approval_missing":
                            raise
                        if await self._approval(call):
                            content = await self.gateway.execute(call)
            except (ValidationError, OSError, UnicodeError) as exc:
                content = f"Tool error: {type(exc).__name__}: {exc}"
            except DomainError as exc:
                if exc.code in {
                    "unknown_effect",
                    "lease_lost",
                    "effect_conflict",
                    "approval_missing",
                }:
                    raise
                content = f"Tool refused [{exc.code}]: {exc}"
            message = {
                "id": message_id,
                "role": "tool",
                "tool_call_id": call.id,
                "content": content,
            }
            async with self.store.transaction() as conn:
                await self.executions.assert_claim(conn, self.claim)
                await self.store.append_message(conn, self.claim, message)
            results.append(message)
        return {"messages": results, "tool_count": count + len(results)}

    async def run(self, *, resume: bool = False) -> dict:
        if resume:
            value = resume_command(await self.graph.aget_state(self.config))
        else:
            checkpoint = await self.graph.aget_state(self.config)
            inherited = (
                [] if checkpoint.values else await BranchLedger(self.store).seed(self.session.id)
            )
            value = {
                "messages": [
                    *inherited,
                    {
                        "id": f"{self.claim.admission.id}:user",
                        "role": "user",
                        "content": self.claim.admission.text,
                    },
                ],
                "admission_id": self.claim.admission.id,
                "step": 0,
                "tool_count": 0,
            }
        result = await self.graph.ainvoke(value, config=self.config, durability="sync")
        if result.get("__interrupt__"):
            return {
                "status": "waiting_approval",
                "result": None,
                "interactions": approval_ids(
                    result["__interrupt__"], self.claim.admission.generation
                ),
            }
        return {"status": "completed", "result": result["messages"][-1]["content"]}

    async def open_interactions(self) -> list[dict]:
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(
                select(s.interactions).where(
                    s.interactions.c.session_id == self.session.id,
                    s.interactions.c.decision.is_(None),
                )
            )
            return [dict(row) for row in rows.mappings()]
