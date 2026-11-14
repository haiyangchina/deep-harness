"""Upgrade preserves legacy data, backs up before mutation and refuses future versions."""

import sqlite3
from contextlib import closing

import pytest

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.migrations import SCHEMA_VERSION
from deep_harness.infrastructure.store import BusinessStore


async def legacy_database(home):
    store = BusinessStore(home, "default")
    await store.open()
    try:
        session = await store.create_session(
            principal="operator", request_id="old", prompt="old prompt", tools=()
        )
        await store.admit(
            session, principal="operator", submission_id="old-input", text="preserve me"
        )
    finally:
        await store.close()
    with closing(sqlite3.connect(home / "business.db")) as conn:
        conn.execute("DROP TABLE session_policies")
        conn.execute("DROP TABLE session_branches")
        conn.execute("PRAGMA user_version=0")
        conn.commit()
    return session


async def test_legacy_upgrade_is_backed_up_preserves_data_and_is_idempotent(tmp_path):
    session = await legacy_database(tmp_path)
    upgraded = BusinessStore(tmp_path, "default")
    try:
        await upgraded.open()
        assert (await upgraded.get_session(session.id, "operator")).prompt == "old prompt"
        assert (await upgraded.history(session.id))[0]["content"] == "preserve me"
        backups = list((tmp_path / "backups").glob("*.db"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
            assert backup.execute("SELECT text FROM admissions").fetchone()[0] == "preserve me"
            assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        await upgraded.close()
        await upgraded.open()
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
        with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        await upgraded.close()


@pytest.mark.parametrize("future", [False, True])
async def test_failed_backup_or_future_schema_never_performs_upgrade(tmp_path, monkeypatch, future):
    from deep_harness.infrastructure import migrations

    await legacy_database(tmp_path)
    if future:
        with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
            conn.execute("PRAGMA user_version=99")
    else:

        def fail_backup(home, version):
            raise OSError("backup destination unavailable")

        monkeypatch.setattr(migrations, "backup_database", fail_backup)
    store = BusinessStore(tmp_path, "default")
    try:
        with pytest.raises(DomainError if future else OSError):
            await store.open()
        with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (99 if future else 0)
            assert conn.execute("SELECT text FROM admissions").fetchone()[0] == "preserve me"
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='session_policies'"
                ).fetchone()
                is None
            )
    finally:
        await store.close()


async def test_partial_migration_rolls_back_ddl_and_preserves_pre_upgrade_backup(
    tmp_path, monkeypatch
):
    from deep_harness.infrastructure import schema

    await legacy_database(tmp_path)

    def fail_migration(connection):
        connection.exec_driver_sql("CREATE TABLE partially_upgraded (value TEXT)")
        raise RuntimeError("injected migration interruption")

    monkeypatch.setattr(schema.metadata, "create_all", fail_migration)
    store = BusinessStore(tmp_path, "default")
    try:
        with pytest.raises(RuntimeError, match="migration interruption"):
            await store.open()
        with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
            assert (
                conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='partially_upgraded'"
                ).fetchone()
                is None
            )
            assert conn.execute("SELECT text FROM admissions").fetchone()[0] == "preserve me"
        assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    finally:
        await store.close()


async def test_current_version_with_missing_tables_is_rejected_not_silently_repaired(tmp_path):
    await legacy_database(tmp_path)
    with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    store = BusinessStore(tmp_path, "default")
    try:
        with pytest.raises(DomainError, match="supported deep-harness schema"):
            await store.open()
    finally:
        await store.close()


async def test_v3_upgrade_backfills_existing_message_search(tmp_path):
    store = BusinessStore(tmp_path, "default")
    await store.open()
    session = await store.create_session(
        principal="operator", request_id="old", prompt="prompt", tools=()
    )
    await store.admit(session, principal="operator", submission_id="one", text="searchable seed")
    await store.close()
    with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
        conn.execute("DROP TRIGGER messages_fts_insert")
        conn.execute("DROP TABLE messages_fts")
        conn.execute("PRAGMA user_version=3")
        conn.commit()
    await store.open()
    try:
        with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'searchable'"
                ).fetchone()[0]
                == 1
            )
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        await store.close()


async def test_v6_upgrade_adds_general_harness_without_changing_sessions(tmp_path):
    store = BusinessStore(tmp_path, "default")
    await store.open()
    session = await store.create_session(
        principal="operator", request_id="old", prompt="prompt", tools=()
    )
    await store.close()
    with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN harness")
        conn.execute("PRAGMA user_version=6")
        conn.commit()
    await store.open()
    try:
        assert (await store.get_session(session.id, "operator")).harness == "general"
        with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        await store.close()


async def test_v7_upgrade_adds_background_delegation_columns(tmp_path):
    store = BusinessStore(tmp_path, "default")
    await store.open()
    await store.close()
    with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE delegations")
        conn.execute(
            "CREATE TABLE delegations (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "admission_id TEXT NOT NULL, parent_tool_call_id TEXT NOT NULL, "
            "kind TEXT NOT NULL, task_hash TEXT NOT NULL, status TEXT NOT NULL, "
            "result TEXT, FOREIGN KEY(session_id) REFERENCES sessions(id), "
            "UNIQUE(admission_id, parent_tool_call_id))"
        )
        conn.execute("PRAGMA user_version=7")
        conn.commit()
    await store.open()
    try:
        with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(delegations)")}
            assert {"mode", "child_session_id", "child_admission_id", "stop_requested"} <= columns
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        await store.close()
