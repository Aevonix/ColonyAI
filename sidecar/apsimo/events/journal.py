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
import re
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
_EVENT_KEY_DIRNAME = ".event-keys"
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


def _event_key_path(journal_dir: Path, event_key: str) -> Path:
    return journal_dir / _EVENT_KEY_DIRNAME / _event_key_digest(event_key)


def _event_key_request_digest(
    event_type: str,
    data: Dict[str, Any],
    occurred_at: Optional[str],
) -> str:
    return _checksum(json.dumps(
        {
            "type": event_type,
            "data": data,
            "occurredAt": occurred_at or "",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ))


def event_record_request_digest(
    event_type: str,
    data: Dict[str, Any],
    occurred_at: Optional[str],
) -> str:
    """Return the immutable keyed-append request identity for a caller."""

    return _event_key_request_digest(event_type, data, occurred_at)


def _event_key_digest(event_key: str) -> str:
    return hashlib.sha256(event_key.encode("utf-8")).hexdigest()


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
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            marker_digest = str(raw.get("eventKeyDigest") or "")
            if re.fullmatch(r"[a-f0-9]{64}", marker_digest):
                by_seq[seq].append(f"@event-key:{marker_digest}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
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
                if filename.startswith("@event-key:"):
                    marker_digest = filename.removeprefix("@event-key:")
                    if not re.fullmatch(r"[a-f0-9]{64}", marker_digest):
                        logger.warning(
                            "Ignoring unsafe event-key index entry %r", filename,
                        )
                        continue
                    if not prune_ok:
                        # The event file remains. Its marker was changed to
                        # pruning, so keyed replay cannot duplicate it while a
                        # later retention pass retries deletion.
                        continue
                    marker_path = journal_dir / _EVENT_KEY_DIRNAME / marker_digest
                    try:
                        marker = json.loads(
                            marker_path.read_text(encoding="utf-8")
                        )
                    except FileNotFoundError:
                        continue
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        logger.warning(
                            "Failed to read journal event-key marker %s",
                            marker_path, exc_info=True,
                        )
                        prune_ok = False
                        continue
                    # The inbox receipt is the only authority allowed to
                    # acknowledge this key. Retention removes event content,
                    # but preserves fixed-size projection metadata so an
                    # interrupted inbox finalization cannot resurrect it.
                    marker["state"] = "pruned"
                    marker.pop("filename", None)
                    try:
                        _atomic_write(
                            marker_path,
                            json.dumps(
                                marker,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ) + "\n",
                        )
                    except OSError:
                        logger.warning(
                            "Failed to retain journal event-key tombstone %s",
                            marker_path, exc_info=True,
                        )
                        prune_ok = False
                    continue
                # Index contents are internal, but still constrain deletion to
                # a basename beneath the journal directory.
                if Path(filename).name != filename:
                    logger.warning("Ignoring unsafe journal index entry %r", filename)
                    needs_fallback = True
                    continue
                marker_path: Optional[Path] = None
                event_path = journal_dir / filename
                try:
                    raw = json.loads(event_path.read_text(encoding="utf-8"))
                    marker_digest = str(raw.get("eventKeyDigest") or "")
                    if re.fullmatch(r"[a-f0-9]{64}", marker_digest):
                        marker_path = (
                            journal_dir / _EVENT_KEY_DIRNAME / marker_digest
                        )
                        if marker_path.exists():
                            marker = json.loads(
                                marker_path.read_text(encoding="utf-8")
                            )
                            marker["state"] = "pruning"
                            _atomic_write(
                                marker_path,
                                json.dumps(
                                    marker,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ) + "\n",
                            )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    marker_path = None
                try:
                    event_path.unlink()
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
    event_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Durably append an event and return its canonical journal record.

    ``event_key`` enables a crash-recoverable exactly-once append. Its marker
    is written after sequence reservation but before the event file, so a
    retry either returns or finishes the same canonical record.

    ``None`` means the event was not made durable and therefore must not be
    published to the live stream.  Sequence gaps are intentionally permitted
    after an interrupted or failed write; sequence reuse is not.
    """
    journal_dir = _journal_dir()
    try:
        with _journal_lock(journal_dir):
            normalized_key = str(event_key or "").strip()
            if len(normalized_key) > 256:
                raise ValueError("event_key exceeds 256 characters")
            request_digest = (
                _event_key_request_digest(event_type, data, occurred_at)
                if normalized_key else ""
            )
            marker_path = (
                _event_key_path(journal_dir, normalized_key)
                if normalized_key else None
            )
            last_seq, pruned_through = _load_cursor(journal_dir)
            if marker_path is not None and marker_path.exists():
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                key_digest = _event_key_digest(normalized_key)
                if (
                    marker.get("eventKeyDigest") != key_digest
                    or marker.get("requestDigest") != request_digest
                ):
                    raise ValueError("immutable event_key replay mismatch")
                seq = int(marker.get("seq") or 0)
                recorded_at = str(marker.get("recordedAt") or "")
                event_id = str(marker.get("ulid") or "")
                state = str(marker.get("state") or "")
                if not seq or not recorded_at or not event_id:
                    raise ValueError("event_key marker metadata is invalid")
                if state in {"pruning", "pruned"} or seq <= pruned_through:
                    # Never reconstruct event content after retention has
                    # started. The request digest above proves this is the same
                    # immutable request; only its original durable metadata is
                    # needed to truthfully finish the inbox transaction.
                    if state != "pruned":
                        marker["state"] = "pruned"
                        marker.pop("filename", None)
                        _atomic_write(
                            marker_path,
                            json.dumps(
                                marker,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ) + "\n",
                        )
                    return {
                        "seq": seq,
                        "ulid": event_id,
                        "recordedAt": recorded_at,
                        "retained": False,
                    }
                if state != "active":
                    raise ValueError("event_key marker state is invalid")
                filename = str(marker.get("filename") or "")
                if (
                    Path(filename).name != filename
                    or not filename or not recorded_at or not event_id
                ):
                    raise ValueError("event_key marker filename is invalid")
                event_payload = {
                    "seq": seq,
                    "ulid": event_id,
                    "type": event_type,
                    "recordedAt": recorded_at,
                    "occurredAt": occurred_at or recorded_at,
                    "data": data,
                    "eventKeyDigest": key_digest,
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
                event_path = journal_dir / filename
                if event_path.exists():
                    persisted = json.loads(event_path.read_text(encoding="utf-8"))
                    if (
                        not _valid_record_checksum(persisted)
                        or {
                            key: value for key, value in persisted.items()
                            if key != "checksum"
                        } != event_payload
                    ):
                        raise ValueError("event_key record is corrupt")
                else:
                    _write_index_entry(
                        journal_dir,
                        int(event_payload["seq"]),
                        [filename, f"@event-key:{key_digest}"],
                    )
                    _atomic_write(event_path, payload_with_checksum)
                return event_payload

            seq = last_seq + 1

            # Reserve the sequence before writing the record.  A crash can burn
            # a number, but cannot allow a later event to reuse it.
            _write_cursor(journal_dir, seq, pruned_through)

            event_id = (
                hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()[:26]
                if normalized_key else uuid.uuid4().hex[:26]
            )
            recorded_at = datetime.now(timezone.utc).isoformat()
            event_payload: Dict[str, Any] = {
                "seq": seq,
                "ulid": event_id,
                "type": event_type,
                "recordedAt": recorded_at,
                "occurredAt": occurred_at or recorded_at,
                "data": data,
            }
            if normalized_key:
                event_payload["eventKeyDigest"] = _event_key_digest(
                    normalized_key
                )
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
            if marker_path is not None:
                _atomic_write(
                    marker_path,
                    json.dumps(
                        {
                            "state": "active",
                            "eventKeyDigest": _event_key_digest(normalized_key),
                            "requestDigest": request_digest,
                            "filename": filename,
                            "seq": seq,
                            "ulid": event_id,
                            "recordedAt": recorded_at,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ) + "\n",
                )
            # Write the prune pointer first.  If the process dies before the
            # record write, a later prune sees a harmless pointer to no file.
            index_filenames = [filename]
            if normalized_key:
                index_filenames.append(
                    f"@event-key:{_event_key_digest(normalized_key)}"
                )
            _write_index_entry(journal_dir, seq, index_filenames)
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


def acknowledge_event_record(
    event_key: str,
    *,
    expected_seq: Optional[int] = None,
    expected_event_id: Optional[str] = None,
    expected_recorded_at: Optional[str] = None,
    expected_request_digest: Optional[str] = None,
) -> bool:
    """Release a keyed projection marker after its owning receipt commits.

    The key itself is never persisted. A missing marker is already
    acknowledged, making this safe to retry after a process restart.
    """
    journal_dir = _journal_dir()
    try:
        normalized_key = str(event_key or "").strip()
        if not normalized_key or len(normalized_key) > 256:
            raise ValueError("event_key is invalid")
        expected_values = (
            expected_seq,
            expected_event_id,
            expected_recorded_at,
            expected_request_digest,
        )
        if any(value is not None for value in expected_values) and not all(
            value is not None for value in expected_values
        ):
            raise ValueError("expected journal projection receipt is incomplete")
        if expected_seq is not None and (
            type(expected_seq) is not int or expected_seq < 1
        ):
            raise ValueError("expected journal sequence is invalid")
        if expected_event_id is not None and (
            type(expected_event_id) is not str
            or not expected_event_id
            or expected_event_id != expected_event_id.strip()
        ):
            raise ValueError("expected journal event ID is invalid")
        if expected_recorded_at is not None and (
            type(expected_recorded_at) is not str
            or not expected_recorded_at
            or expected_recorded_at != expected_recorded_at.strip()
        ):
            raise ValueError("expected journal recorded time is invalid")
        if expected_request_digest is not None and not re.fullmatch(
            r"[a-f0-9]{64}", expected_request_digest,
        ):
            raise ValueError("expected journal request digest is invalid")
        with _journal_lock(journal_dir):
            marker_path = _event_key_path(journal_dir, normalized_key)
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return True
            if marker.get("eventKeyDigest") != _event_key_digest(normalized_key):
                raise ValueError("event_key marker digest mismatch")
            if expected_seq is not None and (
                type(marker.get("seq")) is not int
                or marker.get("seq") != expected_seq
                or marker.get("ulid") != expected_event_id
                or marker.get("recordedAt") != expected_recorded_at
                or marker.get("requestDigest") != expected_request_digest
            ):
                raise ValueError("event_key marker projection receipt mismatch")
            marker_path.unlink()
            _fsync_directory(marker_path.parent)
            return True
    except Exception:
        logger.error("Failed to acknowledge journal event key", exc_info=True)
        return False


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

            # Validate the complete retained generation before applying query
            # filters or pagination.  A page boundary must never hide a second
            # file for an already-returned sequence (or a file whose embedded
            # identity differs from its filename): cursor consumers would skip
            # that record forever on their next ``after_seq`` replay.
            sequence_counts: Dict[int, int] = {}
            for seq, _path in files:
                sequence_counts[seq] = sequence_counts.get(seq, 0) + 1
            duplicate_sequences = {
                seq for seq, count in sequence_counts.items() if count > 1
            }
            corrupt_count = sum(
                count - 1 for count in sequence_counts.values() if count > 1
            )
            validated: list[tuple[int, Path, Dict[str, Any]]] = []
            for seq, path in files:
                if seq in duplicate_sequences:
                    # Neither file is authoritative when one sequence has more
                    # than one durable identity.  Report the collision and
                    # withhold the ambiguous records from every page.
                    continue
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    corrupt_count += 1
                    continue
                if not isinstance(raw, dict) or not _valid_record_checksum(raw):
                    corrupt_count += 1
                    continue
                raw_sequence = raw.get("seq")
                if raw_sequence is not None and (
                    type(raw_sequence) is not int or raw_sequence != seq
                ):
                    corrupt_count += 1
                    continue
                parts = path.name.split(".")
                filename_event_id = parts[1] if len(parts) > 2 else ""
                raw_event_id = raw.get("ulid")
                if raw_event_id not in (None, "") and (
                    type(raw_event_id) is not str
                    or not filename_event_id
                    or raw_event_id != filename_event_id
                ):
                    corrupt_count += 1
                    continue
                validated.append((seq, path, raw))

            ordered = (
                list(reversed(validated)) if newest_first else validated
            )
            events: List[Dict[str, Any]] = []
            has_more = False
            for seq, path, raw in ordered:
                if after_seq is not None and seq <= after_seq:
                    continue
                if until_seq is not None and seq > until_seq:
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
