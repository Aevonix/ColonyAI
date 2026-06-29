"""Full-state backup and restore for Colony.

Creates a compressed, optionally encrypted archive of all Colony state:
- All SQLite databases in COLONY_STATE_DIR (auto-discovered)
- Identity files (colony-id, keys, genesis)
- Config files (scrubbed of secrets)
- LanceDB vector store
- Neo4j graph export (Cypher, best-effort)

Usage::

    # Backup
    colony backup --full --output ~/backups/

    # Restore
    colony restore --full colony-backup-20260627T120000.tar.gz
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

BACKUP_VERSION = 1

_SECRET_KEY_PATTERN = re.compile(
    r"(_KEY|_SECRET|_PASSWORD|_TOKEN|_PASSPHRASE)$", re.IGNORECASE
)


# ── Backup ───────────────────────────────────────────────────────────────


def create_full_backup(
    state_dir: str | Path,
    output_dir: str | Path,
    *,
    passphrase: Optional[bytes] = None,
    include_graph: bool = True,
    include_vectors: bool = True,
    include_host_paths: Optional[list[str]] = None,
) -> Path:
    """Create a full backup archive of Colony state.

    Returns the path to the created archive.
    """
    state_dir = Path(state_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    colony_id = _read_colony_id(state_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"colony-backup-{timestamp}"

    with tempfile.TemporaryDirectory(prefix="colony-backup-") as tmp:
        staging = Path(tmp) / base_name
        staging.mkdir()

        db_manifest = _snapshot_databases(state_dir, staging / "databases")
        _snapshot_identity(state_dir, staging / "identity")
        _snapshot_config(state_dir, staging / "config")

        if include_vectors:
            _snapshot_vectors(state_dir, staging / "vector")

        graph_status = "skipped"
        if include_graph:
            graph_status = _snapshot_graph(state_dir, staging / "graph")

        if include_host_paths:
            _snapshot_host_state(include_host_paths, staging / "host")

        meta = {
            "backup_version": BACKUP_VERSION,
            "colony_version": _get_colony_version(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "colony_id": colony_id,
            "colony_id_hmac": _compute_identity_hmac(colony_id),
            "database_manifest": db_manifest,
            "graph_status": graph_status,
            "encrypted": passphrase is not None,
        }
        (staging / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n"
        )

        archive_path = output_dir / f"{base_name}.tar.gz"
        _create_archive(staging, archive_path)

        if passphrase is not None:
            encrypted_path = output_dir / f"{base_name}.tar.gz.enc"
            _encrypt_file(archive_path, encrypted_path, passphrase)
            archive_path.unlink()
            archive_path = encrypted_path

    logger.info("Full backup created: %s", archive_path)
    return archive_path


# ── Restore ──────────────────────────────────────────────────────────────


def restore_full_backup(
    archive_path: str | Path,
    state_dir: str | Path,
    *,
    passphrase: Optional[bytes] = None,
    force_identity: bool = False,
) -> dict[str, Any]:
    """Restore Colony state from a full backup archive.

    Returns a summary dict of what was restored.
    """
    archive_path = Path(archive_path)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="colony-restore-") as tmp:
        staging = Path(tmp) / "restore"
        staging.mkdir()

        actual_archive = archive_path
        if archive_path.suffix == ".enc":
            if passphrase is None:
                raise ValueError("Archive is encrypted; passphrase required")
            actual_archive = Path(tmp) / "decrypted.tar.gz"
            _decrypt_file(archive_path, actual_archive, passphrase)

        _extract_archive(actual_archive, staging)

        root = _find_backup_root(staging)
        meta = json.loads((root / "meta.json").read_text())

        if meta.get("backup_version", 0) > BACKUP_VERSION:
            raise ValueError(
                f"Backup version {meta['backup_version']} is newer than "
                f"this Colony supports (max {BACKUP_VERSION})"
            )

        existing_id = _read_colony_id(state_dir)
        backup_id = meta.get("colony_id", "")

        if existing_id and existing_id != backup_id and not force_identity:
            raise ValueError(
                f"Backup is from colony {backup_id} but this instance is "
                f"{existing_id}. Use --force-identity to override."
            )

        summary: dict[str, Any] = {"colony_id": backup_id, "databases": [], "errors": []}

        identity_dir = root / "identity"
        if identity_dir.is_dir():
            _restore_directory(identity_dir, state_dir)
            summary["identity"] = True

        db_dir = root / "databases"
        if db_dir.is_dir():
            for db_file in db_dir.glob("*.db"):
                dest = state_dir / db_file.name
                shutil.copy2(db_file, dest)
                summary["databases"].append(db_file.name)
                logger.info("Restored database: %s", db_file.name)

        config_dir = root / "config"
        if config_dir.is_dir():
            _restore_directory(config_dir, state_dir)
            summary["config"] = True

        vector_dir = root / "vector"
        if vector_dir.is_dir():
            dest = state_dir / "lancedb"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(vector_dir / "lancedb", dest, dirs_exist_ok=True)
            summary["vectors"] = True

        graph_dir = root / "graph"
        if graph_dir.is_dir():
            marker = graph_dir / "SKIPPED.txt"
            if not marker.exists():
                summary["graph_export"] = str(graph_dir / "neo4j-dump.cypher")

    logger.info("Restore complete: %s", summary)
    return summary


# ── Database snapshot ────────────────────────────────────────────────────


def _snapshot_databases(
    state_dir: Path, dest: Path,
) -> list[dict[str, str]]:
    """Snapshot all .db files using VACUUM INTO for consistency."""
    dest.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []

    for db_path in sorted(state_dir.glob("*.db")):
        snap_path = dest / db_path.name
        try:
            conn = sqlite3.connect(str(db_path))
            conn.execute(f"VACUUM INTO '{snap_path}'")
            conn.close()
            manifest.append({
                "filename": db_path.name,
                "size_bytes": str(snap_path.stat().st_size),
            })
            logger.info("Snapshotted database: %s", db_path.name)
        except Exception as exc:
            logger.warning(
                "Failed to snapshot %s, falling back to copy: %s",
                db_path.name, exc,
            )
            try:
                shutil.copy2(db_path, snap_path)
                manifest.append({
                    "filename": db_path.name,
                    "size_bytes": str(snap_path.stat().st_size),
                    "method": "copy",
                })
            except Exception as exc2:
                logger.error("Failed to copy %s: %s", db_path.name, exc2)

    return manifest


# ── Identity snapshot ────────────────────────────────────────────────────


def _snapshot_identity(state_dir: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    identity_files = ["colony-id", "genesis.json"]
    keys_dir = state_dir / "colony-keys"

    for f in identity_files:
        src = state_dir / f
        if src.exists():
            shutil.copy2(src, dest / f)

    if keys_dir.is_dir():
        dest_keys = dest / "colony-keys"
        dest_keys.mkdir(exist_ok=True)
        for key_file in keys_dir.iterdir():
            shutil.copy2(key_file, dest_keys / key_file.name)


# ── Config snapshot (with secret scrubbing) ──────────────────────────────


def _snapshot_config(state_dir: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    config_files = [
        ".env", "channels.json", ".colony-llm-config.json",
        "standing_approvals.json",
    ]

    for name in config_files:
        src = state_dir / name
        if not src.exists():
            src = state_dir.parent / name
        if src.exists():
            if name == ".env":
                scrubbed = _scrub_env_file(src.read_text())
                (dest / name).write_text(scrubbed)
            else:
                shutil.copy2(src, dest / name)


def _scrub_env_file(content: str) -> str:
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if "=" in stripped:
            key, _, _ = stripped.partition("=")
            if _SECRET_KEY_PATTERN.search(key.strip()):
                lines.append(f"{key.strip()}=<REDACTED>")
                continue
        lines.append(line)
    return "\n".join(lines) + "\n"


# ── Vector store snapshot ────────────────────────────────────────────────


def _snapshot_vectors(state_dir: Path, dest: Path) -> None:
    lance_dir = state_dir / "lancedb"
    if not lance_dir.is_dir():
        return
    dest_lance = dest / "lancedb"
    shutil.copytree(lance_dir, dest_lance)
    logger.info("Snapshotted LanceDB directory")


# ── Graph export ─────────────────────────────────────────────────────────


def _snapshot_graph(state_dir: Path, dest: Path) -> str:
    """Export Neo4j graph as Cypher. Returns status string."""
    dest.mkdir(parents=True, exist_ok=True)

    neo4j_uri = os.environ.get("NEO4J_URI", "")
    if not neo4j_uri:
        (dest / "SKIPPED.txt").write_text(
            "Neo4j not configured (NEO4J_URI not set)\n"
        )
        return "skipped_not_configured"

    try:
        from neo4j import GraphDatabase

        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_pass = os.environ.get("NEO4J_PASSWORD", "")

        driver = GraphDatabase.driver(
            neo4j_uri, auth=(neo4j_user, neo4j_pass)
        )

        cypher_stmts: list[str] = []
        with driver.session() as session:
            nodes = session.run("MATCH (n) RETURN n")
            for record in nodes:
                node = record["n"]
                labels = ":".join(node.labels)
                props = json.dumps(dict(node), default=str)
                cypher_stmts.append(
                    f"CREATE (:{labels} {props});"
                )

            rels = session.run(
                "MATCH (a)-[r]->(b) "
                "RETURN id(a) as a_id, type(r) as rel_type, "
                "properties(r) as props, id(b) as b_id"
            )
            for record in rels:
                props = json.dumps(record["props"], default=str)
                cypher_stmts.append(
                    f"// REL: ({record['a_id']})-[:{record['rel_type']} {props}]->({record['b_id']})"
                )

        driver.close()

        output = dest / "neo4j-dump.cypher"
        output.write_text("\n".join(cypher_stmts) + "\n")
        logger.info("Exported %d graph statements", len(cypher_stmts))
        return "exported"

    except ImportError:
        (dest / "SKIPPED.txt").write_text("neo4j driver not installed\n")
        return "skipped_no_driver"
    except Exception as exc:
        logger.warning("Neo4j export failed: %s", exc)
        (dest / "SKIPPED.txt").write_text(f"Export failed: {exc}\n")
        return "skipped_error"


# ── Host state ───────────────────────────────────────────────────────────


def _snapshot_host_state(paths: list[str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for p in paths:
        src = Path(p).expanduser()
        if src.is_file():
            shutil.copy2(src, dest / src.name)
        elif src.is_dir():
            shutil.copytree(src, dest / src.name, dirs_exist_ok=True)


# ── Archive creation / extraction ────────────────────────────────────────


def _create_archive(source_dir: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)


def _extract_archive(archive_path: Path, dest: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(dest, filter="data")


def _find_backup_root(staging: Path) -> Path:
    """Find the root directory inside the extracted archive."""
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


# ── Encryption (AES-256-GCM) ────────────────────────────────────────────


def _encrypt_file(
    src: Path, dest: Path, passphrase: bytes,
) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    salt = os.urandom(16)
    kdf = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1)
    key = kdf.derive(passphrase)

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    plaintext = src.read_bytes()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    with dest.open("wb") as f:
        f.write(b"COLONY_ENC_V1\n")
        f.write(salt)
        f.write(nonce)
        f.write(ciphertext)


def _decrypt_file(
    src: Path, dest: Path, passphrase: bytes,
) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    data = src.read_bytes()
    header = b"COLONY_ENC_V1\n"
    if not data.startswith(header):
        raise ValueError("Not a Colony encrypted backup (invalid header)")

    offset = len(header)
    salt = data[offset:offset + 16]
    nonce = data[offset + 16:offset + 28]
    ciphertext = data[offset + 28:]

    kdf = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1)
    key = kdf.derive(passphrase)

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception:
        raise ValueError("Decryption failed -- wrong passphrase?")

    dest.write_bytes(plaintext)


# ── Helpers ──────────────────────────────────────────────────────────────


def _read_colony_id(state_dir: Path) -> str:
    id_path = state_dir / "colony-id"
    if id_path.exists():
        return id_path.read_text().strip()
    return ""


def _compute_identity_hmac(colony_id: str) -> str:
    return hmac.new(
        colony_id.encode(), b"colony-backup-binding", hashlib.sha256
    ).hexdigest()


def _get_colony_version() -> str:
    try:
        from importlib.metadata import version
        return version("colonyai")
    except Exception:
        return "unknown"


def _restore_directory(src: Path, dest: Path) -> None:
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
