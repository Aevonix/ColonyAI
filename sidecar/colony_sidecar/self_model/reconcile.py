"""Offline, rollback-first competence reconciliation CLI.

Dry-run is the default and operates on a temporary SQLite backup, so merely
validating a legacy database cannot run schema migrations against it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from colony_sidecar.self_model.store import CompetenceStore, event_fingerprint


def _parse_ts(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()


def sqlite_backup(source: Path, destination: Path) -> str:
    """Create one consistent SQLite backup; never overwrite a prior backup."""
    if not source.is_file():
        raise ValueError(f"database does not exist: {source}")
    if destination.exists():
        raise ValueError(f"backup already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(db_path: Path, domain: str, since: float,
                     until: float) -> Dict[str, Any]:
    """Read legacy or current raw events without migrating the source DB."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM competence_events WHERE domain=? "
            "AND ts>=? AND ts<? ORDER BY ts ASC, id ASC",
            ((domain or "").strip().lower(), since, until)).fetchall()]
        try:
            ledger_count = int(conn.execute(
                "SELECT COUNT(*) FROM competence_reconciliations"
            ).fetchone()[0])
        except sqlite3.OperationalError:
            ledger_count = 0
    finally:
        conn.close()
    candidates: List[Dict[str, Any]] = []
    for row in rows:
        candidates.append({
            "event_id": int(row["id"]),
            "target_fingerprint": event_fingerprint(row),
            "domain": row["domain"],
            "recorded_outcome": row["outcome"],
            "shadow": bool(row.get("shadow")),
            "violation": bool(row.get("violation")),
            "stated_confidence": row.get("stated_confidence"),
            "source": row.get("source") or "legacy_unattributed",
            "source_ref": row.get("source_ref"),
            "evidence_status": (
                row.get("evidence_status") or "legacy_unattributed"),
            "outcome_contract": (
                row.get("outcome_contract") or "legacy.unversioned"),
            "ts": float(row["ts"]),
        })
    return {
        "database": str(db_path), "domain": domain,
        "since_ts": since, "until_ts": until,
        "candidate_count": len(candidates), "candidates": candidates,
        "ledger_entries": ledger_count, "mutated": False,
    }


def _load_manifest(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest root must be an object")
    return value


def dry_run(db_path: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Validate against a consistent disposable copy of the source DB."""
    with tempfile.TemporaryDirectory(prefix="colony-competence-dryrun-") as td:
        copy_path = Path(td) / "self-model.db"
        sqlite_backup(db_path, copy_path)
        store = CompetenceStore(str(copy_path))
        try:
            result = store.plan_reconciliation(manifest)
        finally:
            store.close()
    return {**result, "mode": "dry-run", "database_mutated": False}


def commit(db_path: Path, manifest: Dict[str, Any],
           backup_path: Path) -> Dict[str, Any]:
    """Back up first, then atomically append the manifest."""
    # Validate on a disposable consistent copy before opening (and therefore
    # migrating) the source database.
    dry_run(db_path, manifest)
    backup_sha256 = sqlite_backup(db_path, backup_path)
    store = CompetenceStore(str(db_path))
    try:
        result = store.apply_reconciliation(manifest)
    finally:
        store.close()
    return {**result, "mode": "commit", "database_mutated": True,
            "backup": str(backup_path), "backup_sha256": backup_sha256}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or reconcile Colony competence evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="list exact raw event targets")
    inspect.add_argument("--db", required=True, type=Path)
    inspect.add_argument("--domain", required=True)
    inspect.add_argument("--since", required=True, type=_parse_ts)
    inspect.add_argument("--until", required=True, type=_parse_ts)
    apply = sub.add_parser("apply", help="validate/apply a v1 manifest")
    apply.add_argument("--db", required=True, type=Path)
    apply.add_argument("--manifest", required=True, type=Path)
    apply.add_argument(
        "--commit", action="store_true",
        help="append after a backup (otherwise uses a disposable dry-run)")
    apply.add_argument(
        "--backup", type=Path,
        help="new backup path; required with --commit and never overwritten")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            if args.since >= args.until:
                raise ValueError("--since must be earlier than --until")
            result = inspect_database(
                args.db, args.domain, args.since, args.until)
        else:
            manifest = _load_manifest(args.manifest)
            if args.commit:
                if args.backup is None:
                    raise ValueError("--backup is required with --commit")
                result = commit(args.db, manifest, args.backup)
            else:
                if args.backup is not None:
                    raise ValueError("--backup is only valid with --commit")
                result = dry_run(args.db, manifest)
    except (OSError, TypeError, ValueError, sqlite3.Error,
            json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True),
              file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
