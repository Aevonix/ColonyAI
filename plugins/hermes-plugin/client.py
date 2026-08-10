"""Minimal HTTP client for the governed Hermes Colony adapter."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import select
import socket
import sqlite3
import stat
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import quote
import uuid

import httpcore
import httpx


logger = logging.getLogger(__name__)


class TurnOutboxConflict(RuntimeError):
    """The same stable turn id was offered with different content."""


class TurnOutboxFull(RuntimeError):
    """The bounded pending ledger cannot safely accept another turn."""


class TurnOutboxPayloadError(ValueError):
    """A turn envelope is not bounded canonical JSON."""


class PrivateSQLitePathError(OSError):
    """A configured private SQLite path does not meet the local trust contract."""


class TurnDeliveryOutcomeUnknown(TimeoutError):
    """A deadline expired after an idempotent turn request may have started."""


class _DrainDeadlineExceeded(TimeoutError):
    """Internal fixed signal that the caller's total drain budget expired."""


class _AbsoluteDeadlineNetworkStream(httpcore.NetworkStream):
    """Apply one monotonic deadline to every operation on a sync stream."""

    def __init__(self, network_socket: socket.socket, deadline_monotonic: float):
        self._socket = network_socket
        self._deadline_monotonic = float(deadline_monotonic)

    def _remaining(self, timeout: float | None, error_type: type[Exception]) -> float:
        remaining = self._deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise error_type("absolute turn-delivery deadline expired")
        if timeout is None:
            return remaining
        bounded = min(max(0.0, float(timeout)), remaining)
        if bounded <= 0:
            raise error_type("absolute turn-delivery phase deadline expired")
        return bounded

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            self._socket.settimeout(
                self._remaining(timeout, httpcore.ReadTimeout)
            )
            value = self._socket.recv(max_bytes)
            self._remaining(None, httpcore.ReadTimeout)
            return value
        except socket.timeout:
            raise httpcore.ReadTimeout(
                "absolute turn-delivery read deadline expired"
            ) from None
        except httpcore.ReadTimeout:
            raise
        except OSError:
            raise httpcore.ReadError("turn-delivery socket read failed") from None

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not buffer:
            return
        pending = memoryview(buffer)
        try:
            while pending:
                self._socket.settimeout(
                    self._remaining(timeout, httpcore.WriteTimeout)
                )
                sent = int(self._socket.send(pending))
                if sent <= 0:
                    raise httpcore.WriteError(
                        "turn-delivery socket stopped accepting bytes"
                    )
                pending = pending[sent:]
            self._remaining(None, httpcore.WriteTimeout)
        except socket.timeout:
            raise httpcore.WriteTimeout(
                "absolute turn-delivery write deadline expired"
            ) from None
        except httpcore.WriteError:
            raise
        except OSError:
            raise httpcore.WriteError("turn-delivery socket write failed") from None

    def close(self) -> None:
        self._socket.close()

    def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        try:
            self._socket.settimeout(
                self._remaining(timeout, httpcore.ConnectTimeout)
            )
            upgraded = ssl_context.wrap_socket(
                self._socket, server_hostname=server_hostname,
            )
            self._remaining(None, httpcore.ConnectTimeout)
            return _AbsoluteDeadlineNetworkStream(
                upgraded, self._deadline_monotonic,
            )
        except socket.timeout:
            self.close()
            raise httpcore.ConnectTimeout(
                "absolute turn-delivery TLS deadline expired"
            ) from None
        except httpcore.ConnectTimeout:
            self.close()
            raise
        except OSError:
            self.close()
            raise httpcore.ConnectError(
                "turn-delivery TLS connection failed"
            ) from None

    def get_extra_info(self, info: str) -> Any:
        try:
            if info == "ssl_object":
                return getattr(self._socket, "_sslobj", None)
            if info == "client_addr":
                return self._socket.getsockname()
            if info == "server_addr":
                return self._socket.getpeername()
            if info == "socket":
                return self._socket
            if info == "is_readable":
                return bool(select.select([self._socket], [], [], 0)[0])
        except (OSError, ValueError):
            return None
        return None


class _AbsoluteDeadlineNetworkBackend(httpcore.NetworkBackend):
    """No-DNS sync backend whose stream operations share one deadline."""

    def __init__(self, deadline_monotonic: float):
        self._deadline_monotonic = float(deadline_monotonic)

    def _remaining(self) -> float:
        remaining = self._deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise httpcore.ConnectTimeout(
                "absolute turn-delivery deadline expired"
            )
        return remaining

    @staticmethod
    def _numeric_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        candidate = "127.0.0.1" if host.lower() == "localhost" else host
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            # Synchronous name resolution has no portable Python wall deadline.
            # Fail closed instead of silently advertising a bound we cannot keep.
            raise httpcore.ConnectError(
                "deadline-bound turn delivery requires an IP literal or localhost"
            ) from None

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        address = self._numeric_host(host)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        network_socket = socket.socket(family, socket.SOCK_STREAM)
        try:
            for option in socket_options or ():
                network_socket.setsockopt(*option)
            network_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if local_address:
                local = self._numeric_host(local_address)
                if local.version != address.version:
                    raise httpcore.ConnectError(
                        "turn-delivery local address family does not match"
                    )
                bind_address = (
                    (str(local), 0, 0, 0) if local.version == 6
                    else (str(local), 0)
                )
                network_socket.bind(bind_address)
            remaining = self._remaining()
            if timeout is not None:
                remaining = min(remaining, max(0.0, float(timeout)))
            if remaining <= 0:
                raise httpcore.ConnectTimeout(
                    "absolute turn-delivery connect deadline expired"
                )
            network_socket.settimeout(remaining)
            remote_address = (
                (str(address), int(port), 0, 0) if address.version == 6
                else (str(address), int(port))
            )
            network_socket.connect(remote_address)
            self._remaining()
        except socket.timeout:
            network_socket.close()
            raise httpcore.ConnectTimeout(
                "absolute turn-delivery connect deadline expired"
            ) from None
        except (httpcore.ConnectError, httpcore.ConnectTimeout):
            network_socket.close()
            raise
        except OSError:
            network_socket.close()
            raise httpcore.ConnectError(
                "turn-delivery socket connection failed"
            ) from None
        return _AbsoluteDeadlineNetworkStream(
            network_socket, self._deadline_monotonic,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError(
            "deadline-bound turn delivery does not use a Unix socket"
        )

    def sleep(self, seconds: float) -> None:
        remaining = self._remaining()
        delay = max(0.0, float(seconds))
        if delay >= remaining:
            time.sleep(remaining)
            raise httpcore.ConnectTimeout(
                "absolute turn-delivery retry deadline expired"
            )
        time.sleep(delay)


class _AbsoluteDeadlineHTTPTransport(httpx.HTTPTransport):
    """HTTPX adapter backed by one fresh absolute-deadline connection pool."""

    def __init__(self, deadline_monotonic: float):
        # HTTPTransport.handle_request/close deliberately operate on this public
        # httpcore pool contract; a fresh one-request pool has no waiter thread.
        self._pool = httpcore.ConnectionPool(
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_AbsoluteDeadlineNetworkBackend(deadline_monotonic),
        )


@dataclass(frozen=True, slots=True)
class _PrivateFileIdentity:
    device: int
    inode: int


class PrivateSQLitePath:
    """Open one owner-private SQLite file without following aliases.

    This is a deliberately small POSIX boundary for local conversation state.
    The database's immediate parent must be an exact mode-0700 directory owned
    by the current effective uid.  Ancestors must be real directories owned by
    that uid or root and may not be group/other writable, except for a
    root-owned sticky directory such as ``/tmp``.  Every component is traversed
    with directory file descriptors and ``O_NOFOLLOW``.

    The leaf is created atomically as mode 0600 or accepted only when it is an
    existing, regular, current-euid, mode-0600, single-link file.  Existing
    files are never chmodded.  Holding the private parent and leaf descriptors
    while SQLite reopens the pathname, then comparing lstat/fstat identities,
    closes the practical path-swap window.  A process already running as the
    same uid is outside this local-filesystem threat boundary.
    """

    def __init__(self, path: str | os.PathLike[str]):
        raw = os.path.expanduser(str(path))
        candidate = Path(raw)
        if not candidate.is_absolute() or any(
            component in {".", ".."} for component in candidate.parts
        ):
            raise PrivateSQLitePathError(
                "private SQLite path must be absolute and normalized"
            )
        if not candidate.name:
            raise PrivateSQLitePathError("private SQLite path must name a file")
        required = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required):
            raise PrivateSQLitePathError(
                "private SQLite path requires POSIX no-follow directory opens"
            )
        self.path = candidate
        self._euid = os.geteuid()

    @staticmethod
    def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    def _validate_directory(
        self,
        value: os.stat_result,
        *,
        private_parent: bool,
        label: str,
    ) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise PrivateSQLitePathError(f"private SQLite {label} is not a directory")
        mode = stat.S_IMODE(value.st_mode)
        if private_parent:
            if value.st_uid != self._euid or mode != 0o700:
                raise PrivateSQLitePathError(
                    "private SQLite parent must be current-euid mode 0700"
                )
            return
        if value.st_uid not in {0, self._euid}:
            raise PrivateSQLitePathError(
                f"private SQLite {label} has an untrusted owner"
            )
        if mode & 0o022:
            sticky_root = value.st_uid == 0 and bool(value.st_mode & stat.S_ISVTX)
            if not sticky_root:
                raise PrivateSQLitePathError(
                    f"private SQLite {label} is writable by another principal"
                )

    def _open_parent(self) -> int:
        directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            current_fd = os.open(self.path.anchor, directory_flags)
        except OSError:
            raise PrivateSQLitePathError(
                "private SQLite root directory cannot be opened"
            ) from None
        components = self.path.parts[1:-1]
        try:
            self._validate_directory(
                os.fstat(current_fd), private_parent=not components, label="root",
            )
            for index, component in enumerate(components):
                is_private_parent = index == len(components) - 1
                try:
                    before = os.stat(
                        component, dir_fd=current_fd, follow_symlinks=False,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    except OSError:
                        raise PrivateSQLitePathError(
                            "private SQLite parent directory cannot be created"
                        ) from None
                    try:
                        before = os.stat(
                            component, dir_fd=current_fd, follow_symlinks=False,
                        )
                    except OSError:
                        raise PrivateSQLitePathError(
                            "private SQLite parent directory is unstable"
                        ) from None
                self._validate_directory(
                    before,
                    private_parent=is_private_parent,
                    label="parent" if is_private_parent else "ancestor",
                )
                try:
                    next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                except OSError:
                    raise PrivateSQLitePathError(
                        "private SQLite parent chain cannot be opened without links"
                    ) from None
                try:
                    after = os.fstat(next_fd)
                    if not self._same_inode(before, after):
                        raise PrivateSQLitePathError(
                            "private SQLite parent changed while it was opened"
                        )
                    self._validate_directory(
                        after,
                        private_parent=is_private_parent,
                        label="parent" if is_private_parent else "ancestor",
                    )
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException as error:
            os.close(current_fd)
            if isinstance(error, PrivateSQLitePathError):
                raise
            if isinstance(error, OSError):
                raise PrivateSQLitePathError(
                    "private SQLite parent chain cannot be inspected safely"
                ) from None
            raise

    def _validate_leaf(self, value: os.stat_result) -> None:
        if not stat.S_ISREG(value.st_mode):
            raise PrivateSQLitePathError(
                "private SQLite leaf must be a regular file"
            )
        if value.st_uid != self._euid:
            raise PrivateSQLitePathError(
                "private SQLite leaf must be owned by the current effective uid"
            )
        if value.st_nlink != 1:
            raise PrivateSQLitePathError(
                "private SQLite leaf must have exactly one filesystem link"
            )
        if stat.S_IMODE(value.st_mode) != 0o600:
            raise PrivateSQLitePathError(
                "private SQLite leaf must already be mode 0600"
            )

    def _assert_leaf_identity(
        self, parent_fd: int, identity: _PrivateFileIdentity,
    ) -> None:
        try:
            current = os.stat(
                self.path.name, dir_fd=parent_fd, follow_symlinks=False,
            )
        except OSError:
            raise PrivateSQLitePathError(
                "private SQLite leaf disappeared during open"
            ) from None
        self._validate_leaf(current)
        if (current.st_dev, current.st_ino) != (identity.device, identity.inode):
            raise PrivateSQLitePathError(
                "private SQLite leaf changed while SQLite reopened it"
            )

    def _open_leaf(
        self, *, create: bool,
    ) -> tuple[int, int, _PrivateFileIdentity]:
        parent_fd = self._open_parent()
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        created = False

        def open_existing() -> tuple[int, os.stat_result]:
            # Reject links and special files before an O_RDWR open can touch
            # them, then bind the no-follow fd back to that exact lstat inode.
            before = os.stat(
                self.path.name, dir_fd=parent_fd, follow_symlinks=False,
            )
            self._validate_leaf(before)
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    self.path.name, flags, dir_fd=parent_fd,
                )
                opened_value = os.fstat(descriptor)
                self._validate_leaf(opened_value)
                if not self._same_inode(before, opened_value):
                    raise PrivateSQLitePathError(
                        "private SQLite leaf changed while it was opened"
                    )
                return descriptor, opened_value
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                raise

        try:
            try:
                leaf_fd, opened = open_existing()
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    leaf_fd = os.open(
                        self.path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    created = True
                except FileExistsError:
                    leaf_fd, opened = open_existing()
                else:
                    opened = os.fstat(leaf_fd)
            if created:
                # This descriptor names the atomically-created, still
                # single-link inode. fchmod cannot follow a replacement path.
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_uid != self._euid
                    or opened.st_nlink != 1
                ):
                    raise PrivateSQLitePathError(
                        "new private SQLite leaf has invalid inode posture"
                    )
                os.fchmod(leaf_fd, 0o600)
                opened = os.fstat(leaf_fd)
            self._validate_leaf(opened)
            listed = os.stat(
                self.path.name, dir_fd=parent_fd, follow_symlinks=False,
            )
            self._validate_leaf(listed)
            if not self._same_inode(opened, listed):
                raise PrivateSQLitePathError(
                    "private SQLite leaf changed while it was opened"
                )
            identity = _PrivateFileIdentity(opened.st_dev, opened.st_ino)
            return parent_fd, leaf_fd, identity
        except BaseException as error:
            if "leaf_fd" in locals():
                os.close(leaf_fd)
            os.close(parent_fd)
            if isinstance(error, PrivateSQLitePathError):
                raise
            if isinstance(error, OSError):
                raise PrivateSQLitePathError(
                    "private SQLite leaf cannot be opened safely"
                ) from None
            raise

    def connect(
        self, *, timeout_seconds: float = 2.0,
    ) -> tuple[sqlite3.Connection, _PrivateFileIdentity]:
        parent_fd, leaf_fd, identity = self._open_leaf(create=True)
        connection: sqlite3.Connection | None = None
        try:
            timeout = max(0.0, min(float(timeout_seconds), 2.0))
            connection = sqlite3.connect(
                str(self.path), timeout=timeout, isolation_level=None,
            )
            self._assert_leaf_identity(parent_fd, identity)
            return connection, identity
        except BaseException as error:
            if connection is not None:
                connection.close()
            if isinstance(error, PrivateSQLitePathError):
                raise
            if isinstance(error, (OSError, sqlite3.Error)):
                raise PrivateSQLitePathError(
                    "private SQLite database could not be opened"
                ) from None
            raise
        finally:
            os.close(leaf_fd)
            os.close(parent_fd)

    def assert_current(self, identity: _PrivateFileIdentity) -> None:
        parent_fd = self._open_parent()
        try:
            self._assert_leaf_identity(parent_fd, identity)
        finally:
            os.close(parent_fd)

    def fsync(self) -> None:
        parent_fd, leaf_fd, identity = self._open_leaf(create=False)
        try:
            os.fsync(leaf_fd)
            os.fsync(parent_fd)
            self._assert_leaf_identity(parent_fd, identity)
        except OSError as error:
            if isinstance(error, PrivateSQLitePathError):
                raise
            raise PrivateSQLitePathError(
                "private SQLite durability sync failed"
            ) from None
        finally:
            os.close(leaf_fd)
            os.close(parent_fd)


class TurnOutbox:
    """SQLite-backed durable turn ledger shared across Hermes processes.

    Enqueue commits with SQLite's ``synchronous=FULL`` configuration before
    returning. Delivery receipts remain in the ledger so a repeated host hook
    or a later one-shot process does not resend an already accepted turn. The
    delivery callback runs synchronously with the remaining cooperative budget;
    the durable row, not a daemon writer, is the loss-prevention mechanism.
    """

    _APPLICATION_ID = 1_129_270_361  # big-endian ASCII ``COLY``
    _USER_VERSION = 1
    _SCHEMA = """
        CREATE TABLE turn_outbox (
            turn_id TEXT PRIMARY KEY,
            envelope_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending', 'delivered')),
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            lease_id TEXT NOT NULL DEFAULT '',
            lease_expires_at REAL NOT NULL DEFAULT 0
        )
    """
    _PREDECESSOR_SCHEMA = """
        CREATE TABLE turn_outbox (
            turn_id TEXT PRIMARY KEY,
            envelope_sha256 TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending', 'delivered')),
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_error TEXT NOT NULL DEFAULT ''
        )
    """
    _PENDING_INDEX = (
        "CREATE INDEX turn_outbox_pending_idx "
        "ON turn_outbox(state, lease_expires_at, created_at, turn_id)"
    )
    _CURRENT_COLUMNS = (
        (0, "turn_id", "TEXT", 0, None, 1),
        (1, "envelope_sha256", "TEXT", 1, None, 0),
        (2, "payload_json", "TEXT", 1, None, 0),
        (3, "state", "TEXT", 1, None, 0),
        (4, "attempts", "INTEGER", 1, "0", 0),
        (5, "created_at", "REAL", 1, None, 0),
        (6, "updated_at", "REAL", 1, None, 0),
        (7, "last_error", "TEXT", 1, "''", 0),
        (8, "lease_id", "TEXT", 1, "''", 0),
        (9, "lease_expires_at", "REAL", 1, "0", 0),
    )
    _PREDECESSOR_COLUMNS = _CURRENT_COLUMNS[:8]

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_payload_bytes: int = 64 * 1024,
        max_pending: int = 10_000,
        max_delivered: int = 8_192,
    ):
        self.path = Path(os.path.expanduser(str(path)))
        self.max_payload_bytes = max(4096, min(int(max_payload_bytes), 256 * 1024))
        self.max_pending = max(128, min(int(max_pending), 100_000))
        self.max_delivered = max(128, min(int(max_delivered), 100_000))
        self._schema_lock = threading.RLock()
        self._validated_identity: _PrivateFileIdentity | None = None
        self._validated_schema_version: int | None = None

    def _storage(self) -> PrivateSQLitePath:
        return PrivateSQLitePath(self.path)

    @staticmethod
    def _normalized_sql(value: Any) -> str:
        compact = " ".join(str(value or "").split())
        return re.sub(r"\s*([(),])\s*", r"\1", compact)

    @classmethod
    def _schema_objects(cls, connection: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "ORDER BY type, name"
            ).fetchall()
        )

    @classmethod
    def _expected_objects(cls, *, predecessor: bool) -> tuple[tuple[Any, ...], ...]:
        table_sql = cls._PREDECESSOR_SCHEMA if predecessor else cls._SCHEMA
        objects: list[tuple[Any, ...]] = [
            ("index", "sqlite_autoindex_turn_outbox_1", "turn_outbox", None),
        ]
        if not predecessor:
            objects.append((
                "index", "turn_outbox_pending_idx", "turn_outbox",
                cls._PENDING_INDEX,
            ))
        objects.append(("table", "turn_outbox", "turn_outbox", table_sql))
        return tuple(objects)

    @classmethod
    def _objects_match(
        cls,
        actual: Sequence[Sequence[Any]],
        expected: Sequence[Sequence[Any]],
    ) -> bool:
        if len(actual) != len(expected):
            return False
        for left, right in zip(actual, expected):
            if tuple(left[:3]) != tuple(right[:3]):
                return False
            if left[3] is None or right[3] is None:
                if left[3] is not None or right[3] is not None:
                    return False
            elif cls._normalized_sql(left[3]) != cls._normalized_sql(right[3]):
                return False
        return True

    @staticmethod
    def _quick_check(connection: sqlite3.Connection) -> None:
        rows = connection.execute("PRAGMA quick_check").fetchall()
        if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
            raise PrivateSQLitePathError(
                "private SQLite consistency check failed"
            )

    @classmethod
    def _table_columns(
        cls, connection: sqlite3.Connection,
    ) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            tuple(row[:6])
            for row in connection.execute(
                "PRAGMA table_info(turn_outbox)"
            ).fetchall()
        )

    @classmethod
    def _validate_current_schema(cls, connection: sqlite3.Connection) -> None:
        cls._quick_check(connection)
        if not cls._objects_match(
            cls._schema_objects(connection),
            cls._expected_objects(predecessor=False),
        ):
            raise PrivateSQLitePathError(
                "private SQLite schema is not the governed outbox schema"
            )
        if cls._table_columns(connection) != cls._CURRENT_COLUMNS:
            raise PrivateSQLitePathError(
                "private SQLite outbox columns are not exact"
            )
        if connection.execute(
            "PRAGMA foreign_key_list(turn_outbox)"
        ).fetchall():
            raise PrivateSQLitePathError(
                "private SQLite outbox cannot contain foreign keys"
            )
        indexes = connection.execute("PRAGMA index_list(turn_outbox)").fetchall()
        by_name = {str(row[1]): tuple(row) for row in indexes}
        pending = by_name.get("turn_outbox_pending_idx")
        automatic = by_name.get("sqlite_autoindex_turn_outbox_1")
        if (
            set(by_name) != {
                "sqlite_autoindex_turn_outbox_1", "turn_outbox_pending_idx",
            }
            or pending is None
            or tuple(pending[2:5]) != (0, "c", 0)
            or automatic is None
            or tuple(automatic[2:5]) != (1, "pk", 0)
        ):
            raise PrivateSQLitePathError(
                "private SQLite outbox indexes are not exact"
            )
        pending_columns = tuple(
            str(row[2])
            for row in connection.execute(
                "PRAGMA index_xinfo(turn_outbox_pending_idx)"
            ).fetchall()
            if int(row[5]) == 1
        )
        if pending_columns != (
            "state", "lease_expires_at", "created_at", "turn_id",
        ):
            raise PrivateSQLitePathError(
                "private SQLite pending index columns are not exact"
            )
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != (
            cls._APPLICATION_ID
        ) or int(connection.execute("PRAGMA user_version").fetchone()[0]) != (
            cls._USER_VERSION
        ):
            raise PrivateSQLitePathError(
                "private SQLite schema version is not recognized"
            )

        # The canonical sqlite_master source above is the primary CHECK proof.
        # Also exercise its behavior inside the surrounding transaction without
        # leaving a row or firing any trigger (the exact object set has none).
        connection.execute("SAVEPOINT governed_check_contract")
        try:
            try:
                connection.execute(
                    "INSERT INTO turn_outbox ("
                    "turn_id, envelope_sha256, payload_json, state, attempts, "
                    "created_at, updated_at, last_error, lease_id, lease_expires_at"
                    ") VALUES (?, ?, ?, ?, 0, 0, 0, '', '', 0)",
                    (
                        "__governed_check_probe__:" + uuid.uuid4().hex,
                        "0" * 64, "{}", "invalid",
                    ),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise PrivateSQLitePathError(
                    "private SQLite state CHECK is not enforced"
                )
        finally:
            connection.execute("ROLLBACK TO governed_check_contract")
            connection.execute("RELEASE governed_check_contract")

    @classmethod
    def _classify_schema(cls, connection: sqlite3.Connection) -> str:
        cls._quick_check(connection)
        objects = cls._schema_objects(connection)
        application_id = int(
            connection.execute("PRAGMA application_id").fetchone()[0]
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if not objects and application_id == 0 and user_version == 0:
            return "empty"
        if cls._objects_match(
            objects, cls._expected_objects(predecessor=True),
        ):
            if (
                cls._table_columns(connection) == cls._PREDECESSOR_COLUMNS
                and application_id == 0
                and user_version == 0
                and not connection.execute(
                    "PRAGMA foreign_key_list(turn_outbox)"
                ).fetchall()
            ):
                return "predecessor"
            return "unknown"
        if cls._objects_match(
            objects, cls._expected_objects(predecessor=False),
        ) and cls._table_columns(connection) == cls._CURRENT_COLUMNS:
            if application_id == 0 and user_version == 0:
                return "unversioned_current"
            if (
                application_id == cls._APPLICATION_ID
                and user_version == cls._USER_VERSION
            ):
                return "current"
        return "unknown"

    @classmethod
    def _prepare_schema(cls, connection: sqlite3.Connection) -> bool:
        """Validate or transactionally migrate only exact recognized states."""

        connection.execute("BEGIN IMMEDIATE")
        mutated = False
        try:
            state = cls._classify_schema(connection)
            if state == "empty":
                connection.execute(cls._SCHEMA)
                connection.execute(cls._PENDING_INDEX)
                mutated = True
            elif state == "predecessor":
                connection.execute(
                    "ALTER TABLE turn_outbox ADD COLUMN "
                    "lease_id TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "ALTER TABLE turn_outbox ADD COLUMN "
                    "lease_expires_at REAL NOT NULL DEFAULT 0"
                )
                connection.execute(cls._PENDING_INDEX)
                mutated = True
            elif state == "unversioned_current":
                mutated = True
            elif state != "current":
                raise PrivateSQLitePathError(
                    "private SQLite schema is unknown or malformed"
                )
            if mutated:
                connection.execute(f"PRAGMA application_id={cls._APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={cls._USER_VERSION}")
            cls._validate_current_schema(connection)
            connection.commit()
            return mutated
        except BaseException as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, PrivateSQLitePathError):
                raise
            if isinstance(error, sqlite3.Error):
                raise PrivateSQLitePathError(
                    "private SQLite schema could not be validated"
                ) from None
            raise

    @staticmethod
    def _configure_durability(connection: sqlite3.Connection) -> dict[str, Any]:
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA fullfsync=ON")
        connection.execute("PRAGMA checkpoint_fullfsync=ON")
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        fullfsync = int(connection.execute("PRAGMA fullfsync").fetchone()[0])
        checkpoint_fullfsync = int(
            connection.execute("PRAGMA checkpoint_fullfsync").fetchone()[0]
        )
        if (
            journal != "delete"
            or synchronous != 2
            or fullfsync != 1
            or checkpoint_fullfsync != 1
        ):
            raise PrivateSQLitePathError(
                "private SQLite durability pragmas are not active"
            )
        return {
            "journal_mode": journal,
            "synchronous": synchronous,
            "fullfsync": fullfsync,
            "checkpoint_fullfsync": checkpoint_fullfsync,
        }

    def _validate_cached_schema(
        self,
        connection: sqlite3.Connection,
        identity: _PrivateFileIdentity,
    ) -> None:
        if (
            self._validated_identity != identity
            or self._validated_schema_version is None
            or int(connection.execute("PRAGMA schema_version").fetchone()[0])
            != self._validated_schema_version
            or int(connection.execute("PRAGMA application_id").fetchone()[0])
            != self._APPLICATION_ID
            or int(connection.execute("PRAGMA user_version").fetchone()[0])
            != self._USER_VERSION
        ):
            self._validated_identity = None
            self._validated_schema_version = None
            raise PrivateSQLitePathError(
                "private SQLite cached schema posture changed"
            )

    @staticmethod
    def _remaining_seconds(deadline_monotonic: float | None) -> float:
        if deadline_monotonic is None:
            return 2.0
        remaining = float(deadline_monotonic) - time.monotonic()
        if remaining <= 0:
            raise _DrainDeadlineExceeded("turn outbox drain budget expired")
        return remaining

    @classmethod
    def _apply_busy_deadline(
        cls,
        connection: sqlite3.Connection,
        deadline_monotonic: float | None,
    ) -> None:
        if deadline_monotonic is None:
            milliseconds = 2_000
        else:
            remaining = cls._remaining_seconds(deadline_monotonic)
            milliseconds = max(0, min(int(remaining * 1_000), 2_000))
        connection.execute(f"PRAGMA busy_timeout={milliseconds}")

    def _connect(
        self,
        *,
        force_schema_validation: bool = False,
        deadline_monotonic: float | None = None,
    ) -> sqlite3.Connection:
        storage = self._storage()
        remaining = self._remaining_seconds(deadline_monotonic)
        connection, identity = storage.connect(timeout_seconds=remaining)
        try:
            connection.row_factory = sqlite3.Row
            self._apply_busy_deadline(connection, deadline_monotonic)
            self._configure_durability(connection)
            if deadline_monotonic is None:
                acquired_schema_lock = self._schema_lock.acquire()
            else:
                acquired_schema_lock = self._schema_lock.acquire(
                    timeout=self._remaining_seconds(deadline_monotonic)
                )
            if not acquired_schema_lock:
                raise _DrainDeadlineExceeded(
                    "turn outbox schema lock budget expired"
                )
            try:
                self._remaining_seconds(deadline_monotonic)
                needs_full_validation = bool(
                    force_schema_validation
                    or self._validated_identity != identity
                    or self._validated_schema_version is None
                )
                if needs_full_validation:
                    self._validated_identity = None
                    self._validated_schema_version = None
                    mutated = self._prepare_schema(connection)
                    self._remaining_seconds(deadline_monotonic)
                    storage.assert_current(identity)
                    # Explicit prepare/enqueue owns the filesystem sync. A
                    # budgeted drain never adds an uninterruptible fsync tail;
                    # SQLite's configured FULL commit remains in force.
                    if mutated and deadline_monotonic is None:
                        storage.fsync()
                    self._validated_identity = identity
                    self._validated_schema_version = int(
                        connection.execute("PRAGMA schema_version").fetchone()[0]
                    )
                else:
                    self._validate_cached_schema(connection, identity)
                    self._remaining_seconds(deadline_monotonic)
                    storage.assert_current(identity)
            finally:
                self._schema_lock.release()
            return connection
        except BaseException:
            connection.close()
            raise

    def prepare(self) -> dict[str, Any]:
        """Attest storage configuration without claiming physical proof."""

        connection = self._connect(force_schema_validation=True)
        try:
            durability = self._configure_durability(connection)
        finally:
            connection.close()
        self._fsync_storage()
        return {
            "schema": "PrivateSQLiteDurabilityConfigurationAttestationV2",
            "version": 2,
            "configuration_ready": True,
            "physical_power_loss_verified": False,
            "readiness_scope": "sqlite_and_filesystem_configuration",
            "path_sha256": hashlib.sha256(
                str(self.path).encode("utf-8")
            ).hexdigest(),
            "private_parent": True,
            "regular_file": True,
            "current_euid_owner": True,
            "single_link": True,
            "mode": "0600",
            "journal_mode": "delete",
            "synchronous": "FULL",
            "fullfsync": "ON" if durability["fullfsync"] == 1 else "OFF",
            "checkpoint_fullfsync": (
                "ON" if durability["checkpoint_fullfsync"] == 1 else "OFF"
            ),
            "application_id": self._APPLICATION_ID,
            "user_version": self._USER_VERSION,
        }

    def _fsync_storage(self) -> None:
        self._storage().fsync()

    def enqueue(self, turn_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        stable_id = str(turn_id or "").strip()
        if not stable_id:
            raise TurnOutboxPayloadError("turn_id is required")
        try:
            payload_json = json.dumps(
                dict(payload), sort_keys=True, separators=(",", ":"),
                ensure_ascii=True, allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise TurnOutboxPayloadError("turn payload is not canonical JSON") from error
        if len(payload_json.encode("utf-8")) > self.max_payload_bytes:
            raise TurnOutboxPayloadError("turn payload exceeds the durable outbox limit")
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT envelope_sha256, state, attempts FROM turn_outbox WHERE turn_id = ?",
                (stable_id,),
            ).fetchone()
            if row is not None:
                if row["envelope_sha256"] != digest:
                    raise TurnOutboxConflict(
                        "stable turn id already has a different envelope"
                    )
                connection.commit()
                return {
                    "turn_id": stable_id,
                    "envelope_sha256": digest,
                    "state": row["state"],
                    "attempts": int(row["attempts"]),
                }
            pending = int(connection.execute(
                "SELECT COUNT(*) FROM turn_outbox WHERE state = 'pending'"
            ).fetchone()[0])
            if pending >= self.max_pending:
                raise TurnOutboxFull("durable turn outbox is full")
            connection.execute(
                """
                INSERT INTO turn_outbox (
                    turn_id, envelope_sha256, payload_json, state, attempts,
                    created_at, updated_at, last_error
                ) VALUES (?, ?, ?, 'pending', 0, ?, ?, '')
                """,
                (stable_id, digest, payload_json, now, now),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        self._fsync_storage()
        return {
            "turn_id": stable_id,
            "envelope_sha256": digest,
            "state": "pending",
            "attempts": 0,
        }

    @staticmethod
    def _cooperative_delivery(
        deliver: Any, payload: dict[str, Any], timeout_seconds: float,
    ) -> str:
        """Call one deadline-aware delivery function without background work."""

        began = time.monotonic()
        try:
            accepted = bool(deliver(
                payload, timeout_seconds=float(timeout_seconds),
            ))
        except (TurnDeliveryOutcomeUnknown, TimeoutError):
            return "timeout"
        except BaseException:
            # Exception details can contain credentials, URLs, or content.
            return "exception"
        if time.monotonic() - began > timeout_seconds:
            # A callback that violates the cooperative contract cannot be
            # interrupted safely. Treat its result as ambiguous and never
            # spawn a continuation or retry it inside this drain.
            return "timeout"
        return "accepted" if accepted else "rejected"

    def _claim_one(
        self,
        *,
        lease_seconds: float,
        excluded_turn_ids: Sequence[str] = (),
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any] | None:
        """Lease one row in a short transaction; never hold a DB lock on I/O."""

        now = time.time()
        lease_id = uuid.uuid4().hex
        connection = self._connect(deadline_monotonic=deadline_monotonic)
        try:
            self._apply_busy_deadline(connection, deadline_monotonic)
            connection.execute("BEGIN IMMEDIATE")
            self._remaining_seconds(deadline_monotonic)
            excluded = tuple(str(item) for item in excluded_turn_ids)
            exclusion_sql = (
                " AND turn_id NOT IN (" + ",".join("?" for _item in excluded) + ")"
                if excluded else ""
            )
            row = connection.execute(
                """
                SELECT turn_id, payload_json FROM turn_outbox
                WHERE state = 'pending'
                  AND (lease_id = '' OR lease_expires_at <= ?)
                """ + exclusion_sql + """
                ORDER BY created_at, turn_id LIMIT 1
                """,
                (now, *excluded),
            ).fetchone()
            if row is None:
                self._apply_busy_deadline(connection, deadline_monotonic)
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE turn_outbox
                SET lease_id = ?, lease_expires_at = ?, attempts = attempts + 1,
                    updated_at = ?, last_error = ''
                WHERE turn_id = ? AND state = 'pending'
                  AND (lease_id = '' OR lease_expires_at <= ?)
                """,
                (
                    lease_id, now + lease_seconds, now,
                    row["turn_id"], now,
                ),
            )
            if updated.rowcount != 1:  # pragma: no cover - IMMEDIATE serializes claims
                connection.rollback()
                return None
            self._apply_busy_deadline(connection, deadline_monotonic)
            connection.commit()
            return {
                "turn_id": str(row["turn_id"]),
                "payload": json.loads(row["payload_json"]),
                "lease_id": lease_id,
            }
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_claim(
        self,
        claim: Mapping[str, Any],
        outcome: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> bool:
        """Finalize only the exact lease that performed the delivery attempt."""

        now = time.time()
        connection = self._connect(deadline_monotonic=deadline_monotonic)
        try:
            self._apply_busy_deadline(connection, deadline_monotonic)
            connection.execute("BEGIN IMMEDIATE")
            self._remaining_seconds(deadline_monotonic)
            if outcome == "accepted":
                updated = connection.execute(
                    """
                    UPDATE turn_outbox
                    SET state = 'delivered', lease_id = '', lease_expires_at = 0,
                        updated_at = ?, last_error = ''
                    WHERE turn_id = ? AND state = 'pending' AND lease_id = ?
                    """,
                    (now, claim["turn_id"], claim["lease_id"]),
                )
            elif outcome == "timeout":
                # A deadline-aware request returned an ambiguous timeout. No
                # local callback continues, but the remote exact PUT may have
                # been accepted, so preserve the lease until expiry.
                updated = connection.execute(
                    """
                    UPDATE turn_outbox SET updated_at = ?,
                        last_error = 'delivery_outcome_unknown'
                    WHERE turn_id = ? AND state = 'pending' AND lease_id = ?
                    """,
                    (now, claim["turn_id"], claim["lease_id"]),
                )
            else:
                error_code = (
                    "delivery_rejected" if outcome == "rejected"
                    else (
                        "delivery_budget_exhausted"
                        if outcome == "budget_exhausted"
                        else "delivery_exception"
                    )
                )
                updated = connection.execute(
                    """
                    UPDATE turn_outbox SET lease_id = '', lease_expires_at = 0,
                        updated_at = ?, last_error = ?
                    WHERE turn_id = ? AND state = 'pending' AND lease_id = ?
                    """,
                    (now, error_code, claim["turn_id"], claim["lease_id"]),
                )
            if outcome == "accepted" and updated.rowcount == 1:
                self._remaining_seconds(deadline_monotonic)
                delivered_rows = connection.execute(
                    """
                    SELECT turn_id FROM turn_outbox WHERE state = 'delivered'
                    ORDER BY updated_at DESC, turn_id DESC
                    """
                ).fetchall()
                for stale in delivered_rows[self.max_delivered:]:
                    self._remaining_seconds(deadline_monotonic)
                    connection.execute(
                        "DELETE FROM turn_outbox "
                        "WHERE turn_id = ? AND state = 'delivered'",
                        (stale["turn_id"],),
                    )
            self._apply_busy_deadline(connection, deadline_monotonic)
            connection.commit()
            return bool(outcome == "accepted" and updated.rowcount == 1)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def drain(
        self,
        deliver: Any,
        *,
        limit: int = 16,
        timeout_seconds: float = 0.25,
        lease_seconds: float | None = None,
    ) -> int:
        """Attempt rows within one cooperative DB-and-delivery wall budget."""

        row_limit = max(1, min(int(limit), 100))
        call_timeout = max(0.01, min(float(timeout_seconds), 1.0))
        lease_ttl = max(
            0.05,
            min(
                float(lease_seconds) if lease_seconds is not None else call_timeout * 4,
                60.0,
            ),
        )
        delivered_count = 0
        deadline = time.monotonic() + call_timeout
        finalization_reserve = min(0.05, max(0.005, call_timeout * 0.25))
        attempted_turn_ids: set[str] = set()
        for _index in range(row_limit):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                claim = self._claim_one(
                    lease_seconds=lease_ttl,
                    excluded_turn_ids=tuple(attempted_turn_ids),
                    deadline_monotonic=deadline,
                )
            except BaseException:
                # Lock acquisition, local validation, and filesystem failures
                # are represented only by the fixed zero-delivery result.
                break
            if claim is None:
                break
            attempted_turn_ids.add(str(claim["turn_id"]))
            remaining = deadline - time.monotonic()
            if remaining <= finalization_reserve:
                outcome = "budget_exhausted"
            else:
                outcome = self._cooperative_delivery(
                    deliver,
                    claim["payload"],
                    remaining - finalization_reserve,
                )
            try:
                delivered_count += int(self._finish_claim(
                    claim, outcome, deadline_monotonic=deadline,
                ))
            except BaseException:
                # The exact lease remains safe for bounded recovery. Never leak
                # a database or callback error to the post-turn path.
                break
        return delivered_count

    def snapshot(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT turn_id, envelope_sha256, payload_json, state, attempts,
                    created_at, updated_at, last_error, lease_id, lease_expires_at
                FROM turn_outbox ORDER BY created_at, turn_id
                """
            ).fetchall()
            return [
                {
                    "turn_id": row["turn_id"],
                    "envelope_sha256": row["envelope_sha256"],
                    "payload": json.loads(row["payload_json"]),
                    "state": row["state"],
                    "attempts": int(row["attempts"]),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                    "last_error": row["last_error"],
                    "lease_id": row["lease_id"],
                    "lease_expires_at": float(row["lease_expires_at"]),
                }
                for row in rows
            ]
        finally:
            connection.close()


def derive_hermes_turn_id(
    *,
    session_id: str,
    turn_id: str = "",
    task_id: str = "",
    user_message: str = "",
    assistant_response: str = "",
    conversation_history: Any = None,
    model: str = "",
    platform: str = "",
) -> str:
    """Derive a stable non-secret turn identifier from host lifecycle data."""

    supplied = str(turn_id or "").strip()
    if supplied:
        return supplied
    session = str(session_id or "").strip()
    task = str(task_id or "").strip()
    if task:
        anchor: dict[str, Any] = {
            "schema": "hermes-hook-turn-v1",
            "session_id": session,
            "task_id": task,
        }
    else:
        anchor = {
            "schema": "hermes-hook-turn-legacy-v1",
            "session_id": session,
            "platform": str(platform or ""),
            "model": str(model or ""),
            "user_message": str(user_message or ""),
            "assistant_response": str(assistant_response or ""),
            "conversation_history": conversation_history or [],
        }
    canonical = json.dumps(
        anchor,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )
    return "hermes:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_env_placeholder(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("${") and text.endswith("}") and len(text) > 3:
        return os.environ.get(text[2:-1], "")
    return text


class ColonyClient:
    """Small synchronous client; effect endpoints are intentionally absent."""

    def __init__(self, url: str | None = None, api_key: str | None = None):
        self.url = str(
            url or os.environ.get("COLONY_URL") or "http://127.0.0.1:7777"
        ).rstrip("/")
        self._api_key = _resolve_env_placeholder(
            api_key if api_key is not None else os.environ.get("COLONY_API_KEY", "")
        )

    def _headers(self, supplied: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = dict(supplied or {})
        if self._api_key:
            headers.setdefault("Authorization", f"Bearer {self._api_key}")
        return headers

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        timeout = kwargs.pop("timeout", 5)
        headers = self._headers(kwargs.pop("headers", None))
        with httpx.Client(timeout=timeout) as client:
            return client.get(f"{self.url}{path}", headers=headers, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        timeout = kwargs.pop("timeout", 5)
        headers = self._headers(kwargs.pop("headers", None))
        deadline = kwargs.pop("_deadline_monotonic", None)
        transport = (
            _AbsoluteDeadlineHTTPTransport(float(deadline))
            if deadline is not None else None
        )
        with httpx.Client(
            timeout=timeout, transport=transport, trust_env=deadline is None,
        ) as client:
            return client.post(f"{self.url}{path}", headers=headers, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        timeout = kwargs.pop("timeout", 5)
        headers = self._headers(kwargs.pop("headers", None))
        deadline = kwargs.pop("_deadline_monotonic", None)
        transport = (
            _AbsoluteDeadlineHTTPTransport(float(deadline))
            if deadline is not None else None
        )
        with httpx.Client(
            timeout=timeout, transport=transport, trust_env=deadline is None,
        ) as client:
            return client.put(f"{self.url}{path}", headers=headers, **kwargs)

    def sync_turn(
        self,
        *,
        session_id: str,
        contact_id: str,
        user_message: str = "",
        assistant_message: str = "",
        tools_used: Sequence[str] | None = None,
        topics: Sequence[str] | None = None,
        entities: Sequence[str] | None = None,
        summary: str = "",
        model: str = "",
        turn_id: str = "",
        sender: Mapping[str, str] | None = None,
        timeout_seconds: float = 0.25,
    ) -> bool:
        """Persist one participant-bound observation through Colony's ledger."""

        try:
            timeout = max(0.01, min(float(timeout_seconds), 1.0))
            deadline = time.monotonic() + timeout
            session = str(session_id or "").strip()
            contact = str(contact_id or "").strip()
            if not session or not contact:
                return False
            payload: dict[str, Any] = {
                "identity": {"host_id": "hermes"},
                "context": {
                    "session_id": session,
                    "contact_id": contact,
                    **({"turn_id": str(turn_id)} if turn_id else {}),
                },
            }
            if sender:
                platform = str(sender.get("platform") or "").strip()
                user_id = str(sender.get("user_id") or "").strip()
                if platform and user_id:
                    payload["sender"] = {"platform": platform, "user_id": user_id}
            if user_message:
                payload["user_message"] = {"role": "user", "content": str(user_message)}
            if assistant_message:
                payload["assistant_message"] = {
                    "role": "assistant", "content": str(assistant_message),
                }
            if tools_used:
                payload["tools_used"] = [str(item) for item in tools_used][:50]
            if topics:
                payload["topics"] = [str(item) for item in topics][:50]
            if entities:
                payload["entities"] = [str(item) for item in entities][:50]
            if summary:
                payload["summary"] = str(summary)
            if model:
                payload["model"] = str(model)

            if turn_id:
                response = self.put(
                    f"/v2/host/turns/{quote(str(turn_id), safe='')}",
                    json=payload,
                    timeout=min(timeout, max(0.0, deadline - time.monotonic())),
                    _deadline_monotonic=deadline,
                )
            else:
                response = self.post(
                    "/v1/host/turns/sync", json=payload, timeout=timeout,
                    _deadline_monotonic=deadline,
                )
            if time.monotonic() >= deadline:
                raise TurnDeliveryOutcomeUnknown(
                    "participant-bound turn outcome is unknown"
                )
            response.raise_for_status()
            value = response.json()
            if time.monotonic() >= deadline:
                raise TurnDeliveryOutcomeUnknown(
                    "participant-bound turn outcome is unknown"
                )
            return bool(isinstance(value, Mapping) and value.get("accepted"))
        except TurnDeliveryOutcomeUnknown:
            raise
        except httpx.TimeoutException:
            raise TurnDeliveryOutcomeUnknown(
                "participant-bound turn outcome is unknown"
            ) from None
        except BaseException as error:
            logger.debug(
                "participant-bound turn sync failed (%s)", type(error).__name__,
            )
            return False


__all__ = [
    "ColonyClient",
    "PrivateSQLitePath",
    "PrivateSQLitePathError",
    "TurnOutbox",
    "TurnOutboxConflict",
    "TurnOutboxFull",
    "TurnOutboxPayloadError",
    "TurnDeliveryOutcomeUnknown",
    "derive_hermes_turn_id",
]
