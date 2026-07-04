"""TrustEngine -- graduated, earned autonomy per action class (Amendment 1).

Replaces static capability gating with calibrated trust:

- Every action class (domain) has a confidence score computed from REAL
  outcomes (Laplace-smoothed success rate over non-shadow events), scaled by
  owner reaction (TypeFeedbackStore multiplier) and penalized by audit
  violations.
- Each domain carries a stage: shadow (calibration) -> ask_first -> act_first.
  `gate()` turns (stage, confidence, floor) into a decision: act | ask | hold,
  and journals it. Below-threshold confidence asks even at act_first.
- Auto-graduation: clean calibration promotes shadow -> ask_first; a real
  track record promotes ask_first -> act_first. Each graduation queues an
  owner NOTIFICATION (not a permission request) drained through the guarded
  delivery path.
- Circuit breaker: N real failures inside the window, or ANY audit
  violation, demotes the class to ask_first and journals why.
- Immutable floor: money movement, non-recoverable deletion,
  credential/security changes, and bulk third-party messaging are never
  self-decidable, regardless of confidence.

The env COLONY_<X>_MODE remains the owner override: "off" stays off and
"live" is live; "shadow" means "start in calibration and earn upward".
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from colony_sidecar.self_model.store import CompetenceStore

logger = logging.getLogger(__name__)

STAGES = ("shadow", "ask_first", "act_first")

# Immutable floor (Amendment 1.6): kept SMALL and principled
# (irreversibility x blast radius). Matched conservatively on action text.
_FLOOR_PATTERNS: Dict[str, re.Pattern] = {
    "money_movement": re.compile(
        r"\b(?:wire|transfer|send|move)\s+(?:\$|money|funds|payment)|"
        r"\b(?:purchase|buy|pay|spend|subscribe)\b.{0,30}\$|"
        r"\bpayment\s+(?:of|for)\b|\$\d{2,}", re.IGNORECASE),
    "irreversible_deletion": re.compile(
        r"\b(?:rm\s+-rf|drop\s+(?:table|database)|delete\s+permanently|"
        r"wipe|purge\s+all|force[- ]?push|erase\s+(?:all|everything))\b",
        re.IGNORECASE),
    "credential_change": re.compile(
        r"\b(?:rotate|change|reset|revoke|create)\b.{0,40}\b(?:credential|"
        r"password|api[_ ]?key|secret|token|ssh[- ]?key|certificate)\b|"
        r"\bsecurity\s+settings?\b", re.IGNORECASE),
    "bulk_third_party_messaging": re.compile(
        r"\b(?:bulk|mass|broadcast|blast|everyone|all\s+contacts)\b.{0,40}"
        r"\b(?:message|text|email|sms|dm)\b|"
        r"\b(?:message|text|email|sms|dm)\b.{0,40}\b(?:bulk|mass|broadcast|"
        r"blast|everyone|all\s+contacts)\b", re.IGNORECASE),
}


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _ienv(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def autograduate_enabled() -> bool:
    return os.environ.get(
        "COLONY_TRUST_AUTOGRADUATE", "true").strip().lower() != "false"


def floor_class(text: str) -> Optional[str]:
    """The immutable-floor class this action text falls into, if any."""
    t = (text or "")
    for name, pat in _FLOOR_PATTERNS.items():
        if pat.search(t):
            return name
    return None


class TrustEngine:
    def __init__(self, store: CompetenceStore, *,
                 db_path: Optional[str] = None,
                 feedback_store: Any = None,
                 journal: Any = None) -> None:
        self._store = store
        self._feedback = feedback_store
        self._journal = journal
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS trust_stage (
                    domain TEXT PRIMARY KEY, stage TEXT NOT NULL,
                    demotions INTEGER DEFAULT 0,
                    graduated_at REAL, updated_at REAL
                )""")
            # Durable notice queue: a graduation/demotion notice must survive
            # restarts until the owner actually receives it (Amendment 1.2).
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS trust_notices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT, stage TEXT, prior TEXT,
                    demotion INTEGER DEFAULT 0, reason TEXT,
                    ts REAL, delivered INTEGER DEFAULT 0
                )""")
            self._conn.commit()
        # Back-compat in-process mirror (tests peek at it); the durable queue
        # in trust_notices is authoritative.
        self.pending_notices: deque = deque(maxlen=50)

    # -- durable notice queue ----------------------------------------------
    def _queue_notice(self, notice: Dict[str, Any]) -> None:
        self.pending_notices.append(notice)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO trust_notices (domain, stage, prior, "
                    "demotion, reason, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    (notice.get("domain"), notice.get("stage"),
                     notice.get("prior"), 1 if notice.get("demotion") else 0,
                     notice.get("reason"), notice.get("ts")))
                self._conn.commit()
        except Exception:
            logger.debug("trust notice persist failed", exc_info=True)

    def undelivered_notices(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trust_notices WHERE delivered=0 "
                "ORDER BY ts ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def mark_notice_delivered(self, notice_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trust_notices SET delivered=1 WHERE id=?",
                (notice_id,))
            self._conn.commit()

    # -- confidence -------------------------------------------------------
    def confidence(self, domain: str) -> float:
        """Calibrated confidence for a domain from REAL (non-shadow) outcomes."""
        events = self._store.events(domain, include_shadow=False)
        n = len(events)
        wins = sum(1 for e in events if e["outcome"] == "success")
        violations = sum(1 for e in events if e.get("violation"))
        conf = (wins + 1.0) / (n + 2.0)          # Laplace-smoothed
        if self._feedback is not None:
            try:
                mult = float(self._feedback.multiplier(domain))
                conf *= max(0.7, min(1.2, mult))  # owner reaction, clamped
            except Exception:
                pass
        conf -= 0.15 * min(violations, 3)         # audit violations bite
        return max(0.0, min(1.0, conf))

    # -- stage persistence --------------------------------------------------
    def stage(self, domain: str, default: str = "shadow") -> str:
        domain = (domain or "unknown").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT stage FROM trust_stage WHERE domain=?",
                (domain,)).fetchone()
        if row is None:
            return default if default in STAGES else "shadow"
        return row["stage"]

    def set_stage(self, domain: str, stage: str, *, reason: str = "",
                  demotion: bool = False, notify: bool = True) -> None:
        domain = (domain or "unknown").strip().lower()
        if stage not in STAGES:
            return
        prior = self.stage(domain, default="")
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO trust_stage (domain, stage, demotions,
                                            graduated_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET
                     stage=?, demotions=demotions+?, graduated_at=?,
                     updated_at=?""",
                (domain, stage, 1 if demotion else 0, now, now,
                 stage, 1 if demotion else 0, now, now))
            self._conn.commit()
        if prior == stage:
            return
        verb = "demoted" if demotion else "graduated"
        logger.info("Trust: %s %s %s -> %s (%s)", domain, verb,
                    prior or "(unset)", stage, reason or "threshold")
        if self._journal is not None:
            self._journal.record(
                "trust", f"{domain} {verb} to {stage}",
                reasoning=reason, confidence=self.confidence(domain),
                decision="noted")
        if notify:
            self._queue_notice({
                "domain": domain, "stage": stage, "prior": prior,
                "demotion": demotion, "reason": reason, "ts": now,
            })

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trust_stage ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["confidence"] = round(self.confidence(d["domain"]), 3)
            out.append(d)
        return out

    # -- the gate -----------------------------------------------------------
    def gate(self, domain: str, description: str, *,
             reasoning: str = "", reversibility: str = "reversible",
             default_stage: str = "shadow", ref: str = "") -> Dict[str, Any]:
        """Decide act | ask | hold for one concrete action, and journal it.

        The immutable floor always asks. Shadow (calibration) holds. ask_first
        asks. act_first acts when confidence clears the threshold, otherwise
        asks (uncertainty asks; it never silently drops work).
        """
        domain = (domain or "unknown").strip().lower()
        floor = floor_class(f"{description} {reasoning}")
        conf = self.confidence(domain)
        stage = self.stage(domain, default=default_stage)
        if floor:
            decision = "ask"
            why = f"immutable floor ({floor}): never self-decidable"
        elif stage == "shadow":
            decision = "hold"
            why = "calibration stage"
        elif stage == "ask_first":
            decision = "ask"
            why = f"ask_first stage (confidence {conf:.2f})"
        else:  # act_first
            if conf >= _fenv("COLONY_TRUST_ACT_THRESHOLD", 0.8):
                decision = "act"
                why = f"act_first, confidence {conf:.2f}"
            else:
                decision = "ask"
                why = f"act_first but confidence {conf:.2f} below threshold"
        journal_id = -1
        if self._journal is not None:
            journal_id = self._journal.record(
                domain, description, reasoning=reasoning or why,
                confidence=conf, reversibility=reversibility,
                decision={"act": "acted", "ask": "asked",
                          "hold": "held"}[decision],
                ref=ref)
        return {"decision": decision, "stage": stage, "confidence": conf,
                "floor": floor, "why": why, "journal_id": journal_id}

    # -- breaker + graduation (invoked after every recorded outcome) --------
    def after_outcome(self, domain: str) -> None:
        domain = (domain or "unknown").strip().lower()
        window = _fenv("COLONY_TRUST_BREAKER_WINDOW_HOURS", 24.0) * 3600.0
        recent = self._store.events(domain, since=time.time() - window,
                                    include_shadow=False)
        failures = sum(1 for e in recent
                       if e["outcome"] in ("failure", "timeout"))
        violations = sum(1 for e in recent if e.get("violation"))
        stage = self.stage(domain)
        # Circuit breaker: demote on violations or clustered failures.
        if stage == "act_first" and (
                violations > 0
                or failures >= _ienv("COLONY_TRUST_BREAKER_FAILURES", 3)):
            self.set_stage(
                domain, "ask_first", demotion=True,
                reason=(f"{violations} violation(s)" if violations
                        else f"{failures} failures in "
                             f"{window / 3600:.0f}h window"))
            return
        if not autograduate_enabled():
            return
        # Graduation.
        if stage == "shadow":
            cal = self._store.events(domain, include_shadow=True)
            n = len(cal)
            bad = sum(1 for e in cal if e["outcome"] != "success")
            if n >= _ienv("COLONY_TRUST_ASK_MIN_N", 3) and bad == 0:
                self.set_stage(domain, "ask_first",
                               reason=f"{n} clean calibration run(s)")
        elif stage == "ask_first":
            real = self._store.events(domain, include_shadow=False)
            conf = self.confidence(domain)
            if (len(real) >= _ienv("COLONY_TRUST_ACT_MIN_N", 5)
                    and conf >= _fenv("COLONY_TRUST_ACT_THRESHOLD", 0.8)
                    and not any(e.get("violation") for e in real)):
                self.set_stage(domain, "act_first",
                               reason=f"confidence {conf:.2f} over "
                                      f"{len(real)} real outcomes")

    # -- adaptive delivery cap (Amendment 1.6) -------------------------------
    def delivery_cap(self, base: int) -> int:
        """Per-recipient daily cap: base, earned upward with delivery track
        record, bounded by COLONY_TRUST_DELIVERY_CAP_MAX."""
        cap_max = _ienv("COLONY_TRUST_DELIVERY_CAP_MAX", 6)
        real = self._store.events("delivery", include_shadow=False)
        if len(real) < 10:
            return base
        conf = self.confidence("delivery")
        if conf <= 0.8:
            return base
        extra = int((conf - 0.8) * 20)  # 0.85 -> +1, 0.9 -> +2, 0.95 -> +3
        return max(base, min(cap_max, base + extra))
