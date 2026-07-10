"""Durable append-only event journal for Colony.

Every externally visible host event is persisted before it is offered to the
live WebSocket stream.  Events remain file-per-record for compatibility with
existing operators and recovery tooling.  A small cursor plus a per-sequence
prune index make the steady-state append path constant-time; a directory scan
is only needed when migrating or repairing missing cursor metadata.

Sequence allocation is protected by both an in-process lock and ``flock`` so
threads and sidecar processes sharing a state directory cannot allocate the
same sequence.  The cursor is advanced before the event file is written.  A
crash may therefore leave a sequence gap, but can never cause a sequence to be
reused or a live event to precede its durable record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:  # Colony is deployed on Linux; retain a safe process-local fallback.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_CURSOR_FILENAME = ".cursor"
_LOCK_FILENAME = ".lock"
_INDEX_DIRNAME = ".sequence-index"
_PROCESS_LOCK = threading.RLock()


def _state_dir() -> Path:
    return Path(os.environ.get("COLONY_STATE_DIR", ".")).resolve()


def _journal_dir() -> Path:
    configured = os.environ.get("COLONY_EVENT_JOURNAL_DIR", "").strip()
    return Path(configured).resolve() if configured else _state_dir() / "events"


def _retention() -> int:
    try:
        return max(1, int(os.environ.get("COLONY_EVENT_JOURNAL_RETENTION", "500")))
    except ValueError:
        return 500


def _format_seq(seq: int) -> str:
    return str(seq).zfill(6)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate retained for callers of the legacy helper."""
    return len(text.split())


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory fsync so a rename survives a host crash."""
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path: Path, content: str) -> None:
    """Durably replace ``path`` without exposing a partial record.

    The temporary filename is unique.  The previous implementation used one
    shared ``.tmp`` name, which allowed concurrent appenders to overwrite one
    another before rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd: Optional[int] = None
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # fdopen owns it now
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_record_checksum(raw: Dict[str, Any]) -> bool:
    """Verify new canonical and legacy journal checksums.

    Very early/manual journal files had no checksum, so absence remains a
    compatibility case. A present but incorrect checksum is corruption.
    """
    expected = raw.get("checksum")
    if not expected:
        return True
    unsigned = {key: value for key, value in raw.items() if key != "checksum"}
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    if _checksum(canonical) == expected:
        return True
    legacy = json.dumps(unsigned, ensure_ascii=False) + "\n"
    return _checksum(legacy) == expected


@contextmanager
def _journal_lock(journal_dir: Path) -> Iterator[None]:
    """Serialize cursor allocation and journal mutation across processes."""
    journal_dir.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        with (journal_dir / _LOCK_FILENAME).open("a+b") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _event_file_sequence(path: Path) -> Optional[int]:
    prefix = path.name.split(".", 1)[0]
    return int(prefix) if prefix.isdigit() else None


def _event_files(journal_dir: Path) -> list[tuple[int, Path]]:
    records: list[tuple[int, Path]] = []
    for path in journal_dir.glob("*.json"):
        seq = _event_file_sequence(path)
        if seq is not None:
            records.append((seq, path))
    records.sort(key=lambda item: (item[0], item[1].name))
    return records


def _cursor_path(journal_dir: Path) -> Path:
    return journal_dir / _CURSOR_FILENAME


def _index_dir(journal_dir: Path) -> Path:
    return journal_dir / _INDEX_DIRNAME


def _write_cursor(journal_dir: Path, last_seq: int, pruned_through: int) -> None:
    payload = json.dumps(
        {"lastSeq": last_seq, "prunedThrough": pruned_through},
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"
    _atomic_write(_cursor_path(journal_dir), payload)


def _write_index_entry(journal_dir: Path, seq: int, filenames: list[str]) -> None:
    payload = json.dumps(filenames, separators=(",", ":")) + "\n"
    _atomic_write(_index_dir(journal_dir) / _format_seq(seq), payload)


def _rebuild_cursor(journal_dir: Path) -> tuple[int, int]:
    """One-time migration/repair scan for legacy file-only journals."""
    files = _event_files(journal_dir)
    by_seq: dict[int, list[str]] = {}
    for seq, path in files:
        by_seq.setdefault(seq, []).append(path.name)
    for seq, filenames in by_seq.items():
        _write_index_entry(journal_dir, seq, filenames)

    last_seq = files[-1][0] if files else 0
    pruned_through = (files[0][0] - 1) if files else 0
    _write_cursor(journal_dir, last_seq, pruned_through)
    return last_seq, pruned_through


def _load_cursor(journal_dir: Path) -> tuple[int, int]:
    try:
        raw = json.loads(_cursor_path(journal_dir).read_text(encoding="utf-8"))
        last_seq = int(raw["lastSeq"])
        pruned_through = int(raw.get("prunedThrough", 0))
        # Legacy test/development journals may contain sequence zero, whose
        # natural predecessor is -1. Newly allocated production sequences
        # still begin at one.
        if last_seq < 0 or pruned_through < -1 or pruned_through > last_seq:
            raise ValueError("invalid journal cursor")
        return last_seq, pruned_through
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        logger.info("Rebuilding event-journal cursor from existing records")
        return _rebuild_cursor(journal_dir)


def _read_index_entry(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, list):
            return [str(item) for item in value]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return []


def _prune_indexed_events(
    journal_dir: Path,
    current_seq: int,
    keep: int,
    pruned_through: int,
) -> int:
    """Prune expired sequences without scanning the journal in steady state."""
    cutoff = current_seq - max(1, keep)
    if cutoff <= pruned_through:
        return pruned_through

    index_dir = _index_dir(journal_dir)
    for seq in range(pruned_through + 1, cutoff + 1):
        index_path = index_dir / _format_seq(seq)
        filenames = _read_index_entry(index_path)
        prune_ok = True
        needs_fallback = not filenames
        if filenames:
            for filename in filenames:
                # Index contents are internal, but still constrain deletion to
                # a basename beneath the journal directory.
                if Path(filename).name != filename:
                    logger.warning("Ignoring unsafe journal index entry %r", filename)
                    needs_fallback = True
                    continue
                try:
                    (journal_dir / filename).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("Failed to prune journal record %s", filename,
                                   exc_info=True)
                    prune_ok = False
        if needs_fallback:
            # Exceptional repair path for an interrupted legacy migration.
            for path in journal_dir.glob(f"{_format_seq(seq)}.*.json"):
                try:
                    path.unlink()
                except OSError:
                    logger.warning("Failed to prune journal record %s", path,
                                   exc_info=True)
                    prune_ok = False
        if not prune_ok:
            # Keep the cursor immediately before the failed sequence so a
            # later append retries instead of silently abandoning retention.
            return seq - 1
        try:
            index_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Failed to remove journal index %s", index_path,
                           exc_info=True)
            return seq - 1
    return cutoff


def append_event_record(
    event_type: str,
    data: Dict[str, Any],
    *,
    occurred_at: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Durably append an event and return its canonical journal record.

    ``None`` means the event was not made durable and therefore must not be
    published to the live stream.  Sequence gaps are intentionally permitted
    after an interrupted or failed write; sequence reuse is not.
    """
    journal_dir = _journal_dir()
    try:
        with _journal_lock(journal_dir):
            last_seq, pruned_through = _load_cursor(journal_dir)
            seq = last_seq + 1

            # Reserve the sequence before writing the record.  A crash can burn
            # a number, but cannot allow a later event to reuse it.
            _write_cursor(journal_dir, seq, pruned_through)

            event_id = uuid.uuid4().hex[:26]
            recorded_at = datetime.now(timezone.utc).isoformat()
            event_payload: Dict[str, Any] = {
                "seq": seq,
                "ulid": event_id,
                "type": event_type,
                "recordedAt": recorded_at,
                "occurredAt": occurred_at or recorded_at,
                "data": data,
            }
            unsigned = json.dumps(
                event_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            payload_with_checksum = json.dumps(
                {**event_payload, "checksum": _checksum(unsigned)},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"

            filename = f"{_format_seq(seq)}.{event_id}.json"
            # Write the prune pointer first.  If the process dies before the
            # record write, a later prune sees a harmless pointer to no file.
            _write_index_entry(journal_dir, seq, [filename])
            _atomic_write(journal_dir / filename, payload_with_checksum)

            new_pruned_through = _prune_indexed_events(
                journal_dir, seq, _retention(), pruned_through
            )
            if new_pruned_through != pruned_through:
                _write_cursor(journal_dir, seq, new_pruned_through)
            return event_payload
    except Exception:
        logger.error("Failed to append event %s to journal", event_type, exc_info=True)
        return None


def append_event(event_type: str, data: Dict[str, Any]) -> int:
    """Compatibility wrapper returning the assigned sequence or ``-1``."""
    record = append_event_record(event_type, data)
    return int(record["seq"]) if record is not None else -1


def current_sequence() -> int:
    """Return the journal's durable high-water sequence."""
    journal_dir = _journal_dir()
    try:
        with _journal_lock(journal_dir):
            last_seq, _ = _load_cursor(journal_dir)
            return last_seq
    except Exception:
        logger.error("Failed to read event-journal cursor", exc_info=True)
        return 0


def _prune_events(current_seq: int, keep: int) -> None:
    """Compatibility helper used by older maintenance code and tests."""
    journal_dir = _journal_dir()
    try:
        with _journal_lock(journal_dir):
            last_seq, pruned_through = _load_cursor(journal_dir)
            new_pruned = _prune_indexed_events(
                journal_dir, current_seq, keep, pruned_through
            )
            if new_pruned != pruned_through:
                _write_cursor(journal_dir, max(last_seq, current_seq), new_pruned)
    except Exception:
        logger.debug("Prune failed", exc_info=True)


def _parse_timestamp(value: str) -> Optional[datetime]:
    if not value or value == "0":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def replay_events(
    since: str = "",
    limit: int = 500,
    types: Optional[List[str]] = None,
    newest_first: bool = False,
    *,
    after_seq: Optional[int] = None,
    until_seq: Optional[int] = None,
) -> Dict[str, Any]:
    """Replay a consistent journal snapshot by sequence and/or timestamp.

    ``after_seq`` is the authoritative reconnect cursor when supplied.  The
    timestamp remains supported for older clients.  ``until_seq`` lets a
    WebSocket handshake replay to a captured high-water mark while live events
    accumulate in its bounded subscriber buffer.
    """
    journal_dir = _journal_dir()
    if not journal_dir.exists():
        return {
            "events": [], "lastSeq": 0, "hasMore": False,
            "firstAvailableSeq": 0, "journalLastSeq": 0,
        }

    since_dt = _parse_timestamp(since)
    limit = max(1, int(limit))
    event_types = set(types) if types else None

    try:
        with _journal_lock(journal_dir):
            journal_last_seq, _ = _load_cursor(journal_dir)
            files = _event_files(journal_dir)
            first_available = files[0][0] if files else 0
            ordered = list(reversed(files)) if newest_first else files

            events: List[Dict[str, Any]] = []
            has_more = False
            corrupt_count = 0
            for seq, path in ordered:
                if after_seq is not None and seq <= after_seq:
                    continue
                if until_seq is not None and seq > until_seq:
                    continue
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    corrupt_count += 1
                    continue
                if not isinstance(raw, dict) or not _valid_record_checksum(raw):
                    corrupt_count += 1
                    continue

                recorded_at = str(raw.get("recordedAt", ""))
                if after_seq is None and since_dt is not None:
                    recorded_dt = _parse_timestamp(recorded_at)
                    if recorded_dt is None or recorded_dt <= since_dt:
                        continue
                event_type = str(raw.get("type", "unknown"))
                if event_types and event_type not in event_types:
                    continue

                if len(events) >= limit:
                    has_more = True
                    break
                parts = path.name.split(".")
                event_id = str(raw.get("ulid") or (parts[1] if len(parts) > 2 else ""))
                events.append({
                    "seq": seq,
                    "ulid": event_id,
                    "type": event_type,
                    "recordedAt": recorded_at,
                    "occurredAt": raw.get("occurredAt", recorded_at),
                    "data": raw.get("data", {}),
                })

            return {
                "events": events,
                "lastSeq": events[-1]["seq"] if events else 0,
                "hasMore": has_more,
                "firstAvailableSeq": first_available,
                "journalLastSeq": journal_last_seq,
                "corruptCount": corrupt_count,
            }
    except OSError:
        logger.error("Failed to replay event journal", exc_info=True)
        return {
            "events": [], "lastSeq": 0, "hasMore": False,
            "firstAvailableSeq": 0, "journalLastSeq": 0,
            "replayError": "journal_unavailable",
        }


def _file_is_after(
    filepath: Path,
    since: str,
    types: Optional[List[str]] = None,
) -> bool:
    """Compatibility predicate for older callers and downstream extensions."""
    try:
        raw = json.loads(filepath.read_text(encoding="utf-8"))
        since_dt = _parse_timestamp(since)
        recorded_dt = _parse_timestamp(str(raw.get("recordedAt", "")))
        if since_dt is not None and (recorded_dt is None or recorded_dt <= since_dt):
            return False
        return not types or raw.get("type", "unknown") in types
    except Exception:
        return False
