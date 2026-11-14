"""Persisted extractive compaction at whole-turn boundaries; full transcript remains intact."""

import json

from sqlalchemy import insert, select, update

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s


def serialized_size(messages, *, utf8=False):
    value = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    return len(value.encode("utf-8")) if utf8 else len(value)


def first_turn(messages):
    for index, message in enumerate(messages[1:], 1):
        if message["role"] == "user":
            return messages[:index], messages[index:]
    return [], messages


def summarize(previous, messages, limit):
    parts = [previous] if previous else []
    for message in messages:
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(f"{message['role']}: {content[:512]}")
    summary = "\n".join(parts)
    if len(summary) > limit:
        half = limit // 2
        summary = summary[:half] + "\n[earlier details archived]\n" + summary[-half + 30 :]
    return summary


class ContextProjector:
    def __init__(self, store, executions, limits):
        self.store = store
        self.executions = executions
        self.limits = limits

    async def project(self, claim, prompt, tools, messages, *, max_context_bytes=None):
        async with self.store.engine.connect() as conn:
            head = (
                (
                    await conn.execute(
                        select(s.context_heads).where(
                            s.context_heads.c.session_id == claim.admission.session_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
        dropped = set(head["dropped_ids"]) if head else set()
        summary = head["summary"] if head else ""
        revision = head["revision"] if head else 0
        remaining = [message for message in messages if message["id"] not in dropped]
        archived = []
        available = self.limits.max_context_characters - (
            len(prompt) + serialized_size(tools) + self.limits.summary_characters + 256
        )
        # Enforce the provider byte budget independently; four bytes per summary character
        # covers multilingual summaries without changing the configured character limit.
        byte_available = None
        if max_context_bytes is not None:
            byte_available = max_context_bytes - (
                len(prompt.encode("utf-8"))
                + serialized_size(tools, utf8=True)
                + self.limits.summary_characters * 4
                + 256
            )
        if available < 256 or (byte_available is not None and byte_available < 256):
            raise DomainError(
                "context_budget_exhausted", "Prompt and tools exceed the configured context budget"
            )
        while serialized_size(remaining) > available or (
            byte_available is not None and serialized_size(remaining, utf8=True) > byte_available
        ):
            turn, rest = first_turn(remaining)
            if not turn or any(message["id"] == f"{claim.admission.id}:user" for message in turn):
                raise DomainError(
                    "context_budget_exhausted",
                    "Current turn exceeds the context budget; use smaller tool output or input",
                )
            if turn[-1]["role"] != "assistant" or turn[-1].get("tool_calls"):
                raise DomainError(
                    "context_incomplete_turn", "Cannot compact an incomplete tool exchange"
                )
            archived.extend(turn)
            dropped.update(message["id"] for message in turn)
            summary = summarize(summary, turn, self.limits.summary_characters)
            remaining = rest
        if archived:
            revision += 1
            await self.publish(claim, revision, dropped, summary, archived, head)
        if summary:
            return [
                {
                    "id": f"context-summary:{revision}",
                    "role": "system",
                    "content": "Earlier conversation (extractive summary; details archived):\n"
                    + summary,
                },
                *remaining,
            ]
        return remaining

    async def publish(self, claim, revision, dropped, summary, archived, previous):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            values = {"revision": revision, "dropped_ids": sorted(dropped), "summary": summary}
            if previous:
                result = await conn.execute(
                    update(s.context_heads)
                    .where(
                        s.context_heads.c.session_id == claim.admission.session_id,
                        s.context_heads.c.revision == previous["revision"],
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise DomainError(
                        "context_conflict", "Context revision changed during compaction"
                    )
            else:
                await conn.execute(
                    insert(s.context_heads).values(session_id=claim.admission.session_id, **values)
                )
            await conn.execute(
                insert(s.context_archives).values(
                    id=fingerprint({"session": claim.admission.session_id, "revision": revision}),
                    session_id=claim.admission.session_id,
                    revision=revision,
                    messages=archived,
                    summary=summary,
                )
            )
            await self.store.append_event(
                conn,
                claim.admission.session_id,
                "context.compacted",
                {
                    "revision": revision,
                    "archived_messages": len(archived),
                },
            )
