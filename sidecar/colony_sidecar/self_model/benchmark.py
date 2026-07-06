"""Selfhood benchmark: falsifiable self-improvement metrics (Mind M0a).

Derives a weekly scorecard entirely from journals and stores that already
exist. Nothing is self-reported by the LLM; every metric is computed from
recorded outcomes, and a metric whose source is unavailable is SKIPPED
rather than defaulted (the same fail-unknown discipline the doctor uses).

Metrics (stable ids):
  commitments.fulfillment   fulfilled / (fulfilled + open-overdue) in window
  initiative.acceptance     owner responded within 24h of a delivery success
  delivery.success          delivery-domain outcome rate (competence events)
  actions.success           all-domain outcome rate, per-domain detail
  journal.acted_share       acted / (acted+asked+held+blocked) decision mix
  recall.fact_coverage      probe: high-confidence shared facts re-queried
                            against graph recall, token-coverage graded
  latency.jobs_p50_secs     completed queue-job durations (p50; p95 detail)
  latency.* / surface.*     host-submitted samples (POST .../samples) rolled
                            up automatically: latency.* -> p50 (+p95),
                            everything else -> mean

Storage: colony-benchmark.db (samples append-only + weekly rollups).
Weeks are ISO (%G-W%V), windows are Monday 00:00 UTC half-open.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_METRIC_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


def benchmark_enabled() -> bool:
    return os.environ.get(
        "COLONY_BENCHMARK_ENABLED", "true").strip().lower() != "false"


def _now() -> float:
    return time.time()


def week_id(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%G-W%V")


def week_window(week: str) -> Tuple[datetime, datetime]:
    """[Monday 00:00 UTC, next Monday) for an ISO week id like 2026-W27."""
    year, wk = week.split("-W")
    start = datetime.fromisocalendar(int(year), int(wk), 1).replace(
        tzinfo=timezone.utc)
    return start, start + timedelta(days=7)


def previous_week(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return week_id(dt - timedelta(days=7))


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    k = max(0, min(len(vs) - 1, int(round((pct / 100.0) * (len(vs) - 1)))))
    return vs[k]


class BenchmarkStore:
    """SQLite persistence: append-only samples + weekly rollups."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS benchmark_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                source TEXT NOT NULL,
                ts REAL NOT NULL,
                meta TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bench_metric_ts
                ON benchmark_samples(metric, ts);
            CREATE TABLE IF NOT EXISTS benchmark_rollups (
                week TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL,
                numerator REAL,
                denominator REAL,
                detail TEXT,
                computed_at REAL NOT NULL,
                PRIMARY KEY (week, metric)
            );
            """
        )
        self._conn.commit()

    def add_sample(self, metric: str, value: float, *, source: str = "host",
                   ts: Optional[float] = None,
                   meta: Optional[Dict[str, Any]] = None) -> bool:
        metric = (metric or "").strip().lower()
        if not _METRIC_RE.match(metric):
            return False
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO benchmark_samples (metric, value, source, ts, meta)"
                " VALUES (?,?,?,?,?)",
                (metric, value, (source or "host")[:64], ts or _now(),
                 json.dumps(meta) if meta else None))
            self._conn.commit()
        return True

    def samples_in(self, since: float, until: float,
                   metric: Optional[str] = None) -> List[Dict[str, Any]]:
        q = ("SELECT metric, value, source, ts, meta FROM benchmark_samples"
             " WHERE ts >= ? AND ts < ?")
        params: List[Any] = [since, until]
        if metric:
            q += " AND metric = ?"
            params.append(metric)
        q += " ORDER BY ts ASC LIMIT 100000"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def write_rollup(self, week: str, metric: str, value: Optional[float], *,
                     numerator: Optional[float] = None,
                     denominator: Optional[float] = None,
                     detail: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO benchmark_rollups"
                " (week, metric, value, numerator, denominator, detail,"
                "  computed_at) VALUES (?,?,?,?,?,?,?)",
                (week, metric, value, numerator, denominator,
                 json.dumps(detail) if detail else None, _now()))
            self._conn.commit()

    def rollups(self, weeks: int = 8) -> Dict[str, Dict[str, Any]]:
        """{week: {metric: {value, numerator, denominator, detail}}},
        newest weeks first, at most `weeks` distinct weeks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM benchmark_rollups ORDER BY week DESC"
            ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            wk = r["week"]
            if wk not in out:
                if len(out) >= weeks:
                    continue
                out[wk] = {}
            out[wk][r["metric"]] = {
                "value": r["value"],
                "numerator": r["numerator"],
                "denominator": r["denominator"],
                "detail": json.loads(r["detail"]) if r["detail"] else None,
            }
        return out


class SelfhoodBenchmark:
    """Weekly metric derivation over the shipped stores.

    Dependencies may be injected (tests) or resolved lazily from the host
    module globals at compute time (production), so construction order in
    the server lifespan does not matter.
    """

    def __init__(self, store: BenchmarkStore, *,
                 commitments: Any = None, competence: Any = None,
                 journal: Any = None, comms: Any = None, graph: Any = None,
                 facts: Any = None, queue: Any = None,
                 owner_contact_id: Optional[str] = None,
                 probes: Optional[int] = None) -> None:
        self.store = store
        self._deps = {
            "commitments": commitments, "competence": competence,
            "journal": journal, "comms": comms, "graph": graph,
            "facts": facts, "queue": queue,
        }
        self._owner = owner_contact_id
        self._probes = probes

    # -- lazy dependency resolution -------------------------------------
    _HOST_GLOBALS = {
        "commitments": "_commitment_store", "comms": "_comms_log",
        "graph": "_graph", "facts": "_facts_store", "queue": "_task_queue",
    }

    def _dep(self, name: str) -> Any:
        if self._deps.get(name) is not None:
            return self._deps[name]
        if name == "competence":
            sm = self._host_attr("_self_model")
            if sm is not None:
                # SelfModel keeps its CompetenceStore as `.store`
                return (getattr(sm, "store", None)
                        or getattr(sm, "competence", None))
            return None
        if name == "journal":
            sm = self._host_attr("_self_model")
            return getattr(sm, "journal", None) if sm is not None else None
        g = self._HOST_GLOBALS.get(name)
        return self._host_attr(g) if g else None

    @staticmethod
    def _host_attr(name: str) -> Any:
        try:
            from colony_sidecar.api.routers import host
            return getattr(host, name, None)
        except Exception:
            return None

    @property
    def owner_contact_id(self) -> str:
        return (self._owner
                or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip())

    @property
    def probe_count(self) -> int:
        if self._probes is not None:
            return self._probes
        try:
            return int(os.environ.get("COLONY_BENCHMARK_PROBES", "8"))
        except ValueError:
            return 8

    # -- derivations ------------------------------------------------------
    async def compute_week(self, week: Optional[str] = None) -> Dict[str, Any]:
        """Derive every computable metric for `week` (default: the previous
        completed ISO week), persist rollups, and return them. Metrics whose
        source is unavailable are omitted, never zero-filled."""
        wk = week or previous_week()
        start, end = week_window(wk)
        since, until = start.timestamp(), end.timestamp()
        out: Dict[str, Any] = {}

        for name, fn in (
            ("commitments.fulfillment", self._m_commitments),
            ("delivery.success", self._m_delivery),
            ("actions.success", self._m_actions),
            ("journal.acted_share", self._m_journal),
            ("initiative.acceptance", self._m_acceptance),
        ):
            try:
                res = fn(start, end, since, until)
                if res is not None:
                    out[name] = res
            except Exception as exc:
                logger.warning("benchmark %s failed: %s", name, exc)
        for name, coro in (
            ("recall.fact_coverage", self._m_recall(since, until)),
            ("latency.jobs_p50_secs", self._m_jobs(start, end)),
        ):
            try:
                res = await coro
                if res is not None:
                    out[name] = res
            except Exception as exc:
                logger.warning("benchmark %s failed: %s", name, exc)
        out.update(self._m_submitted(since, until, skip=set(out)))

        for metric, r in out.items():
            self.store.write_rollup(
                wk, metric, r.get("value"), numerator=r.get("numerator"),
                denominator=r.get("denominator"), detail=r.get("detail"))
        logger.info("benchmark week %s: %d metrics", wk, len(out))
        return {"week": wk, "metrics": out}

    def _m_commitments(self, start, end, since, until):
        cs = self._dep("commitments")
        if cs is None:
            return None
        fulfilled = 0
        for c in (cs.list(status=["fulfilled"], limit=500)
                  .get("commitments", [])):
            fat = (c.get("fulfilled_at") or "") if isinstance(c, dict) else \
                (getattr(c, "fulfilled_at", "") or "")
            if fat and start.isoformat() <= str(fat) < end.isoformat():
                fulfilled += 1
        overdue_open = len(cs.get_overdue())
        den = fulfilled + overdue_open
        if den == 0:
            return None
        return {"value": fulfilled / den, "numerator": fulfilled,
                "denominator": den, "detail": {"overdue_open": overdue_open}}

    def _events(self, domain: str, since: float):
        comp = self._dep("competence")
        if comp is None:
            return None
        return [e for e in comp.events(domain, since=since,
                                       include_shadow=False)]

    def _m_delivery(self, start, end, since, until):
        evs = self._events("delivery", since)
        if evs is None:
            return None
        evs = [e for e in evs if e["ts"] < until]
        if not evs:
            return None
        ok = sum(1 for e in evs if e["outcome"] == "success")
        return {"value": ok / len(evs), "numerator": ok,
                "denominator": len(evs), "detail": {"n": len(evs)}}

    def _m_actions(self, start, end, since, until):
        comp = self._dep("competence")
        if comp is None:
            return None
        per: Dict[str, Dict[str, int]] = {}
        ok = n = 0
        for row in comp.snapshot():
            dom = row.get("domain") if isinstance(row, dict) else None
            if not dom or dom == "delivery":
                continue
            evs = [e for e in comp.events(dom, since=since,
                                          include_shadow=False)
                   if e["ts"] < until]
            if not evs:
                continue
            d_ok = sum(1 for e in evs if e["outcome"] == "success")
            per[dom] = {"success": d_ok, "n": len(evs)}
            ok += d_ok
            n += len(evs)
        if n == 0:
            return None
        return {"value": ok / n, "numerator": ok, "denominator": n,
                "detail": {"domains": per}}

    def _m_journal(self, start, end, since, until):
        j = self._dep("journal")
        if j is None:
            return None
        entries = j.recent(limit=2000, since=since)
        counts: Dict[str, int] = {}
        for e in entries:
            if e.get("ts", 0) >= until:
                continue
            d = e.get("decision") or "unknown"
            counts[d] = counts.get(d, 0) + 1
        gated = sum(counts.get(k, 0)
                    for k in ("acted", "asked", "held", "blocked"))
        if gated == 0:
            return None
        return {"value": counts.get("acted", 0) / gated,
                "numerator": counts.get("acted", 0), "denominator": gated,
                "detail": {"decisions": counts}}

    def _m_acceptance(self, start, end, since, until):
        """Owner responded (inbound comm) within 24h of a delivery success."""
        owner = self.owner_contact_id
        comms = self._dep("comms")
        evs = self._events("delivery", since)
        if not owner or comms is None or evs is None:
            return None
        deliveries = [e for e in evs
                      if e["outcome"] == "success" and e["ts"] < until]
        if not deliveries:
            return None
        inbound = comms.inbound_since(owner, start.isoformat())
        in_ts = []
        for t in inbound:
            try:
                in_ts.append(datetime.fromisoformat(
                    str(t).replace("Z", "+00:00")).timestamp())
            except ValueError:
                continue
        accepted = sum(
            1 for e in deliveries
            if any(e["ts"] < t <= e["ts"] + 86400 for t in in_ts))
        return {"value": accepted / len(deliveries), "numerator": accepted,
                "denominator": len(deliveries),
                "detail": {"deliveries": len(deliveries)}}

    async def _m_recall(self, since, until):
        """Probe: re-query high-confidence shared facts against graph recall
        and grade by token coverage. Records each probe as a sample."""
        graph = self._dep("graph")
        facts = self._dep("facts")
        if graph is None or facts is None:
            return None
        rows = facts.list_facts(min_confidence=0.75, limit=200).get(
            "facts", [])
        if not rows:
            return None
        picks = random.sample(rows, min(self.probe_count, len(rows)))
        hits = 0
        for f in picks:
            fact = (f.get("fact") if isinstance(f, dict)
                    else getattr(f, "fact", "")) or ""
            if not fact.strip():
                continue
            try:
                results = await graph.recall(fact, limit=5,
                                             min_confidence=0.1)
            except Exception:
                continue
            hit = 1.0 if self._covered(fact, results) else 0.0
            hits += int(hit)
            self.store.add_sample(
                "recall.probe", hit, source="benchmark",
                meta={"fact_id": (f.get("id") if isinstance(f, dict)
                                  else getattr(f, "id", None))})
        n = len(picks)
        if n == 0:
            return None
        return {"value": hits / n, "numerator": hits, "denominator": n,
                "detail": {"probes": n}}

    @staticmethod
    def _covered(fact: str, results: List[Dict[str, Any]],
                 threshold: float = 0.5) -> bool:
        words = {w for w in re.findall(r"[a-z0-9]+", fact.lower())
                 if len(w) > 3}
        if not words:
            return False
        for r in results or []:
            content = str((r or {}).get("content", "")).lower()
            if not content:
                continue
            got = sum(1 for w in words if w in content)
            if got / len(words) >= threshold:
                return True
        return False

    async def _m_jobs(self, start, end):
        queue = self._dep("queue")
        if queue is None:
            return None
        # host wires the TaskQueueManager wrapper; the raw QueueManager
        # (which owns completed_durations) sits at .queue
        if not hasattr(queue, "completed_durations"):
            queue = getattr(queue, "queue", None)
            if queue is None or not hasattr(queue, "completed_durations"):
                return None
        durs = [d for d in await queue.completed_durations(
            start.isoformat(), end.isoformat()) if d >= 0]
        if not durs:
            return None
        return {"value": _percentile(durs, 50),
                "numerator": None, "denominator": None,
                "detail": {"p50": _percentile(durs, 50),
                           "p95": _percentile(durs, 95), "n": len(durs)}}

    def _m_submitted(self, since: float, until: float,
                     skip: Optional[set] = None) -> Dict[str, Any]:
        """Roll up host-submitted samples generically."""
        skip = skip or set()
        by_metric: Dict[str, List[float]] = {}
        for s in self.store.samples_in(since, until):
            if s["metric"] == "recall.probe" or s["metric"] in skip:
                continue
            by_metric.setdefault(s["metric"], []).append(s["value"])
        out: Dict[str, Any] = {}
        for metric, vals in by_metric.items():
            if metric.startswith("latency."):
                out[metric] = {
                    "value": _percentile(vals, 50),
                    "numerator": None, "denominator": None,
                    "detail": {"p50": _percentile(vals, 50),
                               "p95": _percentile(vals, 95),
                               "n": len(vals)}}
            else:
                out[metric] = {
                    "value": sum(vals) / len(vals),
                    "numerator": None, "denominator": None,
                    "detail": {"n": len(vals),
                               "min": min(vals), "max": max(vals)}}
        return out

    # -- read side --------------------------------------------------------
    def snapshot(self, weeks: int = 8) -> Dict[str, Any]:
        """Rollups for the last N weeks plus latest-vs-previous deltas."""
        rolls = self.store.rollups(weeks=weeks)
        ordered = sorted(rolls.keys(), reverse=True)
        trends: Dict[str, Any] = {}
        if len(ordered) >= 2:
            cur, prev = rolls[ordered[0]], rolls[ordered[1]]
            for metric, r in cur.items():
                pv = (prev.get(metric) or {}).get("value")
                if r.get("value") is not None and pv is not None:
                    trends[metric] = round(r["value"] - pv, 4)
        return {"weeks": ordered, "rollups": rolls, "trends": trends,
                "latest": ordered[0] if ordered else None}
