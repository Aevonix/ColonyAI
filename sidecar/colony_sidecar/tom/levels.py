"""Effective-level resolver for leveled cross-contact tom2 (L1.3).

The single place that decides, per conversation and per reader, how much
second-order theory-of-mind may render THIS TURN:

    L0 — owner surfacing only (H3.3; today's behavior).
    L1 — self-reflexive prior: the reader's own knows/unaware rows.
    L2 — third-party epistemic topology via the full eligibility pipeline.

``resolve_effective_level()`` is a pure min-chain over independent brakes —
any ONE of them decaying silently drops the level for the turn, no human
action required:

    effective = min(COLONY_TOM2_LEVEL          [default 0],
                    COLONY_TOM2_MAX_LEVEL      [default 1],
                    cap(environment risk)      [COLONY_TOM2_RISK_CAPS],
                    2 if live-enforce-evidence else 1,
                    2 if COLONY_TOM2_CROSS_CONTEXT else 1)
    ... and 0 on ANY error anywhere.

The enforce-evidence input is an injected probe (``set_evidence_probe``)
that later tranches wire to GuardAuditStore.enforce_evidence — until then it
safely reports False, which by construction caps the system at L1. Results
are cached for <=60s per (conversation, reader) to keep the hot path cheap;
the cache only ever serves a REsolved grade, never a default.

Everything here is default-inert: with shipped defaults the resolved level
is 0 everywhere, and nothing consumes it yet (wiring is a later tranche).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: cap(env risk) shipped defaults: R0/R1 rooms may reach L2, R2 caps at L1,
#: R3 (open/hostile, the default grade) renders nothing above L0.
DEFAULT_RISK_CAPS = "0:2,1:2,2:1,3:0"

_FAIL_CLOSED_CAPS = {0: 0, 1: 0, 2: 0, 3: 0}

_CACHE_TTL_SECS = 60.0
_CACHE_MAX = 512

_cache: Dict[Any, Any] = {}
_cache_lock = threading.Lock()

#: Injected enforce-evidence probe: Callable[[gateway: str], bool].
#: None (shipped) => no evidence => the min-chain caps at 1. Set by the
#: egress-net tranche once GuardAuditStore.enforce_evidence is wired.
_evidence_probe: Optional[Callable[[str], bool]] = None


def set_evidence_probe(probe: Optional[Callable[[str], bool]]) -> None:
    """Wire (or clear) the live enforce-evidence probe. The probe must
    answer 'is the outbound guard PROVABLY enforcing on this gateway?';
    any probe error is treated as False (no proof, no L2)."""
    global _evidence_probe
    _evidence_probe = probe


def clear_level_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _env_int(name: str, default: int, lo: int = 0, hi: int = 2) -> int:
    """Parse an int env var, clamped; ANY malformation fails closed to 0."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an integer — failing closed to 0",
                       name, raw)
        return 0
    return max(lo, min(hi, v))


def configured_level() -> int:
    """COLONY_TOM2_LEVEL (default 0): the owner's requested level."""
    return _env_int("COLONY_TOM2_LEVEL", 0)


def configured_max_level() -> int:
    """COLONY_TOM2_MAX_LEVEL (default 1): the hard ceiling."""
    return _env_int("COLONY_TOM2_MAX_LEVEL", 1)


def risk_caps_valid() -> bool:
    """True when COLONY_TOM2_RISK_CAPS parses cleanly (doctor surface)."""
    return _parse_risk_caps_strict() is not None


def _parse_risk_caps_strict() -> Optional[Dict[int, int]]:
    raw = os.environ.get("COLONY_TOM2_RISK_CAPS", DEFAULT_RISK_CAPS).strip()
    caps: Dict[int, int] = {}
    try:
        for pair in raw.split(","):
            k, v = pair.split(":")
            risk, cap = int(k.strip()), int(v.strip())
            if risk not in (0, 1, 2, 3) or not (0 <= cap <= 2):
                return None
            if risk in caps:
                return None
            caps[risk] = cap
    except (TypeError, ValueError):
        return None
    if set(caps) != {0, 1, 2, 3}:
        return None
    return caps


def parse_risk_caps() -> Dict[int, int]:
    """Risk->cap map from COLONY_TOM2_RISK_CAPS (default '0:2,1:2,2:1,3:0').

    A malformed value fails closed to all-zero caps (every environment
    renders L0 only) — and the doctor WARNs about it, because silently
    running with all-0 caps while the owner believes their custom caps are
    live is a posture mismatch worth surfacing."""
    caps = _parse_risk_caps_strict()
    if caps is None:
        logger.warning("COLONY_TOM2_RISK_CAPS=%r is malformed — failing "
                       "closed to all-0 caps",
                       os.environ.get("COLONY_TOM2_RISK_CAPS"))
        return dict(_FAIL_CLOSED_CAPS)
    return caps


@dataclass
class LevelResolution:
    level: int                                  # the effective level, 0..2
    env_risk: Optional[int] = None              # 0..3, None on error
    terms: Dict[str, int] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"level": self.level, "env_risk": self.env_risk,
                "terms": dict(self.terms), "reasons": list(self.reasons)}


def _probe_evidence(gateway: str) -> bool:
    """The injected enforce-evidence probe, failed closed on every edge:
    no probe wired => False; probe raises => False."""
    probe = _evidence_probe
    if probe is None:
        return False
    try:
        return bool(probe(gateway))
    except Exception:
        logger.debug("enforce-evidence probe failed (=> no evidence)",
                     exc_info=True)
        return False


async def resolve_effective_level(
    conversation_key: str,
    reader_contact_id: str,
    *,
    presence_store: Any,
    contacts_store: Any,
    owner_id: Optional[str] = None,
    use_cache: bool = True,
) -> LevelResolution:
    """Resolve the effective tom2 level for (conversation, reader).

    Never raises; ANY internal error resolves level 0. Results are cached
    for <=60s per pair (a decayed brake takes effect within a minute)."""
    key = (str(conversation_key or ""), str(reader_contact_id or ""))
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and now - hit[0] <= _CACHE_TTL_SECS:
                return hit[1]
    try:
        resolution = await _resolve(conversation_key, reader_contact_id,
                                    presence_store=presence_store,
                                    contacts_store=contacts_store,
                                    owner_id=owner_id)
    except Exception as exc:
        logger.debug("effective-level resolution failed (=> 0): %s", exc,
                     exc_info=True)
        resolution = LevelResolution(
            level=0, reasons=[f"resolver-error:{type(exc).__name__}"])
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = (now, resolution)
    return resolution


async def _resolve(conversation_key: str, reader_contact_id: str, *,
                   presence_store: Any, contacts_store: Any,
                   owner_id: Optional[str]) -> LevelResolution:
    from colony_sidecar.gate.env_risk import classify
    from colony_sidecar.tom.tom2 import tom2_cross_context_enabled

    terms: Dict[str, int] = {}
    reasons: List[str] = []

    terms["configured"] = configured_level()
    terms["max"] = configured_max_level()

    risk = await classify(conversation_key, reader_contact_id,
                          presence_store=presence_store,
                          contacts_store=contacts_store, owner_id=owner_id)
    caps = parse_risk_caps()
    terms["risk_cap"] = caps.get(risk.level, 0)
    reasons.extend(f"env:{r}" for r in risk.reasons[:4])

    gateway = str(conversation_key or "").split(":", 1)[0]
    terms["enforce_evidence"] = 2 if _probe_evidence(gateway) else 1
    terms["cross_context"] = 2 if tom2_cross_context_enabled() else 1

    level = min(terms.values())
    level = max(0, min(2, level))
    return LevelResolution(level=level, env_risk=risk.level, terms=terms,
                           reasons=reasons)
