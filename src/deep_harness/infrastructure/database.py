"""Initialize or validate the current business database under the Profile owner lock."""

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


async def validate_tables(conn, tables, required):
    if not required.issubset(tables):
        raise DomainError("schema_unrecognized", "Database is not a supported deep-harness schema")
    for name in sorted(required):
        columns = {row[1] for row in await conn.exec_driver_sql(f"PRAGMA table_info({name})")}
        expected = set(s.metadata.tables[name].columns.keys())
        if not expected.issubset(columns):
            raise DomainError("schema_unrecognized", f"Database table {name} is incomplete")


async def initialize_database(engine):
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
            if version != 0 or tables:
                raise DomainError(
                    "schema_unsupported", "Use a new state directory for this release"
                )
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
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                "USING fts5(profile_id UNINDEXED, memory_id UNINDEXED, content)"
            )
            await conn.exec_driver_sql(
                "CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories "
                "BEGIN INSERT INTO memories_fts(rowid, profile_id, memory_id, content) "
                "VALUES (new.rowid, new.profile_id, new.id, new.content); END"
            )
            for target in HOT_INDEXES:
                name = target.replace("(", "_").replace(", ", "_").removesuffix(")")
                await conn.exec_driver_sql(
                    f"CREATE INDEX IF NOT EXISTS ix_harness_{name} ON {target}"
                )
            await conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
