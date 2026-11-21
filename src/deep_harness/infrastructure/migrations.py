"""Versioned SQLite upgrades under the profile owner lock; graph schema stays SDK-owned."""

import asyncio
import os
import sqlite3
from contextlib import closing
from uuid import uuid4

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure import schema as s

SCHEMA_VERSION = 12
HOT_INDEXES = (
    "events(session_id, seq)",
    "model_receipts(admission_id)",
    "effects(admission_id)",
    "admissions(session_id, status, ordinal)",
    "messages(admission_id, ordinal)",
    "messages(session_id, ordinal)",
    "interactions(admission_id, decision)",
)
BASE_TABLES = {
    "sessions",
    "admissions",
    "messages",
    "events",
    "effects",
    "interactions",
    "model_receipts",
}


async def validate_tables(conn, tables, required, *, legacy=False):
    if not required.issubset(tables):
        raise DomainError("schema_unrecognized", "Database is not a supported deep-harness schema")
    for name in sorted(required):
        columns = {row[1] for row in await conn.exec_driver_sql(f"PRAGMA table_info({name})")}
        expected = set(s.metadata.tables[name].columns.keys())
        if legacy and name == "admissions":
            expected.discard("active_seconds")
        if legacy and name == "sessions":
            expected.discard("harness")
            expected.difference_update({"permission_mode", "permission_revision"})
        if not expected.issubset(columns):
            raise DomainError("schema_unrecognized", f"Database table {name} is incomplete")


def backup_database(home, version):
    directory = home / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"business-before-v{version}-{uuid4().hex}.db"
    partial = target.with_suffix(".partial")
    try:
        with (
            closing(sqlite3.connect(home / "business.db")) as source,
            closing(sqlite3.connect(partial)) as destination,
        ):
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DomainError("backup_invalid", "Pre-upgrade database backup failed validation")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


async def migrate(engine, home):
    async with engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            version = (await conn.exec_driver_sql("PRAGMA user_version")).scalar_one()
            if version > SCHEMA_VERSION:
                raise DomainError(
                    "schema_too_new", "Database requires a newer deep-harness version"
                )
            tables = set(
                (
                    await conn.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                ).scalars()
            )
            if version == SCHEMA_VERSION:
                await validate_tables(conn, tables, set(s.metadata.tables))
                if not {"messages_fts", "memories_fts"}.issubset(tables):
                    raise DomainError("schema_unrecognized", "Search indexes are missing")
                await conn.commit()
                return
            if tables:
                await validate_tables(conn, tables, BASE_TABLES, legacy=True)
                await asyncio.to_thread(backup_database, home, SCHEMA_VERSION)
                columns = {
                    row[1] for row in await conn.exec_driver_sql("PRAGMA table_info(sessions)")
                }
                if "harness" not in columns:
                    await conn.exec_driver_sql(
                        "ALTER TABLE sessions ADD COLUMN harness TEXT NOT NULL DEFAULT 'general'"
                    )
                for name, definition in {
                    "permission_mode": "TEXT NOT NULL DEFAULT 'legacy'",
                    "permission_revision": "INTEGER NOT NULL DEFAULT 0",
                }.items():
                    if name not in columns:
                        await conn.exec_driver_sql(
                            f"ALTER TABLE sessions ADD COLUMN {name} {definition}"
                        )
                admission_columns = {
                    row[1] for row in await conn.exec_driver_sql("PRAGMA table_info(admissions)")
                }
                if "active_seconds" not in admission_columns:
                    await conn.exec_driver_sql(
                        "ALTER TABLE admissions ADD COLUMN active_seconds REAL NOT NULL DEFAULT 0"
                    )
                if "context_heads" in tables:
                    context_columns = {
                        row[1]
                        for row in await conn.exec_driver_sql("PRAGMA table_info(context_heads)")
                    }
                    if "summary_version" not in context_columns:
                        await conn.exec_driver_sql(
                            "ALTER TABLE context_heads ADD COLUMN summary_version INTEGER "
                            "NOT NULL DEFAULT 0"
                        )
                if "delegations" in tables:
                    delegation_columns = {
                        row[1]
                        for row in await conn.exec_driver_sql("PRAGMA table_info(delegations)")
                    }
                    additions = {
                        "mode": "TEXT NOT NULL DEFAULT 'sync'",
                        "child_session_id": "TEXT",
                        "child_admission_id": "TEXT",
                        "stop_requested": "INTEGER NOT NULL DEFAULT 0",
                    }
                    for name, definition in additions.items():
                        if name not in delegation_columns:
                            await conn.exec_driver_sql(
                                f"ALTER TABLE delegations ADD COLUMN {name} {definition}"
                            )
            # v1 registers the original unversioned schema; v2 adds immutable policies and lineage.
            await conn.run_sync(s.metadata.create_all)
            await conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                "USING fts5(session_id UNINDEXED, message_id UNINDEXED, content)"
            )
            await conn.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages "
                "BEGIN INSERT INTO messages_fts(rowid, session_id, message_id, content) "
                "VALUES (new.ordinal, new.session_id, new.id, "
                "json_extract(new.payload, '$.content')); END"
            )
            await conn.exec_driver_sql(
                "INSERT OR REPLACE INTO messages_fts(rowid, session_id, message_id, content) "
                "SELECT ordinal, session_id, id, json_extract(payload, '$.content') "
                "FROM messages"
            )
            await conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                "USING fts5(profile_id UNINDEXED, memory_id UNINDEXED, content)"
            )
            await conn.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories "
                "BEGIN INSERT INTO memories_fts(rowid, profile_id, memory_id, content) "
                "VALUES (new.rowid, new.profile_id, new.id, new.content); END"
            )
            await conn.exec_driver_sql(
                "INSERT OR REPLACE INTO memories_fts(rowid, profile_id, memory_id, content) "
                "SELECT rowid, profile_id, id, content FROM memories"
            )
            for target in HOT_INDEXES:
                name = target.replace("(", "_").replace(", ", "_").removesuffix(")")
                await conn.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS ix_harness_{name} ON {target}"
                )
            if version < 11:
                await conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO approval_decisions(interaction_id, generation) "
                    "SELECT i.id, w.generation FROM approval_waits w "
                    "JOIN json_each(w.interaction_ids) j "
                    "JOIN interactions i ON i.id=j.value AND i.admission_id=w.admission_id "
                    "WHERE i.decision IS NOT NULL"
                )
            if version < 10:
                await conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO tool_attempts(admission_id, call_id) "
                    "SELECT a.id, substr(e.id, length(a.id) + 2) FROM effects e "
                    "JOIN admissions a ON e.admission_id = a.id "
                    "UNION SELECT a.id, d.parent_tool_call_id FROM delegations d "
                    "JOIN admissions a ON d.admission_id = a.id "
                    "UNION SELECT a.id, json_extract(m.payload, '$.tool_call_id') "
                    "FROM messages m JOIN admissions a ON m.admission_id = a.id "
                    "WHERE json_extract(m.payload, '$.role') = 'tool' "
                    "AND json_extract(m.payload, '$.tool_call_id') IS NOT NULL"
                )
                await conn.exec_driver_sql(
                    "INSERT OR IGNORE INTO approval_revocations(admission_id) "
                    "SELECT json_extract(payload, '$.admission_id') FROM events "
                    "WHERE type='approval.reuse_revoked' "
                    "AND json_extract(payload, '$.admission_id') IS NOT NULL"
                )
            await conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
