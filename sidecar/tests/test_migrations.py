"""Tests for the generic schema migration runner."""

import sqlite3
import textwrap
from pathlib import Path

import pytest

from colony_sidecar.migrations import (
    applied_versions_sync,
    run_migrations_sync,
    _discover,
)


@pytest.fixture
def migrations_dir(tmp_path):
    d = tmp_path / "migrations"
    d.mkdir()
    return d


def _write_migration(d: Path, name: str, sql: str) -> None:
    (d / name).write_text(textwrap.dedent(sql))


# ── Discovery ────────────────────────────────────────────────────────────


def test_discover_sorts_by_numeric_prefix(migrations_dir):
    _write_migration(migrations_dir, "003_third.sql", "SELECT 1;")
    _write_migration(migrations_dir, "001_first.sql", "SELECT 1;")
    _write_migration(migrations_dir, "002_second.sql", "SELECT 1;")
    (migrations_dir / "not_a_migration.txt").write_text("ignored")
    (migrations_dir / "readme.md").write_text("ignored")

    result = _discover(migrations_dir)
    assert [v for v, _ in result] == ["001", "002", "003"]


def test_discover_empty_dir(tmp_path):
    assert _discover(tmp_path / "nonexistent") == []


# ── Sync runner ──────────────────────────────────────────────────────────


def test_applies_migrations_in_order(migrations_dir):
    _write_migration(migrations_dir, "001_create.sql", """\
        CREATE TABLE items (id TEXT PRIMARY KEY, name TEXT);
    """)
    _write_migration(migrations_dir, "002_add_col.sql", """\
        ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'active';
    """)

    conn = sqlite3.connect(":memory:")
    applied = run_migrations_sync(conn, migrations_dir)

    assert applied == ["001", "002"]
    cur = conn.execute("PRAGMA table_info(items)")
    cols = {row[1] for row in cur.fetchall()}
    assert "status" in cols
    conn.close()


def test_idempotent_rerun(migrations_dir):
    _write_migration(migrations_dir, "001_create.sql", """\
        CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY);
    """)

    conn = sqlite3.connect(":memory:")
    first = run_migrations_sync(conn, migrations_dir)
    second = run_migrations_sync(conn, migrations_dir)

    assert first == ["001"]
    assert second == []
    conn.close()


def test_tracks_applied_versions(migrations_dir):
    _write_migration(migrations_dir, "001_create.sql", """\
        CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY);
    """)

    conn = sqlite3.connect(":memory:")
    run_migrations_sync(conn, migrations_dir)
    versions = applied_versions_sync(conn)

    assert versions == {"001"}
    conn.close()


def test_applied_versions_no_table():
    conn = sqlite3.connect(":memory:")
    assert applied_versions_sync(conn) == set()
    conn.close()


def test_new_migration_applied_after_existing(migrations_dir):
    _write_migration(migrations_dir, "001_create.sql", """\
        CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY);
    """)

    conn = sqlite3.connect(":memory:")
    run_migrations_sync(conn, migrations_dir)

    _write_migration(migrations_dir, "002_add_col.sql", """\
        ALTER TABLE items ADD COLUMN name TEXT;
    """)
    applied = run_migrations_sync(conn, migrations_dir)

    assert applied == ["002"]
    assert applied_versions_sync(conn) == {"001", "002"}
    conn.close()


def test_failed_migration_does_not_record_version(migrations_dir):
    _write_migration(migrations_dir, "001_create.sql", """\
        CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY);
    """)
    _write_migration(migrations_dir, "002_bad.sql", """\
        THIS IS NOT VALID SQL;
    """)

    conn = sqlite3.connect(":memory:")
    with pytest.raises(Exception):
        run_migrations_sync(conn, migrations_dir)

    assert applied_versions_sync(conn) == {"001"}
    conn.close()


def test_custom_table_name(migrations_dir):
    _write_migration(migrations_dir, "001_create.sql", """\
        CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY);
    """)

    conn = sqlite3.connect(":memory:")
    run_migrations_sync(conn, migrations_dir, table="my_versions")

    cur = conn.execute("SELECT version FROM my_versions")
    assert cur.fetchone()[0] == "001"

    # default table should not exist
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT 1 FROM schema_version")
    conn.close()


def test_table_rebuild_migration(migrations_dir):
    _write_migration(migrations_dir, "001_create.sql", """\
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            gateway TEXT NOT NULL CHECK(gateway IN ('sms','email'))
        );
        INSERT INTO items VALUES ('a', 'sms');
    """)
    _write_migration(migrations_dir, "002_open_enum.sql", """\
        CREATE TABLE items_new (
            id TEXT PRIMARY KEY,
            gateway TEXT NOT NULL
        );
        INSERT INTO items_new SELECT * FROM items;
        DROP TABLE items;
        ALTER TABLE items_new RENAME TO items;
    """)

    conn = sqlite3.connect(":memory:")
    run_migrations_sync(conn, migrations_dir)

    # CHECK constraint removed -- arbitrary gateways now work
    conn.execute("INSERT INTO items VALUES ('b', 'whatsapp-custom')")
    cur = conn.execute("SELECT gateway FROM items ORDER BY id")
    assert [row[0] for row in cur.fetchall()] == ["sms", "whatsapp-custom"]
    conn.close()
