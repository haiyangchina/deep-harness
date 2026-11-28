"""Checkpointed delivery after a transport has accepted explicit operator approval."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.task_workspaces import finish_io


class DeliveryState(TypedDict):
    approved: bool
    receipt: dict | None


async def dispatch_delivery(saver, owner, delivery, action, revision, bundle=None):
    if action not in {"apply", "undo"}:
        return await finish_io(delivery.command, action, revision, bundle)

    async def dispatch(state):
        if not state["approved"]:
            raise RuntimeError("Delivery requires operator approval")
        return {"receipt": await finish_io(delivery.command, action, revision, bundle)}

    graph = StateGraph(DeliveryState)
    graph.add_node("dispatch", dispatch)
    graph.add_edge(START, "dispatch")
    graph.add_edge("dispatch", END)
    compiled = graph.compile(checkpointer=saver)
    key = fingerprint([owner, action, revision, bundle["id"] if bundle else None])
    legacy = {"configurable": {"thread_id": "delivery:" + key}}
    config = {"configurable": {"thread_id": f"delivery:{owner}:{key}"}}
    if await saver.aget_tuple(legacy) is not None:
        config = legacy
    checkpoint = await compiled.aget_state(config)
    if checkpoint.values and not checkpoint.next:
        # The current ledger can have moved from applied to undone since this checkpoint.
        if not await finish_io(delivery.status):
            raise DomainError("unknown_effect", "The delivery ledger is missing")
        return await finish_io(delivery.command, action, revision, bundle)
    value = None if checkpoint.next else {"approved": True, "receipt": None}
    result = await compiled.ainvoke(value, config=config, durability="sync")
    return result["receipt"]
