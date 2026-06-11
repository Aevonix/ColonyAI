"""Temporal reference frame for Colony (v0.21.0).

The single source of truth for "now" and timezone handling across the sidecar.
Everything that needs to reason about wall-clock time — context assembly, the
timeline tool, briefings, autonomy triggers — should go through this module so
the agent gets a consistent, editable sense of time.

Three independent, editable timezones (per the design):
  * AGENT timezone   — where the Colony agent "lives" (its home reference frame).
                       Editable; defaults to COLONY_AGENT_TIMEZONE, then the host
                       system tz, then UTC.
  * CONTACT timezone — a per-contact field (contacts.timezone). Editable per
                       contact. Lets the agent reason "it's 9am my time / 3pm
                       theirs".
  * COMMUNICATION tz — resolved per turn: an explicit override on the turn, else
                       the contact's tz, else the agent tz.

Stdlib only (datetime + zoneinfo). No third-party deps.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:  # py3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover - very old python
    ZoneInfo = None  # type: ignore
    class ZoneInfoNotFoundError(Exception):  # type: ignore
        pass

logger = logging.getLogger(__name__)

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# State (editable agent / default-contact timezone)                           #
# --------------------------------------------------------------------------- #

def _state_dir() -> Path:
    return Path(os.environ.get("COLONY_STATE_DIR", os.path.expanduser("~/.colony")))


def _state_path() -> Path:
    return _state_dir() / "temporal.json"


def _load_state() -> dict:
    try:
        with open(_state_path()) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, p)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to persist temporal state: %s", exc)


def is_valid_timezone(tz: Optional[str]) -> bool:
    if not tz:
        return False
    if tz.upper() == "UTC":
        return True
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


def _system_timezone() -> Optional[str]:
    """Best-effort IANA name of the host's local timezone."""
    # /etc/localtime symlink → .../zoneinfo/<Area>/<City>
    try:
        p = Path("/etc/localtime")
        if p.is_symlink():
            target = os.readlink(p)
            if "zoneinfo/" in target:
                name = target.split("zoneinfo/", 1)[1]
                if is_valid_timezone(name):
                    return name
    except Exception:
        pass
    tz = os.environ.get("TZ")
    if is_valid_timezone(tz):
        return tz
    return None


def agent_timezone() -> str:
    """The agent's home timezone (IANA name). Always returns a usable value."""
    env = os.environ.get("COLONY_AGENT_TIMEZONE")
    if is_valid_timezone(env):
        return env  # type: ignore[return-value]
    stored = _load_state().get("agent_timezone")
    if is_valid_timezone(stored):
        return stored
    sys_tz = _system_timezone()
    if sys_tz:
        return sys_tz
    return "UTC"


def set_agent_timezone(tz: str) -> str:
    """Persist the agent home timezone. Raises ValueError if invalid."""
    if not is_valid_timezone(tz):
        raise ValueError(f"Invalid IANA timezone: {tz!r}")
    state = _load_state()
    state["agent_timezone"] = tz
    _save_state(state)
    return tz


def default_contact_timezone() -> Optional[str]:
    """Fallback tz for contacts that have none set (optional)."""
    env = os.environ.get("COLONY_DEFAULT_CONTACT_TIMEZONE")
    if is_valid_timezone(env):
        return env  # type: ignore[return-value]
    stored = _load_state().get("default_contact_timezone")
    return stored if is_valid_timezone(stored) else None


def set_default_contact_timezone(tz: Optional[str]) -> Optional[str]:
    if tz is not None and not is_valid_timezone(tz):
        raise ValueError(f"Invalid IANA timezone: {tz!r}")
    state = _load_state()
    state["default_contact_timezone"] = tz
    _save_state(state)
    return tz


def resolve_communication_timezone(
    contact_tz: Optional[str] = None,
    override_tz: Optional[str] = None,
) -> str:
    """Per-communication tz: explicit override → contact → default → agent."""
    for cand in (override_tz, contact_tz, default_contact_timezone()):
        if is_valid_timezone(cand):
            return cand  # type: ignore[return-value]
    return agent_timezone()


# --------------------------------------------------------------------------- #
# Now / conversion                                                            #
# --------------------------------------------------------------------------- #

def _zone(tz: str):
    if tz.upper() == "UTC" or ZoneInfo is None:
        return UTC
    try:
        return ZoneInfo(tz)
    except Exception:
        return UTC


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_in(tz: str) -> datetime:
    return now_utc().astimezone(_zone(tz))


def to_zone(dt: datetime, tz: str) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_zone(tz))


def parse_iso(value) -> Optional[datetime]:
    """Parse an ISO-8601 string (or pass through a datetime) → aware UTC dt."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Human-friendly formatting                                                   #
# --------------------------------------------------------------------------- #

def part_of_day(dt: datetime) -> str:
    h = dt.hour
    if h < 5:
        return "the middle of the night"
    if h < 9:
        return "early morning"
    if h < 12:
        return "morning"
    if h < 14:
        return "midday"
    if h < 18:
        return "afternoon"
    if h < 22:
        return "evening"
    return "night"


def format_clock(dt: datetime, tz: Optional[str] = None) -> str:
    """e.g. 'Thu, Jun 12, 12:37 AM EDT'."""
    if tz:
        dt = to_zone(dt, tz)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    label = dt.strftime("%Z") or "UTC"
    # %-I is platform-specific; strip a leading zero portably.
    hm = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%a, %b %d')}, {hm} {label}"


def humanize_delta(ts, ref: Optional[datetime] = None) -> str:
    """'6h ago', 'in 2d', 'just now'. Accepts datetime or ISO string."""
    dt = parse_iso(ts)
    if dt is None:
        return "unknown"
    ref = ref or now_utc()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    delta = ref - dt
    secs = delta.total_seconds()
    future = secs < 0
    secs = abs(secs)

    if secs < 45:
        return "just now"
    if secs < 90 * 60:
        val, unit = round(secs / 60), "m"
    elif secs < 36 * 3600:
        val, unit = round(secs / 3600), "h"
    elif secs < 14 * 86400:
        val, unit = round(secs / 86400), "d"
    elif secs < 8 * 7 * 86400:
        val, unit = round(secs / (7 * 86400)), "w"
    elif secs < 365 * 86400:
        val, unit = round(secs / (30 * 86400)), "mo"
    else:
        val, unit = round(secs / (365 * 86400)), "y"
    return f"in {val}{unit}" if future else f"{val}{unit} ago"


def bucket(ts, tz: Optional[str] = None) -> str:
    """Coarse calendar bucket in the given tz: today / yesterday / this week..."""
    dt = parse_iso(ts)
    if dt is None:
        return "unknown"
    tz = tz or agent_timezone()
    local = to_zone(dt, tz)
    today = now_in(tz).date()
    d = (today - local.date()).days
    if d < 0:
        return "upcoming"
    if d == 0:
        return "today"
    if d == 1:
        return "yesterday"
    if d < 7:
        return "earlier this week"
    if d < 14:
        return "last week"
    if d < 31:
        return f"{d // 7}w ago"
    if d < 365:
        return f"{d // 30}mo ago"
    return f"{d // 365}y ago"


def hours_since(ts, ref: Optional[datetime] = None) -> Optional[float]:
    dt = parse_iso(ts)
    if dt is None:
        return None
    ref = ref or now_utc()
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    return (ref - dt).total_seconds() / 3600.0


def describe_now(agent_tz: Optional[str] = None,
                 contact_tz: Optional[str] = None,
                 contact_label: str = "their") -> str:
    """One-line 'now' anchor, optionally with the contact's local time too."""
    atz = agent_tz or agent_timezone()
    a_local = now_in(atz)
    line = f"{format_clock(a_local)} — {part_of_day(a_local)} (your local time, {atz})"
    if contact_tz and is_valid_timezone(contact_tz) and contact_tz != atz:
        c_local = now_in(contact_tz)
        poss = contact_label if contact_label.endswith("s") else f"{contact_label}'s"
        line += f"\nFor {poss} side it is {format_clock(c_local)} — {part_of_day(c_local)} ({contact_tz})."
    return line
