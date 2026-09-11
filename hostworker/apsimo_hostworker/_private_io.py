"""Owner-only file and loopback-origin primitives for host workers.

Everything here is stdlib-only, side-effect-free beyond reading the named
file, and deliberately paranoid: these primitives sit under the credential
loader (:mod:`colony_hostworker.client`) and the dispatch-admission check
(:mod:`colony_hostworker.admission`), where a symlink race, a
group-readable secret, or an over-large document must fail closed rather
than degrade.

The bounded JSON reader is also the only JSON decoder the client uses for
network responses: it refuses duplicate object keys, non-finite numbers,
oversized integers, control characters, and unbounded nesting before any
value reaches a validator.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import stat
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

PRIVATE_DOCUMENT_MAX_BYTES = 16 * 1024


class PrivateIOError(RuntimeError):
    """An owner-only file or origin failed its safety contract."""


def strict_json_bytes(raw: bytes, *, maximum: int, error=PrivateIOError) -> Any:
    """Decode one bounded finite JSON value without duplicate object keys."""

    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise error("JSON document exceeds its safety bound")

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise error("JSON object keys must be unique")
            result[key] = value
        return result

    def constant(_value):
        raise error("JSON numbers must be finite")

    def integer(value):
        if len(value.lstrip("-")) > 19:
            raise error("JSON integer exceeds its safety bound")
        parsed = int(value)
        if abs(parsed) > (1 << 63) - 1:
            raise error("JSON integer exceeds its safety bound")
        return parsed

    def floating(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise error("JSON numbers must be finite")
        return parsed

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_int=integer,
            parse_float=floating,
        )
    except error:
        raise
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise error("JSON document is malformed") from exc

    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 1024 or depth > 16:
            raise error("JSON document is too complex")
        if isinstance(item, str):
            if len(item) > 8192 or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in item
            ):
                raise error("JSON text is unsafe")
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise error("JSON text is not UTF-8") from exc
        elif isinstance(item, Mapping):
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise error("JSON document contains an unsafe value")
    return value


def safe_ancestry(
    path: Path, *, label: str, error=PrivateIOError
) -> tuple[tuple[int, int, int], ...]:
    """Attest every existing ancestor without resolving through a symlink."""

    chain = []
    cursor = path
    while True:
        chain.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    identities = []
    try:
        for candidate in reversed(chain):
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise error("%s path ancestry is unsafe" % label)
            identities.append((info.st_dev, info.st_ino, info.st_mode))
    except error:
        raise
    except OSError as exc:
        raise error("%s path ancestry is unavailable" % label) from exc
    return tuple(identities)


def read_private_json(
    path: str, *, label: str, error=PrivateIOError
) -> tuple[Path, bytes, Any]:
    """Read an owner-only mode-0600 regular file without racing a symlink.

    The file must be a regular file owned by the calling user with mode
    exactly ``0600``, at most :data:`PRIVATE_DOCUMENT_MAX_BYTES` long, whose
    identity (device, inode, size, mtime) and whole directory ancestry are
    unchanged across the read.  Anything else fails closed.
    """

    configured = str(path or "").strip()
    if not configured:
        raise error("%s file is not configured" % label)
    target = Path(os.path.abspath(os.path.expanduser(configured)))
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_CLOEXEC"):
        raise error("private file loading is unsupported")
    descriptor = None
    try:
        ancestry_before = safe_ancestry(target.parent, label=label, error=error)
        initial = target.lstat()
        if stat.S_ISLNK(initial.st_mode):
            raise error("%s must not be a symlink" % label)
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
            or before.st_size < 0
            or before.st_size > PRIVATE_DOCUMENT_MAX_BYTES
            or (initial.st_dev, initial.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise error("%s must be an owned mode-0600 regular file" % label)
        collected = bytearray()
        while len(collected) <= PRIVATE_DOCUMENT_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, PRIVATE_DOCUMENT_MAX_BYTES + 1 - len(collected)),
            )
            if not chunk:
                break
            collected.extend(chunk)
        after = os.fstat(descriptor)
        current = target.lstat()
        ancestry_after = safe_ancestry(target.parent, label=label, error=error)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_uid,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
        ) == (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
        )
        if (
            len(collected) > PRIVATE_DOCUMENT_MAX_BYTES
            or len(collected) != before.st_size
            or not stable
            or ancestry_before != ancestry_after
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise error("%s changed while being read" % label)
        raw = bytes(collected)
    except error:
        raise
    except OSError as exc:
        raise error("%s file is unavailable" % label) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return (
        target,
        raw,
        strict_json_bytes(raw, maximum=PRIVATE_DOCUMENT_MAX_BYTES, error=error),
    )


def loopback_origin(value: str, *, error=PrivateIOError) -> str:
    """Return ``value`` iff it is one canonical loopback HTTP(S) origin.

    Exactly ``scheme://host[:port]`` with a loopback host: no path, query,
    fragment, userinfo, whitespace, or non-printable bytes.  Everything a
    governed-action client or admission may talk to must pass this, so a
    configuration mistake can never point owner-authorized mutations at a
    remote host.
    """

    raw = str(value or "")
    if (
        not raw
        or raw != raw.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in raw)
    ):
        raise error("service origin is invalid")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise error("service origin is invalid") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.netloc
    ):
        raise error("service origin must be one loopback origin")
    hostname = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if not loopback or (port is not None and not 1 <= port <= 65535):
        raise error("service origin must be loopback")
    if urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")) != raw:
        raise error("service origin must be canonical")
    return raw


__all__ = (
    "PRIVATE_DOCUMENT_MAX_BYTES",
    "PrivateIOError",
    "loopback_origin",
    "read_private_json",
    "safe_ancestry",
    "strict_json_bytes",
)
