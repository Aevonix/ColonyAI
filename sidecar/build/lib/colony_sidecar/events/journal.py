"""Append-only event journal for Colony.

Every event broadcast via the WebSocket bus is also persisted to a
file-per-event journal under ``{state_dir}/events/``.  Disconnected
clients can replay missed events via ``GET /v1/host/events/replay``.

Each event is written as a sequentially-numbered JSON file with an
atomic write (write-to-temp + rename) to avoid corruption on crash.
Retention is bounded: files with seq < (max_seq - retention) are
pruned on append.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    return Path(os.environ.get("COLONY_STATE_DIR", ".")).resolve()


def _journal_dir() -> Path:
    d = Path(os.environ.get("COLONY_EVENT_JOURNAL_DIR", "")).resolve()
    if not d or d == Path(".").resolve():
        d = _state_dir() / "events"
    return d


def _retention() -> int:
    try:
        return int(os.environ.get("COLONY_EVENT_JOURNAL_RETENTION", "500"))
    except ValueError:
        return 500


def _format_seq(seq: int) -> str:
    return str(seq).zfill(6)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: words + punctuation."""
    return len(text.split())


def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp file then rename — avoids partial writes on crash."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        # Clean up temp file if rename fails
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append_event(event_type: str, data: Dict[str, Any]) -> int:
    """Append an event to the journal. Returns the sequence number.

    Args:
        event_type: Canonical event type string (e.g. "memory.consolidated").
        data: Event-specific payload.

    Returns:
        The assigned sequence number, or -1 on failure.
    """
    journal_dir = _journal_dir()
    try:
        journal_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Cannot create journal dir %s", journal_dir, exc_info=True)
        return -1

    # Determine next sequence number
    try:
        existing = sorted(journal_dir.glob("*.json"))
        if existing:
            last_seq = max(
                int(f.name.split(".")[0]) for f in existing if f.name.split(".")[0].isdigit()
            )
            seq = last_seq + 1
        else:
            seq = 1
    except Exception:
        logger.warning("Failed to scan journal dir", exc_info=True)
        seq = 1

    ulid = uuid.uuid4().hex[:26]  # Cheap ULID-like identifier
    recorded_at = datetime.now(timezone.utc).isoformat()

    event_payload = {
        "type": event_type,
        "recordedAt": recorded_at,
        "data": data,
    }

    contents = json.dumps(event_payload, ensure_ascii=False) + "\n"
    checksum = _checksum(contents)
    payload_with_checksum = json.dumps(
        {**event_payload, "checksum": checksum}, ensure_ascii=False
    ) + "\n"

    filename = f"{_format_seq(seq)}.{ulid}.json"
    filepath = journal_dir / filename

    try:
        _atomic_write(filepath, payload_with_checksum)
    except Exception:
        logger.warning("Failed to write journal event %s", filename, exc_info=True)
        return -1

    # Prune old events
    _prune_events(seq, _retention())

    return seq


def _prune_events(current_seq: int, keep: int) -> None:
    """Delete journal files with seq < (current_seq - keep)."""
    if current_seq <= keep:
        return

    journal_dir = _journal_dir()
    cutoff = current_seq - keep + 1  # Keep events from cutoff onward (inclusive)

    try:
        for f in journal_dir.glob("*.json"):
            parts = f.name.split(".")
            if parts and parts[0].isdigit():
                seq = int(parts[0])
                if seq < cutoff:
                    try:
                        f.unlink()
                    except OSError:
                        pass
    except Exception:
        logger.debug("Prune scan failed", exc_info=True)


def replay_events(
    since: str,
    limit: int = 500,
    types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Replay journal events recorded after ``since`` (ISO 8601 timestamp).

    Args:
        since: ISO 8601 timestamp — return events recorded after this time.
        limit: Maximum number of events to return.
        types: Optional list of event type strings to filter by.

    Returns:
        Dict with "events" list, "lastSeq", and "hasMore".
    """
    journal_dir = _journal_dir()

    if not journal_dir.exists():
        return {"events": [], "lastSeq": 0, "hasMore": False}

    events: List[Dict[str, Any]] = []

    try:
        files = sorted(journal_dir.glob("*.json"))
    except OSError:
        return {"events": [], "lastSeq": 0, "hasMore": False}

    for f in files:
        if len(events) >= limit:
            # Check if there are more files after this
            remaining = files[files.index(f):]
            has_more = any(_file_is_after(fn, since) for fn in remaining[1:])
            return {
                "events": events,
                "lastSeq": events[-1]["seq"] if events else 0,
                "hasMore": has_more,
            }

        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        recorded_at = raw.get("recordedAt", "")
        if recorded_at <= since:
            continue

        event_type = raw.get("type", "unknown")
        if types and event_type not in types:
            continue

        parts = f.name.split(".")
        seq = int(parts[0]) if parts and parts[0].isdigit() else 0
        ulid = parts[1].replace(".json", "") if len(parts) > 1 else ""

        events.append({
            "seq": seq,
            "ulid": ulid,
            "type": event_type,
            "recordedAt": recorded_at,
            "data": raw.get("data", {}),
        })

    return {
        "events": events,
        "lastSeq": events[-1]["seq"] if events else 0,
        "hasMore": False,
    }


def _file_is_after(filepath: Path, since: str) -> bool:
    """Quick check if a journal file contains an event after ``since``."""
    try:
        raw = json.loads(filepath.read_text(encoding="utf-8"))
        return raw.get("recordedAt", "") > since
    except Exception:
        return False
