"""Expectation engine: predictions with teeth (Mind M3a).

The agent forms explicit predictions ("this commitment gets done by its due
date", "this contact replies within a day"), and a checker resolves them at
their horizon against reality. A miss is a SURPRISE, the attention signal
biological cognition runs on: surprises raise workspace salience, and the
hit/miss record yields a per-domain calibration score (Brier) that feeds the
selfhood benchmark. Without this, "she has a world model" is a claim; with
it, calibration is a number that can improve.

Resolvers are pluggable by subject prefix so the store stays generic: a
deployment (or another subsystem) registers how to check a class of
predictions. One built-in resolver covers commitment predictions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

OUTCOMES = ("pending", "hit", "miss", "unresolved")


def expectations_enabled() -> bool:
    return os.environ.get(
        "COLONY_EXPECTATIONS", "off").strip().lower() in ("shadow", "live")


def _now() -> float:
    return time.time()


@dataclass
class Prediction:
    prediction_id: str
    subject: str            # e.g. "commitment:cid-..", "contact:cid-.."
    domain: str             # calibration bucket, e.g. "commitment", "cadence"
    expectation: str        # human-readable
    confidence: float       # 0..1
    horizon: float          # epoch when it resolves
    source: str
    outcome: str = "pending"
    resolved_at: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def public(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["horizon_iso"] = datetime.fromtimestamp(
            self.horizon, tz=timezone.utc).isoformat()
        return d


class ExpectationStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                prediction_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                domain TEXT NOT NULL,
                expectation TEXT NOT NULL,
                confidence REAL NOT NULL,
                horizon REAL NOT NULL,
                source TEXT,
                outcome TEXT DEFAULT 'pending',
                resolved_at REAL,
                detail TEXT,
                dedup_key TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pred_outcome_horizon
                ON predictions(outcome, horizon);
            CREATE INDEX IF NOT EXISTS idx_pred_domain ON predictions(domain);
            CREATE INDEX IF NOT EXISTS idx_pred_dedup ON predictions(dedup_key);
            """
        )
        self._conn.commit()

    def _row(self, r: sqlite3.Row) -> Prediction:
        return Prediction(
            prediction_id=r["prediction_id"], subject=r["subject"],
            domain=r["domain"], expectation=r["expectation"],
            confidence=r["confidence"], horizon=r["horizon"],
            source=r["source"] or "", outcome=r["outcome"],
            resolved_at=r["resolved_at"],
            detail=json.loads(r["detail"] or "{}"), created_at=r["created_at"])

    def create(self, *, subject: str, domain: str, expectation: str,
               confidence: float, horizon: float, source: str,
               dedup_key: str, detail: Optional[Dict[str, Any]] = None
               ) -> Optional[Prediction]:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM predictions WHERE dedup_key=? AND "
                "outcome='pending'", (dedup_key,)).fetchone()
            if exists:
                return None
            pid = f"p-{uuid.uuid4().hex[:12]}"
            now = _now()
            self._conn.execute(
                "INSERT INTO predictions (prediction_id,subject,domain,"
                "expectation,confidence,horizon,source,detail,dedup_key,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, subject, domain, expectation,
                 max(0.0, min(1.0, confidence)), horizon, source,
                 json.dumps(detail or {}), dedup_key, now))
            self._conn.commit()
            r = self._conn.execute(
                "SELECT * FROM predictions WHERE prediction_id=?",
                (pid,)).fetchone()
        return self._row(r)

    def due(self, now: Optional[float] = None) -> List[Prediction]:
        now = now or _now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE outcome='pending' AND "
                "horizon <= ? ORDER BY horizon ASC LIMIT 200", (now,)).fetchall()
        return [self._row(r) for r in rows]

    def pending(self, limit: int = 100) -> List[Prediction]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM predictions WHERE outcome='pending' "
                "ORDER BY horizon ASC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def resolve(self, prediction_id: str, outcome: str) -> None:
        if outcome not in OUTCOMES:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE predictions SET outcome=?, resolved_at=? "
                "WHERE prediction_id=?", (outcome, _now(), prediction_id))
            self._conn.commit()

    def resolved_since(self, since: float,
                       domain: Optional[str] = None) -> List[Prediction]:
        q = ("SELECT * FROM predictions WHERE outcome IN ('hit','miss') "
             "AND resolved_at >= ?")
        params: List[Any] = [since]
        if domain:
            q += " AND domain=?"
            params.append(domain)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def domains(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT domain FROM predictions WHERE "
                "outcome IN ('hit','miss')").fetchall()
        return [r["domain"] for r in rows]


class ExpectationEngine:
    """Generate, check, and score predictions.

    Resolvers: `register_resolver(prefix, fn)` where fn(prediction) returns
    True (hit), False (miss), or None (cannot resolve yet -> left pending).
    """

    def __init__(self, store: ExpectationStore, *,
                 workspace: Any = None, journal: Any = None) -> None:
        self.store = store
        self._workspace = workspace
        self._journal = journal
        self._resolvers: Dict[str, Callable[[Prediction], Optional[bool]]] = {}
        self.register_resolver("commitment:", self._resolve_commitment)

    def register_resolver(self, prefix: str,
                          fn: Callable[[Prediction], Optional[bool]]) -> None:
        self._resolvers[prefix] = fn

    # -- generation -------------------------------------------------------
    def generate_from_commitments(self) -> int:
        """A commitment with a due date -> a prediction it gets fulfilled by
        then. Confidence from the agent's commitment track record."""
        cstore = self._commitments()
        if cstore is None:
            return 0
        try:
            pend = cstore.list(status=["pending"], limit=100).get(
                "commitments", [])
        except Exception:
            return 0
        conf = self._commitment_confidence()
        n = 0
        for c in pend:
            due = c.get("due_at")
            cid = c.get("id")
            if not due or not cid:
                continue
            try:
                horizon = datetime.fromisoformat(
                    str(due).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            desc = (c.get("description") or "commitment")[:120]
            p = self.store.create(
                subject=f"commitment:{cid}", domain="commitment",
                expectation=f"'{desc}' fulfilled by its due date",
                confidence=conf, horizon=horizon, source="cadence-model",
                dedup_key=f"commitment:{cid}",
                detail={"commitment_id": cid})
            if p is not None:
                n += 1
        return n

    def _commitment_confidence(self) -> float:
        """Historical fulfillment rate as the prior; defaults to 0.7."""
        cstore = self._commitments()
        if cstore is None:
            return 0.7
        try:
            done = cstore.list(status=["fulfilled"], limit=200).get("total", 0)
            missed = cstore.list(status=["overdue"], limit=200).get("total", 0)
            if done + missed >= 5:
                return max(0.1, min(0.95, (done + 1) / (done + missed + 2)))
        except Exception:
            pass
        return 0.7

    # -- checking ---------------------------------------------------------
    def check(self, now: Optional[float] = None) -> Dict[str, int]:
        """Resolve every due prediction. Misses emit surprise. Returns
        {"hit": n, "miss": n, "unresolved": n}."""
        counts = {"hit": 0, "miss": 0, "unresolved": 0}
        for p in self.store.due(now=now):
            verdict = self._resolve(p)
            if verdict is None:
                # give it one grace period then mark unresolved so it stops
                # recurring; unresolved is excluded from calibration
                if _now() - p.horizon > 86400:
                    self.store.resolve(p.prediction_id, "unresolved")
                    counts["unresolved"] += 1
                continue
            outcome = "hit" if verdict else "miss"
            self.store.resolve(p.prediction_id, outcome)
            counts[outcome] += 1
            self._log(f"prediction {outcome}: {p.expectation}",
                      outcome=outcome)
            if not verdict:
                self._surprise(p)
        return counts

    def _resolve(self, p: Prediction) -> Optional[bool]:
        for prefix, fn in self._resolvers.items():
            if p.subject.startswith(prefix):
                try:
                    return fn(p)
                except Exception:
                    logger.debug("resolver %s failed", prefix, exc_info=True)
                    return None
        return None

    def _resolve_commitment(self, p: Prediction) -> Optional[bool]:
        cstore = self._commitments()
        cid = p.detail.get("commitment_id")
        if cstore is None or not cid:
            return None
        c = cstore.get(cid)
        if c is None:
            return None
        status = c.get("status")
        if status == "fulfilled":
            return True
        if status in ("overdue", "cancelled"):
            return False
        # still pending past its due date -> missed
        return False

    def _surprise(self, p: Prediction) -> None:
        ws = self._workspace
        if ws is None:
            return
        try:
            ws.bump(kind="anomaly",
                    summary=f"surprise: expected {p.expectation} (conf "
                            f"{p.confidence:.2f}) but it did not hold",
                    dedup_key=f"surprise:{p.prediction_id}",
                    salience=min(0.9, 0.5 + p.confidence),
                    sources=[p.subject])
        except Exception:
            logger.debug("surprise -> workspace failed", exc_info=True)

    # -- calibration ------------------------------------------------------
    def calibration(self, domain: Optional[str] = None, *,
                    since: Optional[float] = None) -> Dict[str, Any]:
        """Brier score per domain over resolved predictions. Lower is better
        (0 = perfect, 0.25 = a coin flip at p=0.5). Feeds the benchmark."""
        since = since if since is not None else 0.0
        domains = [domain] if domain else self.store.domains()
        out: Dict[str, Any] = {}
        for d in domains:
            resolved = self.store.resolved_since(since, domain=d)
            if not resolved:
                continue
            brier = sum((p.confidence - (1.0 if p.outcome == "hit" else 0.0)) ** 2
                        for p in resolved) / len(resolved)
            hits = sum(1 for p in resolved if p.outcome == "hit")
            out[d] = {"brier": round(brier, 4), "n": len(resolved),
                      "hit_rate": round(hits / len(resolved), 4)}
        return out

    # -- wiring -----------------------------------------------------------
    def _commitments(self) -> Any:
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, "_commitment_store", None)
        except Exception:
            return None

    def _log(self, desc: str, *, outcome: str) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record("expectation", desc, decision="noted",
                                  outcome=outcome)
        except Exception:
            logger.debug("expectation journal write failed", exc_info=True)

    def snapshot(self, limit: int = 50) -> Dict[str, Any]:
        pending = self.store.pending(limit=limit)
        return {
            "mode": os.environ.get("COLONY_EXPECTATIONS", "off"),
            "pending": [p.public() for p in pending],
            "calibration": self.calibration(),
        }
