"""Tests for the full-state backup and restore system."""

import json
import sqlite3
from pathlib import Path

import pytest

from colony_sidecar.backup import (
    create_full_backup,
    restore_full_backup,
    _scrub_env_file,
    _snapshot_databases,
    _read_colony_id,
    BACKUP_VERSION,
)


@pytest.fixture
def colony_state(tmp_path):
    """Create a minimal Colony state directory for testing."""
    state = tmp_path / "state"
    state.mkdir()

    (state / "colony-id").write_text("test-colony-abc123")

    keys = state / "colony-keys"
    keys.mkdir()
    (keys / "private.pem").write_text("FAKE_PRIVATE_KEY")
    (keys / "public.pem").write_text("FAKE_PUBLIC_KEY")

    (state / "genesis.json").write_text(json.dumps({"genesis": True}))

    conn = sqlite3.connect(str(state / "colony-contacts.db"))
    conn.execute("CREATE TABLE contacts (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO contacts VALUES ('c1', 'Alice')")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(state / "colony-affect.db"))
    conn.execute("CREATE TABLE affect (id TEXT PRIMARY KEY, mood TEXT)")
    conn.commit()
    conn.close()

    (state / ".env").write_text(
        "COLONY_API_KEY=super-secret-key\n"
        "NEO4J_URI=bolt://localhost:7687\n"
        "NEO4J_PASSWORD=neo4j-secret\n"
        "COLONY_SIDECAR_PORT=7777\n"
    )

    return state


@pytest.fixture
def output_dir(tmp_path):
    return tmp_path / "backups"


# ── Secret scrubbing ────────────────────────────────────────────────────


class TestSecretScrubbing:
    def test_scrubs_key_values(self):
        env = "API_KEY=abc123\nNORMAL=value\nDB_PASSWORD=secret\n"
        result = _scrub_env_file(env)
        assert "API_KEY=<REDACTED>" in result
        assert "NORMAL=value" in result
        assert "DB_PASSWORD=<REDACTED>" in result
        assert "abc123" not in result
        assert "secret" not in result

    def test_preserves_comments(self):
        env = "# This is a comment\nAPI_KEY=secret\n"
        result = _scrub_env_file(env)
        assert "# This is a comment" in result

    def test_preserves_blank_lines(self):
        env = "A=1\n\nB=2\n"
        result = _scrub_env_file(env)
        assert "\n\n" in result

    def test_scrubs_token(self):
        env = "CHANNEL_TOKEN=tok_abc\n"
        result = _scrub_env_file(env)
        assert "CHANNEL_TOKEN=<REDACTED>" in result


# ── Database snapshot ────────────────────────────────────────────────────


class TestDatabaseSnapshot:
    def test_snapshots_all_dbs(self, colony_state, tmp_path):
        dest = tmp_path / "db_snap"
        manifest = _snapshot_databases(colony_state, dest)
        filenames = {m["filename"] for m in manifest}
        assert "colony-contacts.db" in filenames
        assert "colony-affect.db" in filenames

    def test_snapshot_is_consistent_copy(self, colony_state, tmp_path):
        dest = tmp_path / "db_snap"
        _snapshot_databases(colony_state, dest)
        conn = sqlite3.connect(str(dest / "colony-contacts.db"))
        cur = conn.execute("SELECT name FROM contacts WHERE id = 'c1'")
        assert cur.fetchone()[0] == "Alice"
        conn.close()


# ── Full backup/restore cycle ────────────────────────────────────────────


class TestFullCycle:
    def test_backup_creates_archive(self, colony_state, output_dir):
        archive = create_full_backup(
            colony_state, output_dir,
            include_graph=False, include_vectors=False,
        )
        assert archive.exists()
        assert archive.suffix == ".gz"

    def test_restore_recovers_state(self, colony_state, output_dir, tmp_path):
        archive = create_full_backup(
            colony_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        summary = restore_full_backup(archive, restore_dir)

        assert summary["colony_id"] == "test-colony-abc123"
        assert "colony-contacts.db" in summary["databases"]
        assert (restore_dir / "colony-id").read_text().strip() == "test-colony-abc123"
        assert (restore_dir / "colony-keys" / "public.pem").exists()

        conn = sqlite3.connect(str(restore_dir / "colony-contacts.db"))
        cur = conn.execute("SELECT name FROM contacts WHERE id = 'c1'")
        assert cur.fetchone()[0] == "Alice"
        conn.close()

    def test_restore_rejects_identity_mismatch(self, colony_state, output_dir, tmp_path):
        archive = create_full_backup(
            colony_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "other"
        restore_dir.mkdir()
        (restore_dir / "colony-id").write_text("different-colony-xyz")

        with pytest.raises(ValueError, match="force-identity"):
            restore_full_backup(archive, restore_dir)

    def test_restore_with_force_identity(self, colony_state, output_dir, tmp_path):
        archive = create_full_backup(
            colony_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "other"
        restore_dir.mkdir()
        (restore_dir / "colony-id").write_text("different-colony-xyz")

        summary = restore_full_backup(
            archive, restore_dir, force_identity=True,
        )
        assert summary["colony_id"] == "test-colony-abc123"

    def test_env_file_is_scrubbed_in_backup(self, colony_state, output_dir, tmp_path):
        archive = create_full_backup(
            colony_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        restore_full_backup(archive, restore_dir)

        env_content = (restore_dir / ".env").read_text()
        assert "super-secret-key" not in env_content
        assert "neo4j-secret" not in env_content
        assert "COLONY_API_KEY=<REDACTED>" in env_content
        assert "COLONY_SIDECAR_PORT=7777" in env_content

    def test_meta_json_in_archive(self, colony_state, output_dir, tmp_path):
        archive = create_full_backup(
            colony_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        import tarfile
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert any("meta.json" in n for n in names)


# ── Encryption ───────────────────────────────────────────────────────────


class TestEncryption:
    def test_encrypted_backup_restore(self, colony_state, output_dir, tmp_path):
        passphrase = b"test-passphrase-123"
        archive = create_full_backup(
            colony_state, output_dir,
            passphrase=passphrase,
            include_graph=False, include_vectors=False,
        )
        assert archive.suffix == ".enc"

        restore_dir = tmp_path / "restored"
        summary = restore_full_backup(
            archive, restore_dir, passphrase=passphrase,
        )
        assert summary["colony_id"] == "test-colony-abc123"
        assert "colony-contacts.db" in summary["databases"]

    def test_wrong_passphrase_fails(self, colony_state, output_dir, tmp_path):
        archive = create_full_backup(
            colony_state, output_dir,
            passphrase=b"correct-pass",
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="wrong passphrase"):
            restore_full_backup(
                archive, restore_dir, passphrase=b"wrong-pass",
            )

    def test_encrypted_without_passphrase_fails(self, colony_state, output_dir, tmp_path):
        archive = create_full_backup(
            colony_state, output_dir,
            passphrase=b"correct-pass",
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="passphrase required"):
            restore_full_backup(archive, restore_dir)
