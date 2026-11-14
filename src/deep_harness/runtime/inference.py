"""Bounded inference, durable partial streams and normalized token accounting."""

import json

from sqlalchemy import select

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s
from deep_harness.runtime.models import with_timeout


async def usage_totals(store, session_id, admission_id=None):
    query = (
        select(s.model_receipts.c.response)
        .join(
            s.admissions,
            s.admissions.c.id == s.model_receipts.c.admission_id,
        )
        .where(s.admissions.c.session_id == session_id)
    )
    if admission_id is not None:
        query = query.where(s.admissions.c.id == admission_id)
    async with store.engine.connect() as conn:
        responses = list((await conn.execute(query)).scalars())
    usage = [response.get("usage", {}) for response in responses]
    return {
        "model_calls": len(responses),
        "input_tokens": sum(row.get("input_tokens", 0) for row in usage),
        "output_tokens": sum(row.get("output_tokens", 0) for row in usage),
        "total_tokens": sum(row.get("total_tokens", 0) for row in usage),
        "contains_estimates": any(row.get("estimated", False) for row in usage),
    }


def normalized_usage(response, prompt, messages):
    reported = response.get("usage") or {}
    if "input_tokens" in reported and "output_tokens" in reported:
        return {
            "input_tokens": reported["input_tokens"],
            "output_tokens": reported["output_tokens"],
            "total_tokens": reported.get(
                "total_tokens", reported["input_tokens"] + reported["output_tokens"]
            ),
            "estimated": False,
        }
    # Explicit estimate, never represented as provider billing or an exact tokenizer count.
    inputs = (
        len(prompt.encode("utf-8")) + len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    ) // 3 + 1
    outputs = (
        len(json.dumps(response.get("content", ""), ensure_ascii=False).encode("utf-8")) // 3 + 1
    )
    return {
        "input_tokens": inputs,
        "output_tokens": outputs,
        "total_tokens": inputs + outputs,
        "estimated": True,
    }


def estimated_input(prompt, messages):
    return (
        len(prompt.encode("utf-8")) + len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    ) // 3 + 1


class Inference:
    def __init__(self, runtime):
        self.runtime = runtime

    async def invoke(self, key, messages):
        runtime = self.runtime
        usage = await usage_totals(runtime.store, runtime.session.id, runtime.claim.admission.id)
        if (
            usage["total_tokens"] + estimated_input(runtime.session.prompt, messages)
            >= runtime.limits.max_run_tokens
        ):
            raise DomainError("budget_exhausted", "Per-run token budget exhausted")
        async with runtime.store.transaction() as conn:
            await runtime.executions.assert_claim(conn, runtime.claim)
            await runtime.store.append_event(conn, runtime.session.id, "model.started", {"id": key})
        if hasattr(runtime.model, "stream"):
            response = await with_timeout(self.stream(key, messages), runtime.model.request_timeout)
        else:
            response = await with_timeout(
                runtime.model.complete(runtime.session.prompt, messages, runtime.session.tools),
                runtime.model.request_timeout,
            )
        return {**response, "usage": normalized_usage(response, runtime.session.prompt, messages)}

    async def stream(self, key, messages):
        runtime = self.runtime
        pending = ""
        sequence = 0
        response = None
        async for item in runtime.model.stream(
            runtime.session.prompt, messages, runtime.session.tools
        ):
            if item["kind"] == "delta":
                pending += item["text"]
                if len(pending) >= 128:
                    await self.delta(key, sequence, pending)
                    sequence += 1
                    pending = ""
            elif item["kind"] == "result":
                response = item["response"]
        if pending:
            await self.delta(key, sequence, pending)
        if response is None:
            raise DomainError(
                "empty_model_stream", "Model stream ended without a complete response"
            )
        return response

    async def delta(self, key, sequence, text):
        runtime = self.runtime
        async with runtime.store.transaction() as conn:
            await runtime.executions.assert_claim(conn, runtime.claim)
            await runtime.store.append_event(
                conn,
                runtime.session.id,
                "model.delta",
                {
                    "id": f"{key}:delta:{sequence}",
                    "message_id": key,
                    "text": text,
                    "provisional": True,
                },
            )
