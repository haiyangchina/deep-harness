"""HarnessGuardMiddleware: the single contract guard for the unified kernel.

This middleware is the one place where the four harness contracts are enforced
around model calls:

1. approval persistence -- tool interrupts are translated by approval_checkpoint
2. receipts -- model calls are idempotent and recorded in the ledger
3. generation fencing -- every model call asserts the execution claim
4. crash recovery -- replays hit the model receipt and never re-run side effects

It is the renamed, extracted form of the former ``ReceiptedModel`` so that the
guard is a first-class architectural component rather than an implementation
detail of the Deep runtime.
"""

import asyncio
import json

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.runtime.approval_checkpoint import pause_at_boundary
from deep_harness.runtime.context import summarize
from deep_harness.runtime.inference import normalized_usage, usage_totals
from deep_harness.runtime.models import model_errors


class HarnessGuardMiddleware(AgentMiddleware):
    """Guards model invocation with receipts, budgets, fencing and context."""

    def __init__(self, runtime):
        self.runtime = runtime

    async def awrap_model_call(self, request, handler):
        async with self.runtime.model_lock:
            await pause_at_boundary(
                self.runtime.pause_probe, self.runtime.claim.admission.generation
            )
            return await self._call(request, handler)

    async def abefore_model(self, state, runtime):
        """Fencing at the model boundary: a lost lease never reaches the model."""


        async with self.runtime.store.transaction() as conn:
            await self.runtime.executions.assert_claim(conn, self.runtime.claim)
        return None

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
        chars = size = 2
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
