"""Experiment framework: self-modification as controlled experiments (M0b).

Formalizes what AdaptiveParamStore started. An experiment applies ONE
bounded change (this version: an adaptive-parameter value; the schema
carries `kind` for future prompt/strategy variants but refuses to run
them), captures the baseline of a benchmark metric, waits a window, and
then adopts or auto-reverts by comparing the metric against a guard.

Honesty rules:
- No baseline rollup for the chosen metric -> the experiment cannot start.
- The decision needs a NEW completed rollup week; until one exists the
  experiment stays running rather than deciding on the baseline itself.
- A parameter changed out from under a running experiment (manually or by
  another writer) aborts it as superseded; the framework never silently
  re-applies its variant.
- One running experiment per parameter; a small global concurrency cap.

Every transition is journaled (domain meta_learning) via the param store's
own journaling plus explicit experiment records.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

STATUSES = ("proposed", "running", "adopted", "reverted", "aborted")


def experiments_enabled() -> bool:
    return os.environ.get(
        "COLONY_EXPERIMENTS_ENABLED", "true").strip().lower() != "false"


def _max_running() -> int:
    try:
        return int(os.environ.get("COLONY_EXPERIMENTS_MAX_RUNNING", "2"))
    except ValueError:
        return 2


class ExperimentStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY,
                hypothesis TEXT NOT NULL,
                kind TEXT NOT NULL,
                ref TEXT NOT NULL,
                variant REAL NOT NULL,
                baseline_param REAL,
                metric TEXT NOT NULL,
                baseline_metric REAL,
                baseline_week TEXT,
                max_regression REAL NOT NULL,
                window_days INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at REAL NOT NULL,
                started_at REAL,
                ends_at REAL,
                decided_at REAL,
                decision_reason TEXT,
                source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_exp_status
                ON experiments(status);
            """
        )
        self._conn.commit()

    def add(self, row: Dict[str, Any]) -> None:
        cols = ",".join(row.keys())
        ph = ",".join("?" for _ in row)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO experiments ({cols}) VALUES ({ph})",
                list(row.values()))
            self._conn.commit()

    def update(self, exp_id: str, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE experiments SET {sets} WHERE id=?",
                list(fields.values()) + [exp_id])
            self._conn.commit()

    def get(self, exp_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
        return dict(r) if r else None

    def list(self, status: Optional[str] = None,
             limit: int = 50) -> List[Dict[str, Any]]:
        q = "SELECT * FROM experiments"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"
            params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def running_for_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM experiments WHERE ref=? AND status IN"
                " ('proposed','running')", (ref,)).fetchone()
        return dict(r) if r else None

    def running_count(self) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM experiments WHERE"
                " status='running'").fetchone()
        return int(r["n"])


class ExperimentEngine:
    """Propose -> start (apply + baseline) -> evaluate (adopt/revert)."""

    def __init__(self, store: ExperimentStore, *, params: Any = None,
                 benchmark: Any = None, journal: Any = None) -> None:
        self.store = store
        self._params = params
        self._benchmark = benchmark
        self._journal = journal

    # lazy production wiring, injectable for tests
    def _p(self) -> Any:
        if self._params is not None:
            return self._params
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, "_adaptive_params", None)
        except Exception:
            return None

    def _b(self) -> Any:
        if self._benchmark is not None:
            return self._benchmark
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, "_benchmark", None)
        except Exception:
            return None

    def _j(self) -> Any:
        if self._journal is not None:
            return self._journal
        try:
            from colony_sidecar.api.routers import host
            sm = getattr(host, "_self_model", None)
            return getattr(sm, "journal", None) if sm is not None else None
        except Exception:
            return None

    def _log(self, description: str, *, reasoning: str = "",
             outcome: str = "", ref: str = "") -> None:
        journal = self._j()
        if journal is None:
            return
        try:
            journal.record(
                "meta_learning", description, reasoning=reasoning,
                decision="acted", outcome=outcome, ref=ref)
        except Exception:
            logger.debug("experiment journal write failed", exc_info=True)

    # -- lifecycle ---------------------------------------------------------
    def propose_and_start(self, *, hypothesis: str, ref: str, variant: float,
                          metric: str, max_regression: float = 0.05,
                          window_days: int = 7,
                          kind: str = "param",
                          source: str = "api") -> Dict[str, Any]:
        """Validate, persist, apply the variant, capture baselines.
        Raises ValueError with a human-readable reason on refusal."""
        if not experiments_enabled():
            raise ValueError("experiments disabled "
                             "(COLONY_EXPERIMENTS_ENABLED=false)")
        if kind != "param":
            raise ValueError(
                f"kind {kind!r} not runnable in this version (param only)")
        params = self._p()
        bench = self._b()
        if params is None:
            raise ValueError("adaptive params store not wired")
        if bench is None:
            raise ValueError("benchmark not wired (metrics are the judge; "
                             "no benchmark, no experiment)")
        known = {p["name"]: p for p in params.snapshot()}
        if ref not in known:
            raise ValueError(f"unknown adaptive param {ref!r}")
        if self.store.running_for_ref(ref) is not None:
            raise ValueError(f"an experiment on {ref!r} is already open")
        if self.store.running_count() >= _max_running():
            raise ValueError("running-experiment cap reached "
                             f"({_max_running()})")
        rolls = bench.store.rollups(weeks=4)
        baseline_week = None
        baseline_metric = None
        for wk in sorted(rolls.keys(), reverse=True):
            r = rolls[wk].get(metric)
            if r and r.get("value") is not None:
                baseline_week, baseline_metric = wk, float(r["value"])
                break
        if baseline_metric is None:
            raise ValueError(
                f"no rollup exists for metric {metric!r}; a baseline is "
                "required before experimenting")
        window_days = max(1, min(28, int(window_days)))
        baseline_param = float(known[ref]["effective"])
        exp_id = f"exp-{uuid.uuid4().hex[:12]}"
        now = time.time()
        applied = params.set(
            ref, float(variant),
            reason=f"experiment {exp_id}: {hypothesis[:120]}",
            source=f"experiment:{exp_id}")
        if applied is None:
            raise ValueError(f"param store refused {ref!r}")
        self.store.add({
            "id": exp_id, "hypothesis": hypothesis[:500], "kind": kind,
            "ref": ref, "variant": applied,
            "baseline_param": baseline_param, "metric": metric,
            "baseline_metric": baseline_metric,
            "baseline_week": baseline_week,
            "max_regression": float(max_regression),
            "window_days": window_days, "status": "running",
            "created_at": now, "started_at": now,
            "ends_at": now + window_days * 86400, "source": source[:64],
        })
        self._log(
            f"experiment {exp_id} started: {ref} "
            f"{baseline_param} -> {applied}",
            reasoning=hypothesis[:300],
            outcome=f"baseline {metric}={baseline_metric:.3f} "
                    f"({baseline_week})",
            ref=exp_id)
        return self.store.get(exp_id) or {}

    def abort(self, exp_id: str, reason: str = "manual abort") -> bool:
        exp = self.store.get(exp_id)
        if not exp or exp["status"] != "running":
            return False
        self._revert(exp, status="aborted", reason=reason)
        return True

    def evaluate(self) -> List[Dict[str, Any]]:
        """Decide every running experiment that can be decided. Returns the
        experiments whose status changed."""
        decided: List[Dict[str, Any]] = []
        params = self._p()
        bench = self._b()
        for exp in self.store.list(status="running", limit=20):
            # superseded: someone else moved the knob
            if params is not None:
                current = None
                for p in params.snapshot():
                    if p["name"] == exp["ref"]:
                        current = float(p["effective"])
                        break
                if current is not None and \
                        abs(current - float(exp["variant"])) > 1e-9:
                    self.store.update(
                        exp["id"], status="aborted", decided_at=time.time(),
                        decision_reason="superseded: parameter changed "
                                        "outside the experiment")
                    self._log(f"experiment {exp['id']} aborted (superseded)",
                              ref=exp["id"])
                    decided.append(self.store.get(exp["id"]) or {})
                    continue
            if time.time() < float(exp["ends_at"] or 0):
                continue
            if bench is None:
                continue
            week, value = self._latest_metric(bench, exp["metric"])
            if value is None or week == exp["baseline_week"]:
                continue  # no NEW completed rollup yet; keep waiting
            higher_is_better = not str(exp["metric"]).startswith("latency.")
            delta = (value - float(exp["baseline_metric"]))
            regression = -delta if higher_is_better else delta
            if regression > float(exp["max_regression"]):
                self._revert(
                    exp, status="reverted",
                    reason=f"{exp['metric']} {exp['baseline_metric']:.3f} "
                           f"-> {value:.3f} ({week}); regression "
                           f"{regression:.3f} > guard "
                           f"{exp['max_regression']}")
            else:
                self.store.update(
                    exp["id"], status="adopted", decided_at=time.time(),
                    decision_reason=f"{exp['metric']} "
                                    f"{exp['baseline_metric']:.3f} -> "
                                    f"{value:.3f} ({week}); within guard")
                self._log(
                    f"experiment {exp['id']} adopted: {exp['ref']} stays "
                    f"at {exp['variant']}",
                    outcome=f"{exp['metric']} {value:.3f}", ref=exp["id"])
            decided.append(self.store.get(exp["id"]) or {})
        return decided

    @staticmethod
    def _latest_metric(bench: Any, metric: str):
        rolls = bench.store.rollups(weeks=4)
        for wk in sorted(rolls.keys(), reverse=True):
            r = rolls[wk].get(metric)
            if r and r.get("value") is not None:
                return wk, float(r["value"])
        return None, None

    def _revert(self, exp: Dict[str, Any], *, status: str,
                reason: str) -> None:
        params = self._p()
        if params is not None and exp.get("baseline_param") is not None:
            params.set(
                exp["ref"], float(exp["baseline_param"]),
                reason=f"experiment {exp['id']} {status}: {reason[:150]}",
                source=f"experiment:{exp['id']}")
        self.store.update(exp["id"], status=status, decided_at=time.time(),
                          decision_reason=reason[:400])
        self._log(f"experiment {exp['id']} {status}: {exp['ref']} restored "
                  f"to {exp.get('baseline_param')}",
                  reasoning=reason[:300], ref=exp["id"])

    def snapshot(self, limit: int = 30) -> Dict[str, Any]:
        exps = self.store.list(limit=limit)
        return {
            "running": [e for e in exps if e["status"] == "running"],
            "recent": [e for e in exps if e["status"] != "running"],
            "enabled": experiments_enabled(),
            "max_running": _max_running(),
        }
