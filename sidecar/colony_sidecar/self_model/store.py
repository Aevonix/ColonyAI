"""CompetenceStore -- per-domain outcome counts + latency, from real work.

Domains are capability classes: initiative types ("research", "follow_up"),
"project" (project steps/outcomes), "directed" (directed-action audits),
"delivery" (real pushes), and worker job types (item 5). Every recording site
is a completion/failure chokepoint that already exists; the store never polls.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Exponential moving average weight for latency (recent work dominates).
_EWMA_ALPHA = 0.3

_OUTCOMES = ("success", "failure", "timeout")


def self_model_enabled() -> bool:
    return os.environ.get("COLONY_SELF_MODEL_ENABLED", "true").strip().lower() != "false"


def _norm_outcome(outcome: str) -> str:
    o = (outcome or "").strip().lower()
    if o in ("success", "completed", "clean", "actioned", "done", "ok"):
        return "success"
    if o in ("timeout", "timed_out"):
        return "timeout"
    return "failure"


class CompetenceStore:
    """SQLite persistence of per-domain success/failure/timeout + ewma latency."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS competence (
                    domain TEXT PRIMARY KEY,
                    success INTEGER DEFAULT 0,
                    failure INTEGER DEFAULT 0,
                    timeout INTEGER DEFAULT 0,
                    ewma_latency_secs REAL,
                    last_outcome TEXT,
                    last_outcome_at REAL
                )""")
            # Per-event log for windowed circuit-breaker queries and
            # calibration-vs-real evidence separation (shadow=1 events are
            # calibration runs; they graduate a class out of shadow but never
            # count toward act-first confidence).
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS competence_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL, outcome TEXT NOT NULL,
                    shadow INTEGER DEFAULT 0, violation INTEGER DEFAULT 0,
                    stated_confidence REAL,
                    ts REAL NOT NULL
                )""")
            # Migration: stated_confidence added after first ship.
            try:
                cols = {r[1] for r in self._conn.execute(
                    "PRAGMA table_info(competence_events)").fetchall()}
                if "stated_confidence" not in cols:
                    self._conn.execute(
                        "ALTER TABLE competence_events "
                        "ADD COLUMN stated_confidence REAL")
            except Exception:
                pass
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cev_domain_ts "
                "ON competence_events(domain, ts)")
            self._conn.commit()

    def record(self, domain: str, outcome: str,
               latency_secs: Optional[float] = None,
               shadow: bool = False, violation: bool = False,
               stated_confidence: Optional[float] = None) -> None:
        """Record one outcome for a domain. Never raises.

        stated_confidence: the confidence the model STATED before doing the
        work (charter contract); stored per event so calibration (stated vs
        realized) is measurable and autonomy is earned against it.
        """
        domain = (domain or "unknown").strip().lower()
        outcome = _norm_outcome(outcome)
        now = time.time()
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT ewma_latency_secs FROM competence WHERE domain=?",
                    (domain,)).fetchone()
                ewma = row["ewma_latency_secs"] if row else None
                if latency_secs is not None:
                    ewma = (float(latency_secs) if ewma is None else
                            _EWMA_ALPHA * float(latency_secs) + (1 - _EWMA_ALPHA) * float(ewma))
                self._conn.execute(
                    f"""INSERT INTO competence
                        (domain, {outcome}, ewma_latency_secs, last_outcome, last_outcome_at)
                        VALUES (?, 1, ?, ?, ?)
                        ON CONFLICT(domain) DO UPDATE SET
                          {outcome}={outcome}+1, ewma_latency_secs=?,
                          last_outcome=?, last_outcome_at=?""",
                    (domain, ewma, outcome, now, ewma, outcome, now))
                self._conn.execute(
                    "INSERT INTO competence_events (domain, outcome, shadow, "
                    "violation, stated_confidence, ts) VALUES (?, ?, ?, ?, ?, ?)",
                    (domain, outcome, 1 if shadow else 0,
                     1 if violation else 0, stated_confidence, now))
                # keep the event log bounded (breaker windows are days, not months)
                self._conn.execute(
                    "DELETE FROM competence_events WHERE ts < ?",
                    (now - 90 * 86400,))
                self._conn.commit()
        except Exception as exc:
            logger.debug("competence record failed for %s: %s", domain, exc)

    def events(self, domain: str, since: Optional[float] = None,
               include_shadow: bool = True) -> List[Dict[str, Any]]:
        """Events for a domain, newest first (windowed by `since`)."""
        domain = (domain or "").strip().lower()
        q = "SELECT * FROM competence_events WHERE domain=?"
        params: List[Any] = [domain]
        if since is not None:
            q += " AND ts >= ?"; params.append(since)
        if not include_shadow:
            q += " AND shadow=0"
        q += " ORDER BY ts DESC LIMIT 1000"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def calibration(self, domain: str) -> Optional[Dict[str, Any]]:
        """Stated-vs-realized calibration for a domain: mean absolute error
        between the confidence the model stated and the realized outcome
        (success=1, else 0), over non-shadow events that stated one."""
        events = [e for e in self.events(domain, include_shadow=False)
                  if e.get("stated_confidence") is not None]
        if not events:
            return None
        errs = [abs(float(e["stated_confidence"])
                    - (1.0 if e["outcome"] == "success" else 0.0))
                for e in events]
        realized = sum(1.0 for e in events
                       if e["outcome"] == "success") / len(events)
        return {"n": len(events),
                "mean_abs_error": round(sum(errs) / len(errs), 3),
                "mean_stated": round(sum(float(e["stated_confidence"])
                                         for e in events) / len(events), 3),
                "mean_realized": round(realized, 3)}

    def get(self, domain: str) -> Optional[Dict[str, Any]]:
        domain = (domain or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM competence WHERE domain=?", (domain,)).fetchone()
        return self._annotate(dict(row)) if row else None

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM competence ORDER BY last_outcome_at DESC").fetchall()
        return [self._annotate(dict(r)) for r in rows]

    @staticmethod
    def _annotate(d: Dict[str, Any]) -> Dict[str, Any]:
        n = int(d.get("success") or 0) + int(d.get("failure") or 0) + int(d.get("timeout") or 0)
        d["n"] = n
        d["success_rate"] = round(int(d.get("success") or 0) / n, 3) if n else None
        d["timeout_rate"] = round(int(d.get("timeout") or 0) / n, 3) if n else None
        return d


class SelfModel:
    """Competence store + live load probe + brief rendering.

    Load is read live from the wired subsystems (never cached): in-progress
    executor initiatives, active projects, queued jobs. Any missing subsystem
    contributes zero.
    """

    def __init__(self, store: CompetenceStore, registry: Any = None,
                 trust: Any = None, journal: Any = None) -> None:
        self.store = store
        self._registry = registry
        self.trust = trust          # TrustEngine (Amendment 1)
        self.journal = journal      # ActionJournal (Amendment 1)

    # -- recording (thin passthrough; trust engine hooks demotion) -------
    def record(self, domain: str, outcome: str,
               latency_secs: Optional[float] = None,
               shadow: bool = False, violation: bool = False,
               stated_confidence: Optional[float] = None) -> None:
        self.store.record(domain, outcome, latency_secs=latency_secs,
                          shadow=shadow, violation=violation,
                          stated_confidence=stated_confidence)
        trust = getattr(self, "trust", None)
        if trust is not None:
            try:
                trust.after_outcome(domain)
            except Exception:
                logger.debug("trust after_outcome failed", exc_info=True)

    # -- live load -------------------------------------------------------
    def load(self) -> Dict[str, int]:
        active_initiatives = 0
        active_projects = 0
        queued_jobs = 0
        reg = self._registry
        if reg is not None:
            try:
                istore = getattr(reg, "initiative_store", None)
                if istore is not None and hasattr(istore, "count"):
                    active_initiatives = int(
                        istore.count(status=["assigned", "acknowledged"]) or 0)
            except Exception:
                pass
            try:
                pengine = getattr(reg, "project_engine", None)
                pstore = getattr(pengine, "store", None)
                if pstore is not None and hasattr(pstore, "count"):
                    active_projects = int(pstore.count(status="active") or 0)
            except Exception:
                pass
            try:
                queue = getattr(reg, "task_queue", None)
                if queue is not None and hasattr(queue, "count_pending"):
                    queued_jobs = int(queue.count_pending() or 0)
            except Exception:
                pass
        return {
            "active_initiatives": active_initiatives,
            "active_projects": active_projects,
            "queued_jobs": queued_jobs,
            "total": active_initiatives + active_projects + queued_jobs,
        }

    def status(self) -> Dict[str, Any]:
        out = {"domains": self.store.snapshot(), "load": self.load()}
        if self.trust is not None:
            try:
                out["trust"] = self.trust.snapshot()
            except Exception:
                pass
        return out

    def brief(self) -> str:
        from colony_sidecar.self_model.brief import self_brief
        return self_brief(self.store.snapshot(), self.load())
