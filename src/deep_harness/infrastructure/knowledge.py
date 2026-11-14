"""Profile-local versioned skills and memories, frozen when a session is created."""

import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert, select, update

from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s

SKILL_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def parse_skill(document: str):
    if len(document) > 20_000:
        raise DomainError("skill_too_large", "Skill exceeds 20,000 characters")
    lines = document.replace("\r\n", "\n").split("\n")
    if len(lines) < 5 or lines[0] != "---":
        raise DomainError("skill_format", "Skill needs frontmatter with name and description")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise DomainError("skill_format", "Skill frontmatter is not closed") from exc
    metadata = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if not separator or key not in {"name", "description"} or key in metadata:
            raise DomainError("skill_format", "Only name and description are supported")
        metadata[key] = value.strip()
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    body = "\n".join(lines[end + 1 :]).strip()
    if not SKILL_NAME.fullmatch(name) or not 1 <= len(description) <= 240 or not body:
        raise DomainError("skill_format", "Skill name, description or body is invalid")
    return name, description, body


class KnowledgeStore:
    def __init__(self, store):
        self.store = store

    async def add_skill(self, document: str):
        name, description, body = parse_skill(document)
        digest = fingerprint({"name": name, "description": description, "body": body})
        async with self.store.transaction() as conn:
            previous = (
                (
                    await conn.execute(
                        select(s.skills)
                        .where(
                            s.skills.c.profile_id == self.store.profile_id,
                            s.skills.c.name == name,
                        )
                        .order_by(s.skills.c.version.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if previous and previous["active"] and previous["content_hash"] == digest:
                return dict(previous)
            if previous:
                await conn.execute(
                    update(s.skills)
                    .where(
                        s.skills.c.profile_id == self.store.profile_id,
                        s.skills.c.name == name,
                    )
                    .values(active=0)
                )
            row = {
                "id": str(uuid4()),
                "profile_id": self.store.profile_id,
                "name": name,
                "description": description,
                "body": body,
                "version": previous["version"] + 1 if previous else 1,
                "active": 1,
                "content_hash": digest,
            }
            await conn.execute(insert(s.skills).values(**row))
            return row

    async def disable_skill(self, name: str):
        async with self.store.transaction() as conn:
            result = await conn.execute(
                update(s.skills)
                .where(
                    s.skills.c.profile_id == self.store.profile_id,
                    s.skills.c.name == name,
                    s.skills.c.active == 1,
                )
                .values(active=0)
            )
            if result.rowcount != 1:
                raise DomainError("skill_not_found", "Active skill is not available")

    async def list_skills(self):
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(
                select(s.skills)
                .where(s.skills.c.profile_id == self.store.profile_id, s.skills.c.active == 1)
                .order_by(s.skills.c.name)
            )
            return [dict(row) for row in rows.mappings()]

    async def remember(self, content: str, source: str, *, memory_id=None):
        content = content.strip()
        if not 1 <= len(content) <= 1000 or not 1 <= len(source) <= 128:
            raise DomainError("invalid_memory", "Memory content or source is invalid")
        memory_id = memory_id or str(uuid4())
        digest = fingerprint({"content": content, "source": source})
        async with self.store.transaction() as conn:
            existing = (
                (await conn.execute(select(s.memories).where(s.memories.c.id == memory_id)))
                .mappings()
                .first()
            )
            if existing:
                if (
                    existing["profile_id"] != self.store.profile_id
                    or existing["content_hash"] != digest
                ):
                    raise DomainError("memory_conflict", "Memory identity has different content")
                return dict(existing)
            row = {
                "id": memory_id,
                "profile_id": self.store.profile_id,
                "content": content,
                "source": source,
                "created_at": datetime.now(UTC).isoformat(),
                "active": 1,
                "content_hash": digest,
            }
            await conn.execute(insert(s.memories).values(**row))
            return row

    async def forget(self, memory_id: str):
        async with self.store.transaction() as conn:
            result = await conn.execute(
                update(s.memories)
                .where(
                    s.memories.c.id == memory_id,
                    s.memories.c.profile_id == self.store.profile_id,
                    s.memories.c.active == 1,
                )
                .values(active=0)
            )
            if result.rowcount != 1:
                raise DomainError("memory_not_found", "Active memory is not available")

    async def list_memories(self, limit=50):
        if not 1 <= limit <= 100:
            raise DomainError("invalid_limit", "Memory page size must be 1-100")
        async with self.store.engine.connect() as conn:
            rows = await conn.execute(
                select(s.memories)
                .where(s.memories.c.profile_id == self.store.profile_id, s.memories.c.active == 1)
                .order_by(s.memories.c.created_at.desc(), s.memories.c.id.desc())
                .limit(limit)
            )
            return [dict(row) for row in rows.mappings()]

    async def snapshot(self):
        skills = await self.list_skills()
        memories = (await self.list_memories(limit=20))[::-1]
        if len(skills) > 8 or sum(len(row["body"]) for row in skills) > 12_000:
            raise DomainError("skill_budget", "Active skills exceed the session prompt budget")
        if sum(len(row["content"]) for row in memories) > 4000:
            raise DomainError("memory_budget", "Recent memories exceed the session prompt budget")
        return {
            "skills": [
                {
                    key: row[key]
                    for key in ("name", "description", "body", "version", "content_hash")
                }
                for row in skills
            ],
            "memories": [
                {key: row[key] for key in ("id", "content", "source", "content_hash")}
                for row in memories
            ],
        }

    async def search(self, query: str, limit=20):
        if not query.strip() or len(query) > 200 or not 1 <= limit <= 50:
            raise DomainError(
                "invalid_search", "Memory search needs 1-200 characters and 1-50 results"
            )
        phrase = '"' + query.strip().replace('"', '""') + '"'
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self.store.engine.connect() as conn:
            rows = await conn.exec_driver_sql(
                "SELECT m.id, m.content, m.source FROM memories_fts "
                "JOIN memories AS m ON m.rowid = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND memories_fts.profile_id = ? AND m.active = 1 "
                "UNION SELECT m.id, m.content, m.source FROM memories AS m "
                "WHERE m.profile_id = ? AND m.active = 1 AND m.content LIKE ? ESCAPE '\\' "
                "LIMIT ?",
                (phrase, self.store.profile_id, self.store.profile_id, f"%{escaped}%", limit),
            )
            return [dict(row._mapping) for row in rows]


def render_prompt(base: str, snapshot: dict):
    pieces = [base]
    for skill in snapshot["skills"]:
        pieces.append(f"Skill {skill['name']} ({skill['description']}):\n{skill['body']}")
    if snapshot["memories"]:
        pieces.append(
            "Profile memories frozen for this conversation:\n"
            + "\n".join(f"- {row['content']}" for row in snapshot["memories"])
        )
    return "\n\n".join(pieces)
