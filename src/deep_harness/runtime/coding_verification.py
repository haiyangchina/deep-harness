"""A host-owned LangGraph step for an operator-pinned verification command."""

import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, ToolCall
from deep_harness.runtime.approval_checkpoint import (
    checkpoint_interrupts,
    interrupt_outcome,
    pause_at_boundary,
    resume_command,
)


class VerificationState(TypedDict):
    status: str


async def verify_coding_task(runtime):
    command = runtime.verification_command
    if not command or runtime.coding_workspace is None or "bash" not in runtime.session.tools:
        return None

    claim = runtime.claim
    generation = claim.admission.generation
    revision = await asyncio.to_thread(runtime.coding_workspace.revision)
    receipt = await runtime.receipts.latest_verification(claim, command)
    if receipt and revision and receipt["after_revision"] == receipt["before_revision"] == revision:
        return None

    call = ToolCall(
        "fixed-verification-" + fingerprint(command)[:24],
        "bash",
        {"command": command, "cwd": ".", "purpose": "verify"},
    )

    async def execute(_state):
        await pause_at_boundary(runtime.pause_probe, generation)
        await runtime.executions.reserve_tools(claim, (call.id,), runtime.limits.max_tool_calls)
        if runtime.gateway.approval_reason(call) is not None and not await runtime._approval(call):
            return {"status": "denied"}
        try:
            await runtime.gateway.execute(call)
        except DomainError as exc:
            if exc.code != "approval_missing":
                raise
            if not await runtime._approval(call):
                return {"status": "denied"}
            await runtime.gateway.execute(call)
        return {"status": "finished"}

    build = StateGraph(VerificationState)
    build.add_node("verify", execute)
    build.add_edge(START, "verify")
    build.add_edge("verify", END)
    graph = build.compile(checkpointer=runtime.saver)
    config = {"configurable": {
        "thread_id": f"{runtime.session.id}:verify:{claim.admission.id}",
    }}
    checkpoint = await graph.aget_state(config)
    if checkpoint.values and not checkpoint.next:
        return None
    if checkpoint.next:
        value = resume_command(checkpoint) if checkpoint_interrupts(checkpoint) else None
    else:
        value = {"status": "pending"}
    result = await graph.ainvoke(value, config=config, durability="sync")
    if pending := result.get("__interrupt__"):
        return interrupt_outcome(pending, generation)
    return None
