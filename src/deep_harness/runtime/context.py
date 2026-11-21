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
        if message["role"] != "user":
            continue
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(f"{message['role']}: {content[:512]}")
    summary = "\n".join(parts)
    if len(summary) > limit:
        half = limit // 2
        summary = summary[:half] + "\n[earlier details archived]\n" + summary[-half + 30 :]
    return summary


MIN_TOOL_CHARS = 512
TRUNCATION_MARKER = (
    "\n[... tool output truncated to fit the context budget; re-read with a smaller limit ...]\n"
)


def _content(message):
    value = message.get("content", "")
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def clip_tool_outputs(messages, fits):
    """Shrink tool results until the projected view fits.

    Only the projected view is rewritten; the persisted transcript keeps the complete
    tool output. Returns None when clipping cannot free enough room, in which case the
    caller must still fail explicitly rather than send an oversized request.
    """
    clipped = [dict(message) for message in messages]
    for _ in range(64):
        if fits(clipped):
            return clipped
        candidates = [
            (len(_content(clipped[index])), index)
            for index in range(len(clipped))
            if clipped[index]["role"] == "tool" and len(_content(clipped[index])) > MIN_TOOL_CHARS
        ]
        if not candidates:
            return None
        _, index = max(candidates)
        content = _content(clipped[index])
        keep = max(MIN_TOOL_CHARS, int(len(content) * 0.6))
        head = int(keep * 0.7)
        tail = keep - head - len(TRUNCATION_MARKER)
        if tail <= 0:
            return None
        clipped[index]["content"] = content[:head] + TRUNCATION_MARKER + content[-tail:]
    return None


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
        legacy_summary = head is not None and head["summary_version"] == 0
        if legacy_summary:
            summary = summarize(
                "", [message for message in messages if message["id"] in dropped],
                self.limits.summary_characters,
            )
        else:
            summary = head["summary"] if head else ""
        revision = head["revision"] if head else 0
        remaining = [message for message in messages if message["id"] not in dropped]
        archived = []
        fixed_chars = len(prompt) + serialized_size(tools) + 256
        fixed_bytes = (
            len(prompt.encode("utf-8")) + serialized_size(tools, utf8=True) + 256
            if max_context_bytes is not None
            else 0
        )

        def budgets():
            # Only a summary that actually exists consumes room. Reserving
            # summary_characters * 4 bytes unconditionally wasted 8 KB on every
            # projection, even when no compaction had ever happened.
            available = self.limits.max_context_characters - fixed_chars - len(summary)
            byte_available = (
                max_context_bytes - fixed_bytes - len(summary.encode("utf-8"))
                if max_context_bytes is not None
                else None
            )
            return available, byte_available

        def over(rows):
            available, byte_available = budgets()
            return serialized_size(rows) > available or (
                byte_available is not None and serialized_size(rows, utf8=True) > byte_available
            )

        available, byte_available = budgets()
        if available < 256 or (byte_available is not None and byte_available < 256):
            raise DomainError(
                "context_budget_exhausted", "Prompt and tools exceed the configured context budget"
            )
        while over(remaining):
            turn, rest = first_turn(remaining)
            if not turn or any(message["id"] == f"{claim.admission.id}:user" for message in turn):
                # No completed turn is left to archive. Clip oversized tool results in the
                # projection instead of leaving the session permanently stuck: previously
                # every further model call failed with context_budget_exhausted.
                clipped = clip_tool_outputs(remaining, lambda rows: not over(rows))
                if clipped is None:
                    raise DomainError(
                        "context_budget_exhausted",
                        "Current turn exceeds the context budget; use smaller tool output or input",
                    )
                remaining = clipped
                break
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
        elif legacy_summary:
            async with self.store.transaction() as conn:
                await self.executions.assert_claim(conn, claim)
                await conn.execute(
                    update(s.context_heads)
                    .where(
                        s.context_heads.c.session_id == claim.admission.session_id,
                        s.context_heads.c.revision == revision,
                    )
                    .values(summary=summary, summary_version=1)
                )
        if summary:
            return [
                {
                    "id": f"context-summary:{revision}",
                    "role": "user",
                    "content": "Archived user inputs (historical context):\n" + summary,
                },
                *remaining,
            ]
        return remaining

    async def publish(self, claim, revision, dropped, summary, archived, previous):
        async with self.store.transaction() as conn:
            await self.executions.assert_claim(conn, claim)
            values = {
                "revision": revision,
                "dropped_ids": sorted(dropped),
                "summary": summary,
                "summary_version": 1,
            }
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
