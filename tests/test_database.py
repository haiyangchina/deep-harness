"""The Profile store initializes empty directories and validates existing databases."""

import sqlite3
from contextlib import closing

import pytest

from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.database import SCHEMA_VERSION
from deep_harness.infrastructure.store import BusinessStore


async def test_current_database_is_initialized_and_reopened(tmp_path):
    for _ in range(2):
        store = BusinessStore(tmp_path, "profile")
        try:
            await store.open()
        finally:
            await store.close()
    with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


async def test_unsupported_database_is_rejected_without_changes(tmp_path):
    path = tmp_path / "business.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE existing_data (value TEXT)")
        conn.execute("INSERT INTO existing_data VALUES ('keep')")
        conn.commit()
    content = path.read_bytes()
    store = BusinessStore(tmp_path, "profile")
    try:
        with pytest.raises(DomainError, match="new state directory"):
            await store.open()
    finally:
        await store.close()
    assert path.read_bytes() == content


async def test_current_database_with_missing_table_is_rejected(tmp_path):
    store = BusinessStore(tmp_path, "profile")
    try:
        await store.open()
    finally:
        await store.close()
    with closing(sqlite3.connect(tmp_path / "business.db")) as conn:
        conn.execute("DROP TABLE approval_decisions")
        conn.commit()
    reopened = BusinessStore(tmp_path, "profile")
    try:
        with pytest.raises(DomainError) as error:
            await reopened.open()
        assert error.value.code == "schema_unrecognized"
    finally:
        await reopened.close()
