"""Reach-out delivery policy: sanitisation, staleness, quiet-hours urgency.

These are the quality/safety guards applied to a user-facing reach-out
initiative *before* it is handed to the guarded Hermes delivery path. They are
generic and deployment-agnostic (patterns + env-tunable thresholds, no
hardcoded identities or messages).

* sanitise  -- strip raw conversation / skill / control markup from the
               outward-facing text so Hermes composes from clean intent.
* staleness -- drop reach-outs whose subject is older than a max age; a late
               autonomous ping about a two-week-stale item is noise.
* quiet     -- reach-out must respect quiet hours by default; a high
               stale-age priority must NOT silently bypass the quiet window.
               Only an explicit urgent signal may exceed the cap.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------

# ``<<RCSCTX ...>>`` and any other ``<<...>>`` control block, terminated or not
# (titles are truncated so the closing ``>>`` is frequently missing).
_ANGLE_BLOCK = re.compile(r"<<.*?>>", re.DOTALL)
_ANGLE_UNTERMINATED = re.compile(r"<<[^>]*$", re.DOTALL)

# Bracketed system/skill directives, e.g.
#   [IMPORTANT: The user has invoked the "colony-operations" skill ...]
#   [System note: Your previous turn was interrupted ...]
_BRACKET_DIRECTIVE = re.compile(
    r"\[\s*(?:IMPORTANT|SYSTEM\s*NOTE|SYSTEM|INTERNAL|CONTEXT|NOTE)\b[^\]]*\]",
    re.IGNORECASE | re.DOTALL,
)
_BRACKET_UNTERMINATED = re.compile(
    r"\[\s*(?:IMPORTANT|SYSTEM\s*NOTE|SYSTEM|INTERNAL|CONTEXT|NOTE)\b[^\]]*$",
    re.IGNORECASE | re.DOTALL,
)

# Non-printable / control characters (keep tab/newline out of outward text too).
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WS = re.compile(r"\s+")

_SANITIZE_FIELDS = ("title", "description", "rationale", "suggested_action")


def sanitize_text(text: Optional[str]) -> str:
    """Strip control/conversation/skill markup and collapse whitespace."""
    if not text:
        return ""
    s = str(text)
    s = _ANGLE_BLOCK.sub(" ", s)
    s = _ANGLE_UNTERMINATED.sub(" ", s)
    s = _BRACKET_DIRECTIVE.sub(" ", s)
    s = _BRACKET_UNTERMINATED.sub(" ", s)
    s = _CONTROL.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


def sanitize_payload(payload: dict) -> dict:
    """Return a copy of the initiative payload with its text fields cleaned."""
    out = dict(payload)
    for field in _SANITIZE_FIELDS:
        value = out.get(field)
        if isinstance(value, str):
            out[field] = sanitize_text(value)
    if not out.get("title"):
        out["title"] = (out.get("description") or "")[:80] or "(follow-up)"
    return out


# ---------------------------------------------------------------------------
# Meaningful-content gate (applied at reach-out GENERATION time)
# ---------------------------------------------------------------------------

# Minimum real alphanumeric substance a reach-out source must retain after
# sanitisation to be worth generating an initiative from. Low enough to keep
# legitimately short subjects ("Call Bob"), high enough to drop fragments.
_MIN_MEANINGFUL_CHARS = 4

# Markers that a source turn is system / skill / non-conversational, i.e. not
# genuine conversation or a real commitment. These identify origins that
# sanitisation alone might leave partial residue from.
_SYSTEM_ORIGIN = re.compile(
    r"\binvoked\b.{0,60}?\bskill\b"          # "... invoked the <name> skill ..."
    r"|previous turn was interrupted"
    r"|\bsystem note\b"
    r"|\bsystem[- ]?generated\b"
    r"|conversation (?:was )?(?:interrupted|truncated|reset)"
    r"|<\s*/?\s*system[\s>]",                 # <system> ... </system> style tags
    re.IGNORECASE | re.DOTALL,
)


def is_system_origin(text: Optional[str]) -> bool:
    """True if the raw source text is a system/skill/non-conversational turn."""
    return bool(text) and bool(_SYSTEM_ORIGIN.search(str(text)))


def meaningful_reachout_text(text: Optional[str]) -> str:
    """Clean reach-out source text, or "" if it is not worth surfacing.

    Returns the sanitised text when the source is genuine, human-meaningful
    content; returns "" when the source is empty, of system/skill origin, or
    has no real substance after sanitisation. Use at reach-out GENERATION time
    so near-empty / system-origin junk never becomes an initiative.
    """
    if not text or is_system_origin(text):
        return ""
    cleaned = sanitize_text(text)
    substance = re.sub(r"[^0-9a-z]+", "", cleaned.lower())
    if len(substance) < _MIN_MEANINGFUL_CHARS:
        return ""
    return cleaned


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

# Structured "how overdue is the subject" signals. Deliberately excludes
# contact-recency signals (e.g. days_since_contact) which are a *reason* to
# reach out, not a disqualifier.
_STALENESS_KEYS = (
    "days_pending", "days_overdue", "days_stale", "stale_days", "age_days",
)


def _scan_staleness(obj: Any, depth: int = 2) -> float:
    best = 0.0
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _STALENESS_KEYS and isinstance(value, (int, float)):
                best = max(best, float(value))
            elif depth > 0 and isinstance(value, (dict, list)):
                best = max(best, _scan_staleness(value, depth - 1))
    elif isinstance(obj, list) and depth > 0:
        for value in obj:
            best = max(best, _scan_staleness(value, depth - 1))
    return best


def reachout_age_days(payload: dict) -> float:
    """Effective age of a reach-out: max of structured subject-staleness and
    the initiative's own age since generation."""
    age = _scan_staleness(payload.get("context") or {})
    generated_at = payload.get("generated_at")
    if generated_at:
        try:
            ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            init_age = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
            age = max(age, init_age)
        except Exception:
            pass
    return age


def max_age_days() -> float:
    try:
        return float(os.environ.get("COLONY_REACHOUT_MAX_AGE_DAYS", "7"))
    except (TypeError, ValueError):
        return 7.0


def is_aged_out(payload: dict) -> bool:
    """True if this reach-out is too old to be worth surfacing."""
    return reachout_age_days(payload) > max_age_days()


# ---------------------------------------------------------------------------
# Quiet-hours urgency
# ---------------------------------------------------------------------------

# The rate limiter bypasses quiet hours at urgency >= 0.9. Reach-out urgency is
# derived from priority, which for stale items pins to 1.0 -- an artifact, not a
# genuine "wake them up" signal. Cap it below the bypass threshold by default.
def _urgency_cap() -> float:
    try:
        return float(os.environ.get("COLONY_REACHOUT_URGENCY_CAP", "0.85"))
    except (TypeError, ValueError):
        return 0.85


def _explicit_urgent(payload: dict) -> bool:
    if payload.get("urgent") is True or payload.get("bypass_quiet_hours") is True:
        return True
    ctx = payload.get("context") or {}
    if isinstance(ctx, dict):
        return ctx.get("urgent") is True or ctx.get("bypass_quiet_hours") is True
    return False


def quiet_hours_urgency(payload: dict, raw_urgency: float) -> float:
    """Urgency to use for the delivery gate. Reach-out respects quiet hours
    unless an explicit urgent flag is set on the initiative/context."""
    if _explicit_urgent(payload):
        return raw_urgency
    return min(raw_urgency, _urgency_cap())
