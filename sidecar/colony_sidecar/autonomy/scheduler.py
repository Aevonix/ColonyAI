"""Durable periodic scheduler for Colony autonomy subsystems.

The schedules table remains compatible with the original scheduler. Additive
lease, attempt, and receipt state makes one due claim atomic across processes
and makes every terminal transition auditable without treating a callback
failure as a successful run.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import types
import uuid
from typing import Any, Callable, Coroutine, Dict, Iterator, List, Mapping, Optional, Union

logger = logging.getLogger(__name__)

_TASK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_INTERVAL_SECONDS = 31_536_000
_MAX_METADATA_FIELDS = 32
_MAX_METADATA_BYTES = 8192
_MAX_RESULT_NODES = 256
_MAX_RESULT_ITEMS = 64
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_TEXT_CHARS = 65_536
_MAX_RESULT_INTEGER_BITS = 4096
_MAX_RESULT_SUMMARY_CHARS = 2000
_MAX_RESULT_DESCRIPTOR_CHARS = 4096
_SCHEDULE_NAMESPACE = uuid.UUID("9fa9f892-fc4b-5ef4-aa25-b45d740d157c")
_TYPE_NAME_DESCRIPTOR = type.__dict__["__name__"]
_TYPE_MRO_DESCRIPTOR = type.__dict__["__mro__"]
_TYPE_DICT_DESCRIPTOR = type.__dict__["__dict__"]
_MAPPING_PROXY_TYPE = type(type.__dict__)
_MAPPING_PROXY_ITEMS = _MAPPING_PROXY_TYPE.__dict__["items"]
_GENERATOR_CODE_DESCRIPTOR = types.GeneratorType.__dict__["gi_code"]
_CODE_FLAGS_DESCRIPTOR = types.CodeType.__dict__["co_flags"]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("scheduler clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not _TASK_NAME.fullmatch(name):
        raise ValueError("task name must be 1-128 safe characters")
    return name


def _validate_interval(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_INTERVAL_SECONDS
    ):
        raise ValueError(
            f"interval_seconds must be an integer from 1-{_MAX_INTERVAL_SECONDS}"
        )
    return value


def _metadata_scalar(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError(f"metadata.{field} integer is outside bounds")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"metadata.{field} must be finite")
        return value
    if isinstance(value, str):
        normalized = " ".join(value.split()).strip()
        if len(normalized) > 500:
            raise ValueError(f"metadata.{field} exceeds 500 characters")
        return normalized
    raise ValueError(f"metadata.{field} must be a scalar or scalar array")


def _canonical_metadata(value: Optional[Mapping[str, Any]]) -> tuple[Dict[str, Any], str]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be one flat object")
    if len(value) > _MAX_METADATA_FIELDS:
        raise ValueError(f"metadata exceeds {_MAX_METADATA_FIELDS} fields")
    normalized: Dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not _METADATA_KEY.fullmatch(raw_key):
            raise ValueError("metadata contains an invalid key")
        if isinstance(raw_value, list):
            if len(raw_value) > 16:
                raise ValueError(f"metadata.{raw_key} exceeds 16 list items")
            normalized[raw_key] = [
                _metadata_scalar(item, field=raw_key) for item in raw_value
            ]
        else:
            normalized[raw_key] = _metadata_scalar(raw_value, field=raw_key)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError(f"metadata exceeds {_MAX_METADATA_BYTES} bytes")
    return normalized, encoded


def _safe_text(value: Any, maximum: int) -> str:
    """Bound exact primitive text without dispatching to user conversions."""
    value_type = type(value)
    if value_type is str:
        text = value
    elif value is None:
        text = "null"
    elif value_type is bool:
        text = "true" if value else "false"
    elif value_type is int and int.bit_length(value) <= _MAX_RESULT_INTEGER_BITS:
        text = json.dumps(value)
    elif value_type is float and math.isfinite(value):
        text = json.dumps(value, allow_nan=False)
    else:
        text = f"<{_safe_type_name(value)}>"
    bounded = text[:maximum]
    return bounded.encode("utf-8", "replace").decode("utf-8")


def _safe_type_name(value: Any) -> str:
    """Return a bounded type label without invoking a custom metaclass hook."""
    try:
        # Cache and call the built-in getset descriptor itself. Going through
        # either ``type(value).__name__`` or ``type.__getattribute__`` still
        # honors a hostile metaclass data descriptor named ``__name__``.
        name = _TYPE_NAME_DESCRIPTOR.__get__(type(value), type)
    except BaseException:
        return "unknown"
    if type(name) is not str:
        return "unknown"
    bounded = name[:128]
    cleaned = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "._-")
        else "_"
        for character in bounded
    )
    return cleaned or "unknown"


def _is_hook_free_awaitable(value: Any) -> bool:
    """Classify real awaitables without generic ABC/metaclass dispatch."""
    value_type = type(value)
    try:
        if value_type is types.GeneratorType:
            code = _GENERATOR_CODE_DESCRIPTOR.__get__(
                value, types.GeneratorType,
            )
            flags = _CODE_FLAGS_DESCRIPTOR.__get__(code, types.CodeType)
            if (
                type(flags) is int
                and flags & inspect.CO_ITERABLE_COROUTINE
            ):
                return True

        # Python's special-method lookup reads the actual MRO dictionaries;
        # it does not consult metaclass attributes. Reproduce that lookup via
        # cached built-in descriptors so hostile ``__mro__``/``__dict__``
        # properties and ABC ``__subclasshook__`` code never execute.
        hierarchy = _TYPE_MRO_DESCRIPTOR.__get__(value_type, type)
        if type(hierarchy) is not tuple:
            return False
        for base_index in range(tuple.__len__(hierarchy)):
            base = tuple.__getitem__(hierarchy, base_index)
            namespace = _TYPE_DICT_DESCRIPTOR.__get__(base, type)
            if type(namespace) is not _MAPPING_PROXY_TYPE:
                return False
            await_method = None
            found_await_method = False
            for key, method in _MAPPING_PROXY_ITEMS(namespace):
                # A metaclass can inject a ``str`` subclass as a namespace
                # key. It may compare equal to ``__await__`` regardless of its
                # visible contents, and continuing into a base would let the
                # eventual ``await`` execute that equality hook. Iterating the
                # built-in mapping proxy exposes key/value pairs without any
                # lookup or descriptor dispatch, so reject the whole MRO as
                # soon as any non-exact namespace key is observed.
                if type(key) is not str:
                    return False
                if key == "__await__":
                    found_await_method = True
                    await_method = method
            if found_await_method:
                # Match ``inspect.isawaitable`` compatibility: an exact first
                # MRO definition of ``None`` explicitly disables awaiting.
                return await_method is not None
    except BaseException:
        return False
    return False


class _BoundedText:
    """A small append-only text sink that never exceeds its character budget."""

    __slots__ = ("_maximum", "_parts", "_size", "truncated")

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._parts: List[str] = []
        self._size = 0
        self.truncated = False

    def add(self, value: str) -> None:
        remaining = self._maximum - self._size
        if remaining <= 0:
            self.truncated = True
            return
        if len(value) > remaining:
            self._parts.append(value[:remaining])
            self._size = self._maximum
            self.truncated = True
            return
        self._parts.append(value)
        self._size += len(value)

    def render(self) -> str:
        rendered = "".join(self._parts)
        if not self.truncated:
            return rendered
        if not rendered:
            return ""
        return rendered[:-1] + "…"


def _safe_json_clone(
    value: Any,
    *,
    depth: int,
    budget: Dict[str, int],
    active: set[int],
) -> tuple[bool, Any, str]:
    """Clone only exact built-in JSON values under fixed traversal budgets.

    Exact-type checks are intentional: a list/dict/string subclass may replace
    iteration, conversion, indexing, or length with arbitrary user code.
    """
    if budget["nodes"] <= 0 or depth > _MAX_RESULT_DEPTH:
        return False, None, "oversized"
    budget["nodes"] -= 1
    value_type = type(value)

    if value is None or value_type is bool:
        return True, value, ""
    if value_type is int:
        if int.bit_length(value) > _MAX_RESULT_INTEGER_BITS:
            return False, None, "oversized"
        return True, value, ""
    if value_type is float:
        if not math.isfinite(value):
            return False, None, "not_json"
        return True, value, ""
    if value_type is str:
        length = str.__len__(value)
        if length > budget["text"]:
            return False, None, "oversized"
        budget["text"] -= length
        return True, value, ""

    if value_type not in (dict, list, tuple):
        return False, None, "not_json"
    identity = id(value)
    if identity in active:
        return False, None, "not_json"
    active.add(identity)
    try:
        if value_type is dict:
            length = dict.__len__(value)
            if length > _MAX_RESULT_ITEMS or length > budget["nodes"]:
                return False, None, "oversized"
            entries = list(dict.items(value))
            if any(type(key) is not str for key, _item in entries):
                return False, None, "not_json"
            entries.sort(key=lambda entry: entry[0])
            cloned: Dict[str, Any] = {}
            for key, item in entries:
                key_length = str.__len__(key)
                if key_length > budget["text"]:
                    return False, None, "oversized"
                budget["text"] -= key_length
                ok, projected, reason = _safe_json_clone(
                    item, depth=depth + 1, budget=budget, active=active,
                )
                if not ok:
                    return False, None, reason
                cloned[key] = projected
            return True, cloned, ""

        length = (
            list.__len__(value) if value_type is list else tuple.__len__(value)
        )
        if length > _MAX_RESULT_ITEMS or length > budget["nodes"]:
            return False, None, "oversized"
        projected_items: List[Any] = []
        getter = list.__getitem__ if value_type is list else tuple.__getitem__
        for index in range(length):
            ok, projected, reason = _safe_json_clone(
                getter(value, index),
                depth=depth + 1,
                budget=budget,
                active=active,
            )
            if not ok:
                return False, None, reason
            projected_items.append(projected)
        return True, projected_items, ""
    finally:
        active.remove(identity)


def _summary_token(
    value: Any,
    *,
    maximum: int,
    depth: int = 0,
    active: Optional[set[int]] = None,
) -> str:
    writer = _BoundedText(maximum)
    _write_summary(value, writer=writer, depth=depth, active=active or set())
    return writer.render()


_BASE_EXCEPTION_ARGS = BaseException.__dict__["args"]


def _safe_exception_projection(error: BaseException) -> tuple[str, str]:
    """Project exception identity/args without calling exception user hooks."""
    error_type = _safe_type_name(error)
    try:
        arguments = _BASE_EXCEPTION_ARGS.__get__(error, type(error))
    except BaseException:
        arguments = ()
    if type(arguments) is not tuple:
        return error_type, f"<{error_type} raised>"
    if tuple.__len__(arguments) == 1:
        first = tuple.__getitem__(arguments, 0)
        if type(first) is str:
            return error_type, _safe_text(first, 1000)
        return error_type, _summary_token(first, maximum=1000)
    if not arguments:
        return error_type, f"<{error_type} raised>"
    return error_type, _summary_token(arguments, maximum=1000)


def _shallow_summary_token(value: Any, *, maximum: int) -> str:
    writer = _BoundedText(maximum)
    value_type = type(value)
    type_name = _safe_type_name(value)
    if value is None:
        writer.add("null")
    elif value_type is bool:
        writer.add("true" if value else "false")
    elif value_type is int:
        bits = int.bit_length(value)
        if bits <= _MAX_RESULT_INTEGER_BITS:
            writer.add(json.dumps(value, allow_nan=False))
        else:
            sign = "negative" if value < 0 else "positive"
            writer.add(f"<int bits={bits} sign={sign}>")
    elif value_type is float:
        if math.isfinite(value):
            writer.add(json.dumps(value, allow_nan=False))
        elif math.isnan(value):
            writer.add("<float nan>")
        elif value < 0:
            writer.add("<float -infinity>")
        else:
            writer.add("<float infinity>")
    elif value_type is str:
        length = str.__len__(value)
        prefix = str.__getitem__(value, slice(0, 512))
        writer.add(json.dumps(prefix, ensure_ascii=False))
        if length > 512:
            writer.add(f"<chars={length}>")
    elif value_type is bytes:
        length = bytes.__len__(value)
        writer.add("bytes:")
        writer.add(bytes.hex(bytes.__getitem__(value, slice(0, 128))))
        if length > 128:
            writer.add(f"<bytes={length}>")
    elif value_type is bytearray:
        length = bytearray.__len__(value)
        prefix = bytearray.__getitem__(value, slice(0, 128))
        writer.add("bytearray:")
        writer.add(bytearray.hex(prefix))
        if length > 128:
            writer.add(f"<bytes={length}>")
    elif value_type is dict:
        writer.add(f"<dict items={dict.__len__(value)}>")
    elif value_type is list:
        writer.add(f"<list items={list.__len__(value)}>")
    elif value_type is tuple:
        writer.add(f"<tuple items={tuple.__len__(value)}>")
    elif value_type is set:
        writer.add(f"<set items={set.__len__(value)}>")
    elif value_type is frozenset:
        writer.add(f"<frozenset items={frozenset.__len__(value)}>")
    else:
        writer.add(f"<{type_name} opaque>")
    return writer.render()


def _write_summary(
    value: Any,
    *,
    writer: _BoundedText,
    depth: int,
    active: set[int],
) -> None:
    """Write a deterministic diagnostic shape without calling user methods."""
    value_type = type(value)
    type_name = _safe_type_name(value)
    if depth > _MAX_RESULT_DEPTH:
        writer.add(f"<max-depth:{type_name}>")
        return
    if value_type not in (dict, list, tuple, set, frozenset):
        writer.add(_shallow_summary_token(value, maximum=2000))
        return

    identity = id(value)
    if identity in active:
        writer.add(f"<cycle:{type_name}>")
        return
    active.add(identity)
    try:
        if value_type is dict:
            length = dict.__len__(value)
            if length > _MAX_RESULT_ITEMS:
                writer.add(f"<dict items={length}>")
                return
            pairs = []
            for key, item in dict.items(value):
                key_token = _shallow_summary_token(key, maximum=256)
                item_token = _shallow_summary_token(item, maximum=512)
                pairs.append((key_token, item_token))
            pairs.sort()
            writer.add("{")
            for index, (key_token, item_token) in enumerate(pairs):
                if index:
                    writer.add(",")
                writer.add(key_token)
                writer.add(":")
                writer.add(item_token)
            writer.add("}")
            return

        if value_type in (list, tuple):
            length = (
                list.__len__(value)
                if value_type is list
                else tuple.__len__(value)
            )
            getter = list.__getitem__ if value_type is list else tuple.__getitem__
            visible = min(length, _MAX_RESULT_ITEMS)
            writer.add("[" if value_type is list else "tuple(")
            for index in range(visible):
                if index:
                    writer.add(",")
                writer.add(_shallow_summary_token(
                    getter(value, index), maximum=512,
                ))
            if visible < length:
                if visible:
                    writer.add(",")
                writer.add(f"…<items={length}>")
            writer.add("]" if value_type is list else ")")
            return

        length = (
            set.__len__(value)
            if value_type is set
            else frozenset.__len__(value)
        )
        if length > _MAX_RESULT_ITEMS:
            writer.add(f"<{type_name} items={length}>")
            return
        iterator = (
            set.__iter__(value)
            if value_type is set
            else frozenset.__iter__(value)
        )
        tokens = sorted(
            _shallow_summary_token(item, maximum=512)
            for item in iterator
        )
        writer.add("set{" if value_type is set else "frozenset{")
        for index, token in enumerate(tokens):
            if index:
                writer.add(",")
            writer.add(token)
        writer.add("}")
    finally:
        active.remove(identity)


def _render_receipt_projection(
    *,
    value_type: str,
    reason: str,
    summary: str,
    descriptor: str,
) -> str:
    digest_payload = json.dumps(
        [value_type, reason, descriptor],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    envelope = {
        "digest": hashlib.sha256(digest_payload.encode("ascii")).hexdigest(),
        "digest_scope": "bounded-shape-v1",
        "projection": "bounded",
        "reason": reason,
        "summary": "",
        "type": value_type,
    }

    def render(summary_prefix: str) -> str:
        envelope["summary"] = summary_prefix
        return json.dumps(
            envelope,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    encoded = render(summary)
    if len(encoded) <= _MAX_METADATA_BYTES:
        return encoded

    # ASCII JSON makes character length the exact persisted byte length.
    # Binary search retains the largest exact prefix; whitespace is never
    # normalized or collapsed.
    low, high = 0, len(summary)
    bounded = render("")
    while low <= high:
        middle = (low + high) // 2
        candidate = summary[:middle]
        if middle < len(summary):
            candidate += "…"
        projected = render(candidate)
        if len(projected) <= _MAX_METADATA_BYTES:
            bounded = projected
            low = middle + 1
        else:
            high = middle - 1
    return bounded


def _bounded_receipt_json(value: Any, *, value_type: str) -> str:
    budget = {
        "nodes": _MAX_RESULT_NODES,
        "text": _MAX_RESULT_TEXT_CHARS,
    }
    ok, safe_value, reason = _safe_json_clone(
        value, depth=0, budget=budget, active=set(),
    )
    canonical: Optional[str] = None
    if ok:
        canonical = json.dumps(
            safe_value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        if len(canonical) <= _MAX_METADATA_BYTES:
            return canonical
        reason = "oversized"

    if canonical is not None:
        descriptor = canonical
        summary = canonical[:_MAX_RESULT_SUMMARY_CHARS]
    else:
        descriptor = _summary_token(
            value, maximum=_MAX_RESULT_DESCRIPTOR_CHARS,
        )
        summary = descriptor[:_MAX_RESULT_SUMMARY_CHARS]
    return _render_receipt_projection(
        value_type=value_type,
        reason=reason or "not_json",
        summary=summary,
        descriptor=descriptor,
    )


def _receipt_json(value: Any) -> str:
    """Project an arbitrary callback result into deterministic bounded JSON.

    No operation in this function dispatches through a user-defined iterator,
    string/repr conversion, container override, or metaclass attribute hook.
    Concurrent mutation of an exact built-in container also degrades to an
    opaque receipt instead of escaping and stranding the claimed attempt.
    """
    value_type = _safe_type_name(value)
    try:
        return _bounded_receipt_json(value, value_type=value_type)
    except Exception:
        descriptor = f"<{value_type} projection-unavailable>"
        return _render_receipt_projection(
            value_type=value_type,
            reason="not_json",
            summary=descriptor,
            descriptor=descriptor,
        )


class TaskSchedule:
    """A persisted periodic task and its current scheduling state."""

    def __init__(
        self,
        id: str,
        name: str,
        interval_seconds: int,
        callback_name: str,
        last_run: Optional[datetime] = None,
        next_run: Optional[datetime] = None,
        enabled: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
        failure_count: int = 0,
        lease_token: Optional[str] = None,
        lease_expires_at: Optional[datetime] = None,
        degraded_reason: Optional[str] = None,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        self.id = id
        self.name = name
        self.interval_seconds = interval_seconds
        self.callback_name = callback_name
        self.last_run = last_run
        self.next_run = next_run or datetime.now(timezone.utc)
        self.enabled = enabled
        self.metadata = metadata or {}
        self.failure_count = max(0, int(failure_count))
        self.lease_token = lease_token
        self.lease_expires_at = lease_expires_at
        self.degraded_reason = degraded_reason
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "callback_name": self.callback_name,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "enabled": self.enabled,
            "metadata": self.metadata,
            "failure_count": self.failure_count,
            "leased": bool(self.lease_token),
            "lease_expires_at": (
                self.lease_expires_at.isoformat()
                if self.lease_expires_at else None
            ),
            "degraded_reason": self.degraded_reason,
        }


@dataclass(frozen=True)
class RunClaim:
    schedule: TaskSchedule
    attempt_id: str
    lease_token: str
    claimed_at: datetime
    lease_expires_at: datetime


class ScheduleStore:
    """SQLite schedule store with atomic cross-process due claims."""

    def __init__(self, db_path: str, *, clock: Optional[Callable[[], datetime]] = None):
        self._db_path = str(db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._init_db()

    def _now(self) -> datetime:
        return _utc(self._clock())

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._db_path,
            timeout=30.0,
            isolation_level=None,
        )
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        now = _iso(self._now())
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS schedules (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        interval_seconds INTEGER NOT NULL,
                        callback_name TEXT NOT NULL,
                        last_run TEXT,
                        next_run TEXT,
                        enabled INTEGER DEFAULT 1,
                        metadata TEXT DEFAULT '{}'
                    )
                """)
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(schedules)")
                }
                additions = {
                    "failure_count": "INTEGER NOT NULL DEFAULT 0",
                    "lease_token": "TEXT",
                    "lease_expires_at": "TEXT",
                    "degraded_reason": "TEXT",
                    "created_at": "TEXT",
                    "updated_at": "TEXT",
                }
                for column, declaration in additions.items():
                    if column not in columns:
                        conn.execute(
                            f"ALTER TABLE schedules ADD COLUMN {column} {declaration}"
                        )
                conn.execute(
                    "UPDATE schedules SET created_at=COALESCE(created_at, ?), "
                    "updated_at=COALESCE(updated_at, ?), failure_count="
                    "COALESCE(failure_count, 0), next_run=COALESCE(next_run, ?)",
                    (now, now, now),
                )
                conn.execute(
                    "UPDATE schedules SET lease_token=NULL, lease_expires_at=NULL "
                    "WHERE (lease_token IS NULL) != (lease_expires_at IS NULL)"
                )
                self._migrate_duplicate_names(conn)
                self._migrate_legacy_rows(conn)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS schedules_name_unique "
                    "ON schedules(name)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules "
                    "(enabled, next_run, lease_expires_at)"
                )
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS schedule_run_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        schedule_id TEXT NOT NULL,
                        task_name TEXT NOT NULL,
                        callback_name TEXT NOT NULL,
                        lease_token TEXT NOT NULL UNIQUE,
                        claimed_at TEXT NOT NULL,
                        lease_expires_at TEXT NOT NULL,
                        scheduled_for TEXT NOT NULL,
                        failure_count_before INTEGER NOT NULL,
                        metadata_digest TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS schedule_run_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        attempt_id TEXT NOT NULL UNIQUE,
                        schedule_id TEXT NOT NULL,
                        task_name TEXT NOT NULL,
                        lease_token TEXT NOT NULL,
                        status TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        result_json TEXT,
                        error_type TEXT,
                        error_message TEXT,
                        failure_count_after INTEGER NOT NULL,
                        next_run TEXT
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS schedule_attempt_schedule_idx "
                    "ON schedule_run_attempts(schedule_id, claimed_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS schedule_receipt_schedule_idx "
                    "ON schedule_run_receipts(schedule_id, finished_at)"
                )
                for table in ("schedule_run_attempts", "schedule_run_receipts"):
                    conn.execute(f"""
                        CREATE TRIGGER IF NOT EXISTS {table}_no_update
                        BEFORE UPDATE ON {table}
                        BEGIN
                            SELECT RAISE(ABORT, '{table} append-only');
                        END
                    """)
                    conn.execute(f"""
                        CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                        BEFORE DELETE ON {table}
                        BEGIN
                            SELECT RAISE(ABORT, '{table} append-only');
                        END
                    """)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _migrate_duplicate_names(conn: sqlite3.Connection) -> None:
        duplicates = conn.execute(
            "SELECT name FROM schedules GROUP BY name HAVING COUNT(*) > 1"
        ).fetchall()
        for duplicate in duplicates:
            name = str(duplicate["name"])
            rows = conn.execute(
                "SELECT rowid,id FROM schedules WHERE name=? ORDER BY rowid,id",
                (name,),
            ).fetchall()
            canonical_id = str(rows[0]["id"])
            for index, row in enumerate(rows[1:], start=1):
                digest = hashlib.sha256(str(row["id"]).encode()).hexdigest()[:12]
                renamed = f"{name[:96]}#legacy-duplicate-{digest}-{index}"
                conn.execute(
                    "UPDATE schedules SET name=?,enabled=0,degraded_reason=? "
                    "WHERE rowid=?",
                    (
                        renamed,
                        f"migration_duplicate_name:{canonical_id}"[:500],
                        row["rowid"],
                    ),
                )

    @staticmethod
    def _migrate_legacy_rows(conn: sqlite3.Connection) -> None:
        for row in conn.execute(
            "SELECT id,name,interval_seconds,callback_name,metadata "
            "FROM schedules"
        ).fetchall():
            reason: Optional[str] = None
            if not _TASK_NAME.fullmatch(str(row["name"] or "")):
                reason = "migration_invalid_task_name"
            try:
                _validate_interval(row["interval_seconds"])
            except ValueError:
                reason = reason or "migration_invalid_interval"
            if not str(row["callback_name"] or "").strip():
                reason = reason or "migration_invalid_callback_name"
            try:
                decoded = json.loads(str(row["metadata"] or "{}"))
                _, encoded = _canonical_metadata(decoded)
            except (TypeError, ValueError, json.JSONDecodeError):
                reason = reason or "migration_invalid_metadata"
                encoded = None
            if encoded is not None:
                conn.execute(
                    "UPDATE schedules SET metadata=? WHERE id=?",
                    (encoded, row["id"]),
                )
            if reason:
                conn.execute("""
                    UPDATE schedules SET enabled=0,
                        degraded_reason=COALESCE(degraded_reason, ?)
                    WHERE id=?
                """, (reason, row["id"]))

    def register_schedule(
        self,
        *,
        name: str,
        interval_seconds: int,
        callback_name: str,
        metadata_json: str,
        now: datetime,
    ) -> TaskSchedule:
        schedule_id = str(uuid.uuid5(
            _SCHEDULE_NAMESPACE, f"colony-autonomy-schedule:{name}",
        ))
        stamp = _iso(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT * FROM schedules WHERE name=?", (name,),
                ).fetchone()
                if existing is None:
                    conn.execute("""
                        INSERT INTO schedules
                        (id,name,interval_seconds,callback_name,last_run,next_run,
                         enabled,metadata,failure_count,lease_token,
                         lease_expires_at,degraded_reason,created_at,updated_at)
                        VALUES (?,?,?,?,NULL,?,1,?,0,NULL,NULL,NULL,?,?)
                    """, (
                        schedule_id, name, interval_seconds, callback_name,
                        stamp, metadata_json, stamp, stamp,
                    ))
                else:
                    degraded = str(existing["degraded_reason"] or "")
                    recover_callback = (
                        degraded.startswith("callback_unregistered:")
                        or degraded in {
                            "migration_invalid_metadata",
                            "migration_invalid_interval",
                            "migration_invalid_callback_name",
                        }
                    )
                    conn.execute("""
                        UPDATE schedules SET interval_seconds=?,callback_name=?,
                            metadata=?,enabled=CASE WHEN ? THEN 1 ELSE enabled END,
                            degraded_reason=CASE WHEN ? THEN NULL
                                                 ELSE degraded_reason END,
                            updated_at=?
                        WHERE name=?
                    """, (
                        interval_seconds, callback_name, metadata_json,
                        1 if recover_callback else 0,
                        1 if recover_callback else 0,
                        stamp, name,
                    ))
                row = conn.execute(
                    "SELECT * FROM schedules WHERE name=?", (name,),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._row_to_schedule(row)

    def upsert(self, schedule: TaskSchedule) -> None:
        """Persist compatibility configuration without rewriting run state.

        Existing embedded callers may retain a ``TaskSchedule`` object while a
        different scheduler process claims or completes it.  On conflict only
        caller-owned definition/configuration fields are updated.  The store is
        the sole owner of cadence, terminal state, failures, degradation, and
        exact-token leases after initial insertion.
        """
        _, metadata_json = _canonical_metadata(schedule.metadata)
        stamp = _iso(self._now())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""
                    INSERT INTO schedules
                    (id,name,interval_seconds,callback_name,last_run,next_run,
                     enabled,metadata,failure_count,lease_token,lease_expires_at,
                     degraded_reason,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        interval_seconds=excluded.interval_seconds,
                        callback_name=excluded.callback_name,
                        enabled=excluded.enabled,
                        metadata=excluded.metadata,
                        updated_at=excluded.updated_at
                """, (
                    schedule.id, schedule.name, schedule.interval_seconds,
                    schedule.callback_name,
                    _iso(schedule.last_run) if schedule.last_run else None,
                    _iso(schedule.next_run), 1 if schedule.enabled else 0,
                    metadata_json, schedule.failure_count, schedule.lease_token,
                    _iso(schedule.lease_expires_at)
                    if schedule.lease_expires_at else None,
                    schedule.degraded_reason,
                    _iso(schedule.created_at) if schedule.created_at else stamp,
                    stamp,
                ))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def get_due(self, now: Optional[datetime] = None) -> List[TaskSchedule]:
        stamp = _iso(now or self._now())
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM schedules
                WHERE enabled=1 AND next_run <= ?
                  AND (lease_token IS NULL OR lease_expires_at <= ?)
                ORDER BY next_run,name
            """, (stamp, stamp)).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    def claim_one_due(
        self,
        now: datetime,
        *,
        lease_seconds: int,
        exclude_schedule_ids: Optional[set[str]] = None,
    ) -> Optional[RunClaim]:
        observed = _utc(now)
        stamp = _iso(observed)
        excluded = sorted(exclude_schedule_ids or set())
        exclusion_sql = ""
        parameters: List[Any] = [stamp, stamp]
        if excluded:
            exclusion_sql = " AND id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            parameters.extend(excluded)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM schedules WHERE enabled=1 "
                    "AND next_run <= ? AND (lease_token IS NULL OR "
                    "lease_expires_at <= ?)" + exclusion_sql
                    + " ORDER BY next_run,name LIMIT 1",
                    parameters,
                ).fetchone()
                if row is None:
                    conn.commit()
                    return None

                failure_count = int(row["failure_count"] or 0)
                old_token = str(row["lease_token"] or "")
                if old_token:
                    expired_attempt = conn.execute(
                        "SELECT * FROM schedule_run_attempts WHERE lease_token=?",
                        (old_token,),
                    ).fetchone()
                    if expired_attempt is not None:
                        inserted = self._insert_receipt(
                            conn,
                            attempt=expired_attempt,
                            status="lease_expired",
                            now=observed,
                            result_json=None,
                            error_type="LeaseExpired",
                            error_message="lease expired before terminal receipt",
                            failure_count_after=failure_count + 1,
                            next_run=_parse_time(row["next_run"]),
                        )
                        if inserted:
                            failure_count += 1

                lease_token = uuid.uuid4().hex
                attempt_id = str(uuid.uuid4())
                expires = observed + timedelta(seconds=lease_seconds)
                updated = conn.execute("""
                    UPDATE schedules
                    SET lease_token=?,lease_expires_at=?,failure_count=?,updated_at=?
                    WHERE id=? AND enabled=1 AND next_run <= ?
                      AND (lease_token IS NULL OR lease_expires_at <= ?)
                """, (
                    lease_token, _iso(expires), failure_count, stamp,
                    row["id"], stamp, stamp,
                ))
                if updated.rowcount != 1:
                    conn.rollback()
                    return None
                metadata_text = str(row["metadata"] or "{}")
                conn.execute("""
                    INSERT INTO schedule_run_attempts
                    (attempt_id,schedule_id,task_name,callback_name,lease_token,
                     claimed_at,lease_expires_at,scheduled_for,
                     failure_count_before,metadata_digest)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    attempt_id, row["id"], row["name"], row["callback_name"],
                    lease_token, stamp, _iso(expires), row["next_run"],
                    failure_count,
                    hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
                ))
                claimed_row = conn.execute(
                    "SELECT * FROM schedules WHERE id=?", (row["id"],),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return RunClaim(
            schedule=self._row_to_schedule(claimed_row),
            attempt_id=attempt_id,
            lease_token=lease_token,
            claimed_at=observed,
            lease_expires_at=expires,
        )

    @staticmethod
    def _insert_receipt(
        conn: sqlite3.Connection,
        *,
        attempt: sqlite3.Row,
        status: str,
        now: datetime,
        result_json: Optional[str],
        error_type: Optional[str],
        error_message: Optional[str],
        failure_count_after: int,
        next_run: Optional[datetime],
    ) -> bool:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO schedule_run_receipts
            (receipt_id,attempt_id,schedule_id,task_name,lease_token,status,
             finished_at,result_json,error_type,error_message,
             failure_count_after,next_run)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(uuid.uuid4()), attempt["attempt_id"], attempt["schedule_id"],
            attempt["task_name"], attempt["lease_token"], status[:64],
            _iso(now), result_json,
            _safe_text(error_type, 128) if error_type else None,
            _safe_text(error_message, 1000) if error_message else None,
            max(0, int(failure_count_after)),
            _iso(next_run) if next_run else None,
        ))
        return cursor.rowcount == 1

    def _completion_rows(
        self,
        conn: sqlite3.Connection,
        claim: RunClaim,
        now: datetime,
    ) -> tuple[Optional[sqlite3.Row], Optional[sqlite3.Row], bool]:
        schedule = conn.execute(
            "SELECT * FROM schedules WHERE id=?", (claim.schedule.id,),
        ).fetchone()
        attempt = conn.execute(
            "SELECT * FROM schedule_run_attempts WHERE attempt_id=?",
            (claim.attempt_id,),
        ).fetchone()
        owns = bool(
            schedule is not None
            and attempt is not None
            and schedule["lease_token"] == claim.lease_token
            and attempt["schedule_id"] == claim.schedule.id
            and attempt["lease_token"] == claim.lease_token
            and str(schedule["lease_expires_at"] or "") > _iso(now)
        )
        return schedule, attempt, owns

    def _record_lost_or_expired(
        self,
        conn: sqlite3.Connection,
        claim: RunClaim,
        schedule: Optional[sqlite3.Row],
        attempt: Optional[sqlite3.Row],
        now: datetime,
    ) -> None:
        if (
            attempt is None
            or attempt["schedule_id"] != claim.schedule.id
            or attempt["lease_token"] != claim.lease_token
        ):
            return
        expired_while_owned = bool(
            schedule is not None
            and schedule["lease_token"] == claim.lease_token
        )
        failure_count = int(schedule["failure_count"] or 0) if schedule else 0
        status = "lease_expired" if expired_while_owned else "lease_lost"
        inserted = self._insert_receipt(
            conn,
            attempt=attempt,
            status=status,
            now=now,
            result_json=None,
            error_type="LeaseExpired" if expired_while_owned else "LeaseLost",
            error_message=(
                "lease expired before terminal receipt"
                if expired_while_owned
                else "exact lease token no longer owns schedule"
            ),
            failure_count_after=failure_count + (1 if expired_while_owned else 0),
            next_run=_parse_time(schedule["next_run"]) if schedule else None,
        )
        if expired_while_owned and inserted:
            conn.execute("""
                UPDATE schedules SET lease_token=NULL,lease_expires_at=NULL,
                    failure_count=?,updated_at=?
                WHERE id=? AND lease_token=?
            """, (
                failure_count + 1, _iso(now), claim.schedule.id,
                claim.lease_token,
            ))

    def _complete_success_with_projection(
        self,
        claim: RunClaim,
        result: Any,
        *,
        now: datetime,
    ) -> tuple[bool, Any]:
        observed = _utc(now)
        # Projection is deliberately outside the SQLite writer transaction.
        # Even an unexpectedly expensive diagnostic value cannot hold the
        # scheduler's global write lock while it is rendered.
        result_json = _receipt_json(result) if result is not None else None
        projected_result = (
            json.loads(result_json) if result_json is not None else None
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                schedule, attempt, owns = self._completion_rows(
                    conn, claim, observed,
                )
                if not owns:
                    self._record_lost_or_expired(
                        conn, claim, schedule, attempt, observed,
                    )
                    conn.commit()
                    return False, projected_result
                interval = int(schedule["interval_seconds"])
                next_run = observed + timedelta(seconds=interval)
                self._insert_receipt(
                    conn,
                    attempt=attempt,
                    status="success",
                    now=observed,
                    result_json=result_json,
                    error_type=None,
                    error_message=None,
                    failure_count_after=0,
                    next_run=next_run,
                )
                updated = conn.execute("""
                    UPDATE schedules SET last_run=?,next_run=?,failure_count=0,
                        lease_token=NULL,lease_expires_at=NULL,
                        degraded_reason=NULL,updated_at=?
                    WHERE id=? AND lease_token=? AND lease_expires_at > ?
                """, (
                    _iso(observed), _iso(next_run), _iso(observed),
                    claim.schedule.id, claim.lease_token, _iso(observed),
                ))
                if updated.rowcount != 1:
                    raise RuntimeError("lease ownership changed during success commit")
                conn.commit()
                return True, projected_result
            except Exception:
                conn.rollback()
                raise

    def complete_success(self, claim: RunClaim, result: Any, *, now: datetime) -> bool:
        accepted, _projection = self._complete_success_with_projection(
            claim, result, now=now,
        )
        return accepted

    def complete_failure(
        self,
        claim: RunClaim,
        error: BaseException,
        *,
        now: datetime,
        base_backoff_seconds: int,
        max_backoff_seconds: int,
    ) -> bool:
        observed = _utc(now)
        error_type, error_message = _safe_exception_projection(error)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                schedule, attempt, owns = self._completion_rows(
                    conn, claim, observed,
                )
                if not owns:
                    self._record_lost_or_expired(
                        conn, claim, schedule, attempt, observed,
                    )
                    conn.commit()
                    return False
                failures = int(schedule["failure_count"] or 0) + 1
                exponent = min(30, failures - 1)
                backoff = min(
                    max_backoff_seconds,
                    base_backoff_seconds * (2 ** exponent),
                )
                next_run = observed + timedelta(seconds=backoff)
                self._insert_receipt(
                    conn,
                    attempt=attempt,
                    status="error",
                    now=observed,
                    result_json=None,
                    error_type=error_type,
                    error_message=error_message,
                    failure_count_after=failures,
                    next_run=next_run,
                )
                updated = conn.execute("""
                    UPDATE schedules SET next_run=?,failure_count=?,
                        lease_token=NULL,lease_expires_at=NULL,updated_at=?
                    WHERE id=? AND lease_token=? AND lease_expires_at > ?
                """, (
                    _iso(next_run), failures, _iso(observed),
                    claim.schedule.id, claim.lease_token, _iso(observed),
                ))
                if updated.rowcount != 1:
                    raise RuntimeError("lease ownership changed during failure commit")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def degrade_unknown_callback(
        self,
        claim: RunClaim,
        *,
        now: datetime,
    ) -> bool:
        observed = _utc(now)
        reason = f"callback_unregistered:{claim.schedule.callback_name}"[:500]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                schedule, attempt, owns = self._completion_rows(
                    conn, claim, observed,
                )
                if not owns:
                    self._record_lost_or_expired(
                        conn, claim, schedule, attempt, observed,
                    )
                    conn.commit()
                    return False
                failures = int(schedule["failure_count"] or 0) + 1
                self._insert_receipt(
                    conn,
                    attempt=attempt,
                    status="degraded",
                    now=observed,
                    result_json=None,
                    error_type="CallbackUnregistered",
                    error_message=reason,
                    failure_count_after=failures,
                    next_run=_parse_time(schedule["next_run"]),
                )
                updated = conn.execute("""
                    UPDATE schedules SET enabled=0,degraded_reason=?,
                        failure_count=?,lease_token=NULL,lease_expires_at=NULL,
                        updated_at=?
                    WHERE id=? AND lease_token=? AND lease_expires_at > ?
                """, (
                    reason, failures, _iso(observed), claim.schedule.id,
                    claim.lease_token, _iso(observed),
                ))
                if updated.rowcount != 1:
                    raise RuntimeError("lease ownership changed during degradation")
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def list_all(self) -> List[TaskSchedule]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedules ORDER BY next_run,name"
            ).fetchall()
        return [self._row_to_schedule(row) for row in rows]

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        stamp = _iso(self._now())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM schedules WHERE id=?", (schedule_id,),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return False
                # Enabling is an idempotent configuration operation regardless
                # of the row's current enabled flag. A compatibility writer may
                # have disabled a row after another process claimed it; the
                # exact-token lease still belongs to that original attempt and
                # must survive the disabled-to-enabled transition.
                if enabled:
                    conn.execute("""
                        UPDATE schedules SET enabled=1,degraded_reason=NULL,
                            updated_at=? WHERE id=?
                    """, (stamp, schedule_id))
                    conn.commit()
                    return True
                if not enabled and row["lease_token"]:
                    attempt = conn.execute(
                        "SELECT * FROM schedule_run_attempts WHERE lease_token=?",
                        (row["lease_token"],),
                    ).fetchone()
                    if attempt is not None:
                        self._insert_receipt(
                            conn,
                            attempt=attempt,
                            status="disabled",
                            now=self._now(),
                            result_json=None,
                            error_type="ScheduleDisabled",
                            error_message="schedule disabled while leased",
                            failure_count_after=int(row["failure_count"] or 0),
                            next_run=_parse_time(row["next_run"]),
                        )
                conn.execute("""
                    UPDATE schedules SET enabled=0,lease_token=NULL,
                        lease_expires_at=NULL,updated_at=? WHERE id=?
                """, (stamp, schedule_id))
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def delete(self, schedule_id: str) -> bool:
        """Delete an idle schedule, refusing to orphan an open run attempt.

        A live (or otherwise unreceipted) claim must first reach a terminal
        receipt.  ``set_enabled(..., False)`` can explicitly cancel a leased
        schedule with a ``disabled`` receipt before a subsequent delete.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT lease_token FROM schedules WHERE id=?",
                    (schedule_id,),
                ).fetchone()
                if row is None:
                    conn.commit()
                    return False
                open_attempt = conn.execute("""
                    SELECT 1
                    FROM schedule_run_attempts AS attempt
                    LEFT JOIN schedule_run_receipts AS receipt
                      ON receipt.attempt_id=attempt.attempt_id
                    WHERE attempt.schedule_id=? AND receipt.attempt_id IS NULL
                    LIMIT 1
                """, (schedule_id,)).fetchone()
                if row["lease_token"] is not None or open_attempt is not None:
                    conn.commit()
                    return False
                cursor = conn.execute(
                    "DELETE FROM schedules WHERE id=?", (schedule_id,),
                )
                conn.commit()
                return cursor.rowcount > 0
            except Exception:
                conn.rollback()
                raise

    def list_attempts(
        self, schedule_id: Optional[str] = None, *, limit: int = 100,
    ) -> List[dict]:
        bounded = max(1, min(1000, int(limit)))
        query = "SELECT * FROM schedule_run_attempts"
        params: List[Any] = []
        if schedule_id:
            query += " WHERE schedule_id=?"
            params.append(schedule_id)
        query += " ORDER BY claimed_at,rowid LIMIT ?"
        params.append(bounded)
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def list_receipts(
        self, schedule_id: Optional[str] = None, *, limit: int = 100,
    ) -> List[dict]:
        bounded = max(1, min(1000, int(limit)))
        query = "SELECT * FROM schedule_run_receipts"
        params: List[Any] = []
        if schedule_id:
            query += " WHERE schedule_id=?"
            params.append(schedule_id)
        query += " ORDER BY finished_at,rowid LIMIT ?"
        params.append(bounded)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item.get("result_json") is not None:
                item["result"] = json.loads(item["result_json"])
            result.append(item)
        return result

    def health_snapshot(self, now: datetime) -> dict:
        stamp = _iso(now)
        with self._connect() as conn:
            counts = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,
                       SUM(CASE WHEN degraded_reason IS NOT NULL THEN 1 ELSE 0 END)
                           AS degraded,
                       SUM(CASE WHEN lease_token IS NOT NULL
                                      AND lease_expires_at > ? THEN 1 ELSE 0 END)
                           AS leased,
                       SUM(CASE WHEN lease_token IS NOT NULL
                                      AND lease_expires_at <= ? THEN 1 ELSE 0 END)
                           AS expired,
                       SUM(CASE WHEN enabled=1 AND next_run <= ?
                                      AND (lease_token IS NULL
                                           OR lease_expires_at <= ?)
                                THEN 1 ELSE 0 END)
                           AS due
                FROM schedules
            """, (stamp, stamp, stamp, stamp)).fetchone()
            attempts = conn.execute(
                "SELECT COUNT(*) FROM schedule_run_attempts"
            ).fetchone()[0]
            receipts = conn.execute(
                "SELECT COUNT(*) FROM schedule_run_receipts"
            ).fetchone()[0]
            degraded_rows = conn.execute("""
                SELECT name,degraded_reason FROM schedules
                WHERE degraded_reason IS NOT NULL ORDER BY name LIMIT 20
            """).fetchall()
        return {
            "total_schedules": int(counts["total"] or 0),
            "enabled_schedules": int(counts["enabled"] or 0),
            "degraded_schedules": int(counts["degraded"] or 0),
            "active_leases": int(counts["leased"] or 0),
            "expired_leases": int(counts["expired"] or 0),
            "due_schedules": int(counts["due"] or 0),
            "run_attempts": int(attempts),
            "terminal_receipts": int(receipts),
            "open_attempts": max(0, int(attempts) - int(receipts)),
            "degraded": [dict(row) for row in degraded_rows],
        }

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> TaskSchedule:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {"legacy_metadata_invalid": True}
        if not isinstance(metadata, dict):
            metadata = {"legacy_metadata_invalid": True}
        return TaskSchedule(
            id=str(row["id"]),
            name=str(row["name"]),
            interval_seconds=int(row["interval_seconds"]),
            callback_name=str(row["callback_name"]),
            last_run=_parse_time(row["last_run"]),
            next_run=_parse_time(row["next_run"]),
            enabled=bool(row["enabled"]),
            metadata=metadata,
            failure_count=int(row["failure_count"] or 0),
            lease_token=str(row["lease_token"]) if row["lease_token"] else None,
            lease_expires_at=_parse_time(row["lease_expires_at"]),
            degraded_reason=(
                str(row["degraded_reason"])
                if row["degraded_reason"] else None
            ),
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )


class AutonomyScheduler:
    """Periodic scheduler with durable exact-token leases and receipts."""

    def __init__(
        self,
        db_path: str,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        lease_seconds: int = 300,
        base_backoff_seconds: int = 5,
        max_backoff_seconds: int = 3600,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lease_seconds = self._bounded_positive(
            lease_seconds, "lease_seconds", maximum=86_400,
        )
        self._base_backoff_seconds = self._bounded_positive(
            base_backoff_seconds, "base_backoff_seconds", maximum=86_400,
        )
        self._max_backoff_seconds = self._bounded_positive(
            max_backoff_seconds, "max_backoff_seconds", maximum=604_800,
        )
        if self._max_backoff_seconds < self._base_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be >= base_backoff_seconds"
            )
        self._store = ScheduleStore(db_path, clock=self._clock)
        self._callbacks: Dict[str, Callable] = {}
        self._last_tick_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._tick_count = 0

    @staticmethod
    def _bounded_positive(value: Any, field: str, *, maximum: int) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{field} must be an integer from 1-{maximum}")
        return value

    def _now(self) -> datetime:
        return _utc(self._clock())

    def register(
        self,
        name: str,
        callback: Union[Callable, Callable[..., Coroutine]],
        interval_seconds: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Register or update one stable-name periodic task.

        The public call signature is unchanged. Existing last/next run state
        and operator-disabled state survive re-registration. A schedule that
        was degraded only because its callback was absent is recovered.
        """
        task_name = _validate_name(name)
        interval = _validate_interval(interval_seconds)
        if not callable(callback):
            raise ValueError("callback must be callable")
        _, metadata_json = _canonical_metadata(metadata)
        schedule = self._store.register_schedule(
            name=task_name,
            interval_seconds=interval,
            callback_name=task_name,
            metadata_json=metadata_json,
            now=self._now(),
        )
        self._callbacks[task_name] = callback
        logger.debug("Registered schedule: %s (every %ds)", task_name, interval)
        return schedule.id

    async def tick(self) -> List[dict]:
        """Atomically claim and execute due tasks once for this tick."""
        results: List[dict] = []
        claimed_ids: set[str] = set()
        try:
            for _ in range(100):
                claim = self._store.claim_one_due(
                    self._now(),
                    lease_seconds=self._lease_seconds,
                    exclude_schedule_ids=claimed_ids,
                )
                if claim is None:
                    break
                task = claim.schedule
                claimed_ids.add(task.id)
                callback = self._callbacks.get(task.callback_name)
                if callback is None:
                    reason = f"callback_unregistered:{task.callback_name}"
                    accepted = self._store.degrade_unknown_callback(
                        claim, now=self._now(),
                    )
                    results.append({
                        "task": task.name,
                        "status": "degraded" if accepted else "ambiguous",
                        "error": reason if accepted else "lease_lost",
                    })
                    logger.warning(
                        "Disabled degraded schedule '%s': %s", task.name, reason,
                    )
                    continue
                try:
                    result = callback()
                    if _is_hook_free_awaitable(result):
                        result = await result
                except Exception as exc:
                    error_type, error_message = _safe_exception_projection(exc)
                    accepted = self._store.complete_failure(
                        claim,
                        exc,
                        now=self._now(),
                        base_backoff_seconds=self._base_backoff_seconds,
                        max_backoff_seconds=self._max_backoff_seconds,
                    )
                    results.append({
                        "task": task.name,
                        "status": "error" if accepted else "ambiguous",
                        "error": error_message if accepted else "lease_lost",
                    })
                    logger.warning(
                        "Scheduled task failed: %s — %s: %s",
                        task.name, error_type, error_message,
                    )
                    continue
                accepted, projected_result = (
                    self._store._complete_success_with_projection(
                        claim, result, now=self._now(),
                    )
                )
                results.append({
                    "task": task.name,
                    "status": "ok" if accepted else "ambiguous",
                    **(
                        {"result": projected_result}
                        if accepted
                        else {"error": "lease_lost"}
                    ),
                })
                if accepted:
                    logger.debug("Scheduled task completed: %s", task.name)
            self._last_tick_at = _iso(self._now())
            self._last_error = None
            self._tick_count += 1
            return results
        except Exception as exc:
            self._last_tick_at = _iso(self._now())
            error_type, error_message = _safe_exception_projection(exc)
            self._last_error = f"{error_type}:{error_message}"[:1000]
            self._tick_count += 1
            raise

    def list_schedules(self) -> List[TaskSchedule]:
        return self._store.list_all()

    def list_run_attempts(
        self, schedule_id: Optional[str] = None, *, limit: int = 100,
    ) -> List[dict]:
        return self._store.list_attempts(schedule_id, limit=limit)

    def list_run_receipts(
        self, schedule_id: Optional[str] = None, *, limit: int = 100,
    ) -> List[dict]:
        return self._store.list_receipts(schedule_id, limit=limit)

    @property
    def health(self) -> dict:
        snapshot = self._store.health_snapshot(self._now())
        snapshot.update({
            "healthy": (
                self._last_error is None
                and snapshot["degraded_schedules"] == 0
                and snapshot["expired_leases"] == 0
            ),
            "last_tick_at": self._last_tick_at,
            "last_error": self._last_error,
            "tick_count": self._tick_count,
        })
        return snapshot

    def enable(self, schedule_id: str) -> bool:
        return self._store.set_enabled(schedule_id, True)

    def disable(self, schedule_id: str) -> bool:
        return self._store.set_enabled(schedule_id, False)


__all__ = [
    "AutonomyScheduler", "RunClaim", "ScheduleStore", "TaskSchedule",
]
