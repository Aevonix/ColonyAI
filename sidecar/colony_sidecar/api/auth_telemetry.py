"""Privacy-safe, durable API authentication migration telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import Any


logger = logging.getLogger(__name__)

_AUTH_KINDS = frozenset({"legacy", "scoped", "anonymous", "unauthenticated", "public"})
_DECISIONS = frozenset({"allow", "deny"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AuthCounter:
    auth_kind: str
    principal_id: str
    method: str
    route: str
    required_scope: str
    decision: str
    reason: str
    count: int
    first_seen_at: str
    last_seen_at: str

    def key(self) -> tuple[str, ...]:
        return (
            self.auth_kind,
            self.principal_id,
            self.method,
            self.route,
            self.required_scope,
            self.decision,
            self.reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class AuthTelemetry:
    """Bounded counters keyed only by validated authority and route template.

    Tokens, credential IDs, headers, bodies, query values, peer addresses, and
    concrete path parameters are never accepted by this API.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path else None
        self._counters: dict[tuple[str, ...], AuthCounter] = {}
        self._error: str | None = None
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if self.path is not None:
            self._open()

    def _open(self) -> None:
        assert self.path is not None
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.exists():
                info = self.path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise ValueError("auth telemetry path must be a regular file")
                if stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("auth telemetry database must be mode 0600")
                if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                    raise ValueError("auth telemetry database must be owned by the service user")
            connection = sqlite3.connect(str(self.path), check_same_thread=False)
            os.chmod(self.path, 0o600)
            # Avoid persistent -wal/-shm sidecars with umask-dependent modes.
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_counters (
                    auth_kind TEXT NOT NULL,
                    principal_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    route TEXT NOT NULL,
                    required_scope TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (
                        auth_kind, principal_id, method, route,
                        required_scope, decision, reason
                    )
                )
                """
            )
            connection.commit()
            self._connection = connection
            for row in connection.execute(
                "SELECT auth_kind, principal_id, method, route, required_scope, "
                "decision, reason, count, first_seen_at, last_seen_at FROM auth_counters"
            ):
                counter = AuthCounter(*row)
                if counter.auth_kind not in _AUTH_KINDS or counter.decision not in _DECISIONS:
                    raise ValueError("auth telemetry database contains invalid enums")
                self._safe_value(counter.principal_id, maximum=128)
                self._safe_value(counter.method, maximum=12)
                self._safe_value(counter.route, maximum=256)
                self._safe_value(counter.required_scope, maximum=128)
                self._safe_value(counter.reason, maximum=64)
                if not isinstance(counter.count, int) or counter.count < 0:
                    raise ValueError("auth telemetry database contains an invalid count")
                for timestamp in (counter.first_seen_at, counter.last_seen_at):
                    parsed = datetime.fromisoformat(
                        timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
                    )
                    if parsed.tzinfo is None:
                        raise ValueError("auth telemetry timestamp needs a timezone")
                self._counters[counter.key()] = counter
            self._error = None
        except Exception as exc:  # diagnostics must not take authentication down
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._connection = None
            self._counters = {}
            self._error = f"{type(exc).__name__}: {exc}"
            logger.error("Auth telemetry persistence unavailable: %s", exc)

    @staticmethod
    def _safe_value(value: str, *, maximum: int = 256) -> str:
        cleaned = str(value or "").strip()
        if not cleaned or len(cleaned) > maximum:
            raise ValueError("unsafe auth telemetry label")
        if any(ord(char) < 32 or ord(char) > 126 for char in cleaned):
            raise ValueError("unsafe auth telemetry label")
        return cleaned

    def record(
        self,
        *,
        auth_kind: str,
        principal_id: str,
        method: str,
        route: str,
        required_scope: str,
        decision: str,
        reason: str,
    ) -> None:
        """Increment one privacy-safe counter.

        Callers must supply a framework route template, never ``request.url``.
        Invalid labels are discarded rather than risking sensitive persistence.
        """

        try:
            auth_kind = self._safe_value(auth_kind, maximum=32)
            decision = self._safe_value(decision, maximum=16)
            if auth_kind not in _AUTH_KINDS or decision not in _DECISIONS:
                raise ValueError("unknown auth telemetry enum")
            principal_id = self._safe_value(principal_id, maximum=128)
            method = self._safe_value(method.upper(), maximum=12)
            route = self._safe_value(route, maximum=256)
            required_scope = self._safe_value(required_scope, maximum=128)
            reason = self._safe_value(reason, maximum=64)
        except ValueError:
            logger.warning("Discarded unsafe auth telemetry labels")
            return

        timestamp = _now()
        key = (
            auth_kind, principal_id, method, route,
            required_scope, decision, reason,
        )
        with self._lock:
            counter = self._counters.get(key)
            if counter is None:
                counter = AuthCounter(
                    auth_kind=auth_kind,
                    principal_id=principal_id,
                    method=method,
                    route=route,
                    required_scope=required_scope,
                    decision=decision,
                    reason=reason,
                    count=1,
                    first_seen_at=timestamp,
                    last_seen_at=timestamp,
                )
                self._counters[key] = counter
            else:
                counter.count += 1
                counter.last_seen_at = timestamp
            if self._connection is not None:
                try:
                    self._connection.execute(
                        """
                        INSERT INTO auth_counters (
                            auth_kind, principal_id, method, route,
                            required_scope, decision, reason, count,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (
                            auth_kind, principal_id, method, route,
                            required_scope, decision, reason
                        ) DO UPDATE SET
                            count=excluded.count,
                            last_seen_at=excluded.last_seen_at
                        """,
                        (
                            *key,
                            counter.count,
                            counter.first_seen_at,
                            counter.last_seen_at,
                        ),
                    )
                    self._connection.commit()
                except Exception as exc:
                    self._error = f"{type(exc).__name__}: {exc}"
                    logger.error("Auth telemetry write failed: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            records = sorted(
                (counter.to_dict() for counter in self._counters.values()),
                key=lambda row: (
                    row["auth_kind"], row["principal_id"], row["method"],
                    row["route"], row["decision"], row["reason"],
                ),
            )
            totals = {
                "allow": sum(row["count"] for row in records if row["decision"] == "allow"),
                "deny": sum(row["count"] for row in records if row["decision"] == "deny"),
                "legacy_allow": sum(
                    row["count"] for row in records
                    if row["auth_kind"] == "legacy" and row["decision"] == "allow"
                ),
                "scoped_allow": sum(
                    row["count"] for row in records
                    if row["auth_kind"] == "scoped" and row["decision"] == "allow"
                ),
            }
            principal_totals: dict[str, dict[str, Any]] = {}
            for row in records:
                if row["auth_kind"] not in {"legacy", "scoped"}:
                    continue
                summary = principal_totals.setdefault(row["principal_id"], {
                    "auth_kind": row["auth_kind"],
                    "allow": 0,
                    "deny": 0,
                    "last_seen_at": None,
                })
                summary[row["decision"]] += row["count"]
                if not summary["last_seen_at"] or row["last_seen_at"] > summary["last_seen_at"]:
                    summary["last_seen_at"] = row["last_seen_at"]
            return {
                "enabled": True,
                "persistent": self.path is not None and self._connection is not None,
                "error": self._error,
                "totals": totals,
                "principals": principal_totals,
                "records": records,
                "privacy": {
                    "stores_key_material": False,
                    "stores_request_content": False,
                    "routes_are_templates": True,
                },
            }

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
