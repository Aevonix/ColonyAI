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

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_METRIC_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
_DEFINITION_VERSION_RE = re.compile(r"^v[1-9][0-9]{0,5}$")
_P4_MODES = frozenset({"off", "shadow", "live"})


def cognition_p4_mode() -> str:
    """Controlled-learning mode. New deployments are deliberately dark."""

    value = os.environ.get("COLONY_COGNITION_P4_MODE", "off").strip().lower()
    return value if value in _P4_MODES else "off"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


@dataclass(frozen=True)
class MetricDefinition:
    """Immutable measurement contract for one benchmark metric version."""

    metric: str
    version: str
    direction: str
    unit: str
    evidence_query: str
    minimum_samples: int
    description: str = ""

    def normalized(self) -> Dict[str, Any]:
        metric = (self.metric or "").strip().lower()
        version = (self.version or "").strip().lower()
        direction = (self.direction or "").strip().lower()
        unit = (self.unit or "").strip().lower()
        evidence_query = (self.evidence_query or "").strip()
        description = (self.description or "").strip()
        if not _METRIC_RE.fullmatch(metric):
            raise ValueError("metric definition has an invalid metric id")
        if not _DEFINITION_VERSION_RE.fullmatch(version):
            raise ValueError("metric definition version must look like v1")
        if direction not in {"higher", "lower"}:
            raise ValueError("metric direction must be higher or lower")
        if not unit or len(unit) > 64:
            raise ValueError("metric unit is required")
        if not evidence_query or len(evidence_query) > 2000:
            raise ValueError("metric evidence_query is required")
        minimum = int(self.minimum_samples)
        if minimum < 1 or minimum > 1_000_000:
            raise ValueError("metric minimum_samples is out of bounds")
        return {
            "metric": metric,
            "version": version,
            "direction": direction,
            "unit": unit,
            "evidence_query": evidence_query,
            "minimum_samples": minimum,
            "description": description[:1000],
        }


BUILTIN_METRIC_DEFINITIONS = (
    MetricDefinition(
        "commitments.fulfillment", "v2", "higher", "ratio",
        "commitment.due_at in cohort_week AND terminal evidence is recorded",
        5, "On-time fulfillment for the commitments due in one coherent cohort."),
    MetricDefinition(
        "delivery.success", "v1", "higher", "ratio",
        "competence.domain=delivery AND evidence_status=verified", 5,
        "Receipt-backed transport success."),
    MetricDefinition(
        "actions.success", "v2", "higher", "ratio",
        "competence evidence is available and outcome is non-neutral", 10,
        "Verified, versioned action outcomes."),
    MetricDefinition(
        "journal.acted_share", "v1", "higher", "ratio",
        "action_journal.decision in (acted,asked,held,blocked)", 5,
        "Decision mix; diagnostic rather than a success claim."),
    MetricDefinition(
        "initiative.acceptance", "v2", "higher", "ratio",
        "owner reaction explicitly names the delivered initiative or message", 5,
        "Message-bound owner acceptance; unrelated inbound turns never count."),
    MetricDefinition(
        "responses.correction_rate", "v1", "lower", "ratio",
        "owner correction context_hash names a receipt-backed outbound response", 10,
        "Owner-corrected responses divided by the same outbound cohort."),
    MetricDefinition(
        "recall.fact_coverage", "v2", "higher", "ratio",
        "fact is viewer-allowed and recall uses the fact subject scope", 8,
        "Viewer-scoped recall probe coverage."),
    MetricDefinition(
        "latency.jobs_p50_secs", "v1", "lower", "seconds",
        "queue completion has a start and terminal timestamp", 5,
        "Median queue-job completion latency."),
)


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
            CREATE TABLE IF NOT EXISTS benchmark_metric_definitions (
                metric TEXT NOT NULL,
                version TEXT NOT NULL,
                direction TEXT NOT NULL,
                unit TEXT NOT NULL,
                evidence_query TEXT NOT NULL,
                minimum_samples INTEGER NOT NULL,
                description TEXT,
                definition_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (metric, version)
            );
            CREATE TRIGGER IF NOT EXISTS benchmark_definition_no_update
                BEFORE UPDATE ON benchmark_metric_definitions
                BEGIN
                    SELECT RAISE(ABORT, 'benchmark definition is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS benchmark_definition_no_delete
                BEFORE DELETE ON benchmark_metric_definitions
                BEGIN
                    SELECT RAISE(ABORT, 'benchmark definition is immutable');
                END;
            """
        )
        self._additive_columns(
            "benchmark_samples",
            {
                "sample_id": "TEXT",
                "definition_version": "TEXT",
                "sample_principal": "TEXT",
                "source_ref": "TEXT",
                "receipt_ref": "TEXT",
                "evidence_status": "TEXT",
                "exposure_id": "TEXT",
            },
        )
        self._additive_columns(
            "benchmark_rollups",
            {
                "definition_version": "TEXT",
                "definition_hash": "TEXT",
                "evidence_count": "INTEGER",
            },
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bench_sample_id "
            "ON benchmark_samples(sample_id) WHERE sample_id IS NOT NULL"
        )
        self._conn.commit()
        for definition in BUILTIN_METRIC_DEFINITIONS:
            self.register_definition(definition)

    def _additive_columns(self, table: str,
                          columns: Dict[str, str]) -> None:
        existing = {str(row[1]) for row in self._conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        for name, sql_type in columns.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def register_definition(self, definition: MetricDefinition
                            ) -> Dict[str, Any]:
        """Idempotently register an immutable metric definition."""

        normalized = definition.normalized()
        digest = hashlib.sha256(
            _canonical(normalized).encode("utf-8")).hexdigest()
        with self._lock:
            current = self._conn.execute(
                "SELECT * FROM benchmark_metric_definitions "
                "WHERE metric=? AND version=?",
                (normalized["metric"], normalized["version"]),
            ).fetchone()
            if current is not None:
                result = dict(current)
                if result["definition_hash"] != digest:
                    raise ValueError(
                        "metric definition is immutable; publish a new version")
                return result
            self._conn.execute(
                "INSERT INTO benchmark_metric_definitions "
                "(metric,version,direction,unit,evidence_query,minimum_samples,"
                "description,definition_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    normalized["metric"], normalized["version"],
                    normalized["direction"], normalized["unit"],
                    normalized["evidence_query"], normalized["minimum_samples"],
                    normalized["description"], digest, _now(),
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM benchmark_metric_definitions "
                "WHERE metric=? AND version=?",
                (normalized["metric"], normalized["version"]),
            ).fetchone()
        assert row is not None
        return dict(row)

    def definition(self, metric: str, version: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM benchmark_metric_definitions "
                "WHERE metric=? AND version=?",
                ((metric or "").strip().lower(),
                 (version or "").strip().lower()),
            ).fetchone()
        return dict(row) if row is not None else None

    def definitions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM benchmark_metric_definitions "
                "ORDER BY metric, version").fetchall()
        return [dict(row) for row in rows]

    def add_sample(self, metric: str, value: float, *, source: str = "host",
                   ts: Optional[float] = None,
                   meta: Optional[Dict[str, Any]] = None) -> bool:
        """Compatibility ingestion.

        Legacy samples remain queryable but are explicitly unattested and are
        never eligible for a P4 causal decision.
        """
        metric = (metric or "").strip().lower()
        if not _METRIC_RE.match(metric):
            return False
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value):
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO benchmark_samples "
                "(metric,value,source,ts,meta,sample_id,definition_version,"
                "sample_principal,source_ref,receipt_ref,evidence_status,exposure_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    metric, value, (source or "host")[:64],
                    ts if ts is not None else _now(),
                    json.dumps(meta) if meta else None,
                    f"legacy-{uuid.uuid4().hex}", "legacy.unversioned",
                    f"legacy:{(source or 'host')[:96]}", None, None,
                    "legacy_unverified", None,
                ))
            self._conn.commit()
        return True

    def add_evidence_sample(
        self,
        metric: str,
        value: float,
        *,
        definition_version: str,
        sample_principal: str,
        source_ref: str,
        receipt_ref: Optional[str] = None,
        sample_id: Optional[str] = None,
        exposure_id: Optional[str] = None,
        ts: Optional[float] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Append one attested sample under a registered evidence contract.

        A stable ``sample_id`` makes transport retries idempotent. Reusing it
        with changed content is refused rather than silently replacing proof.
        """

        normalized_metric = (metric or "").strip().lower()
        version = (definition_version or "").strip().lower()
        definition = self.definition(normalized_metric, version)
        if definition is None:
            raise ValueError("registered metric definition is required")
        principal = (sample_principal or "").strip()
        source = (source_ref or "").strip()
        receipt = (receipt_ref or "").strip() or None
        exposure = (exposure_id or "").strip() or None
        if not principal or len(principal) > 192:
            raise ValueError("sample_principal is required")
        if not source or len(source) > 512:
            raise ValueError("source_ref is required")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("sample value must be numeric") from exc
        if not math.isfinite(number):
            raise ValueError("sample value must be finite")
        sid = (sample_id or f"bms-{uuid.uuid4().hex}").strip()
        if not sid or len(sid) > 192:
            raise ValueError("sample_id is malformed")
        stamp = float(ts) if ts is not None else _now()
        payload = {
            "metric": normalized_metric,
            "value": number,
            "source": "evidence",
            "ts": stamp,
            "meta": meta or None,
            "sample_id": sid,
            "definition_version": version,
            "sample_principal": principal,
            "source_ref": source,
            "receipt_ref": receipt,
            "evidence_status": "verified" if receipt else "observed",
            "exposure_id": exposure,
        }
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM benchmark_samples WHERE sample_id=?", (sid,)
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                comparable = {
                    key: row.get(key) for key in (
                        "metric", "value", "source", "sample_id",
                        "definition_version", "sample_principal", "source_ref",
                        "receipt_ref", "evidence_status", "exposure_id")
                }
                if comparable != {key: payload.get(key) for key in comparable}:
                    raise ValueError("sample_id replay changed immutable evidence")
                try:
                    stored_meta = json.loads(row["meta"]) if row.get("meta") else None
                except ValueError:
                    stored_meta = row.get("meta")
                if _canonical(stored_meta) != _canonical(meta or None):
                    raise ValueError("sample_id replay changed immutable evidence")
                if ts is not None and abs(float(row["ts"]) - stamp) > 1e-9:
                    raise ValueError("sample_id replay changed immutable evidence")
                return True
            self._conn.execute(
                "INSERT INTO benchmark_samples "
                "(metric,value,source,ts,meta,sample_id,definition_version,"
                "sample_principal,source_ref,receipt_ref,evidence_status,exposure_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["metric"], payload["value"], payload["source"],
                    payload["ts"], json.dumps(meta) if meta else None,
                    payload["sample_id"], payload["definition_version"],
                    payload["sample_principal"], payload["source_ref"],
                    payload["receipt_ref"], payload["evidence_status"],
                    payload["exposure_id"],
                ),
            )
            self._conn.commit()
        return True

    def samples_in(self, since: float, until: float,
                   metric: Optional[str] = None) -> List[Dict[str, Any]]:
        q = ("SELECT * FROM benchmark_samples"
             " WHERE ts >= ? AND ts < ?")
        params: List[Any] = [since, until]
        if metric:
            q += " AND metric = ?"
            params.append(metric)
        q += " ORDER BY ts ASC LIMIT 100000"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def evidence_samples_in(
        self,
        since: float,
        until: float,
        metric: Optional[str] = None,
        *,
        definition_version: Optional[str] = None,
        exposure_id: Optional[str] = None,
        require_receipt: bool = False,
    ) -> List[Dict[str, Any]]:
        q = ("SELECT * FROM benchmark_samples WHERE ts>=? AND ts<? "
             "AND evidence_status IN ('observed','verified')")
        params: List[Any] = [float(since), float(until)]
        if metric:
            q += " AND metric=?"
            params.append((metric or "").strip().lower())
        if definition_version:
            q += " AND definition_version=?"
            params.append((definition_version or "").strip().lower())
        if exposure_id:
            q += " AND exposure_id=?"
            params.append(exposure_id)
        if require_receipt:
            q += " AND receipt_ref IS NOT NULL AND receipt_ref!=''"
        q += " ORDER BY ts,id LIMIT 100000"
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(row) for row in rows]

    def write_rollup(self, week: str, metric: str, value: Optional[float], *,
                     numerator: Optional[float] = None,
                     denominator: Optional[float] = None,
                     detail: Optional[Dict[str, Any]] = None,
                     definition_version: Optional[str] = None,
                     evidence_count: Optional[int] = None) -> None:
        definition_hash = None
        if definition_version:
            definition = self.definition(metric, definition_version)
            if definition is None:
                raise ValueError("registered metric definition is required")
            definition_hash = definition["definition_hash"]
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO benchmark_rollups"
                " (week, metric, value, numerator, denominator, detail,"
                "  computed_at,definition_version,definition_hash,evidence_count)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (week, metric, value, numerator, denominator,
                 json.dumps(detail) if detail else None, _now(),
                 definition_version, definition_hash, evidence_count))
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
                "definition_version": r["definition_version"],
                "definition_hash": r["definition_hash"],
                "evidence_count": r["evidence_count"],
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
                 corrections: Any = None,
                 owner_contact_id: Optional[str] = None,
                 probes: Optional[int] = None) -> None:
        self.store = store
        self._deps = {
            "commitments": commitments, "competence": competence,
            "journal": journal, "comms": comms, "graph": graph,
            "facts": facts, "queue": queue, "corrections": corrections,
        }
        self._owner = owner_contact_id
        self._probes = probes

    # -- lazy dependency resolution -------------------------------------
    _HOST_GLOBALS = {
        "commitments": "_commitment_store", "comms": "_comms_log",
        "graph": "_graph", "facts": "_facts_store", "queue": "_task_queue",
        "corrections": "_learning_feedback_store",
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
            ("responses.correction_rate", self._m_corrections),
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
        try:
            out.update(self._m_calibration(since))
        except Exception as exc:
            logger.warning("benchmark calibration failed: %s", exc)
        out.update(self._m_submitted(since, until, skip=set(out)))

        for metric, r in out.items():
            definition_version = self._definition_version(metric)
            self.store.write_rollup(
                wk, metric, r.get("value"), numerator=r.get("numerator"),
                denominator=r.get("denominator"), detail=r.get("detail"),
                definition_version=definition_version,
                evidence_count=(int(r.get("denominator"))
                                if r.get("denominator") is not None else None))
        logger.info("benchmark week %s: %d metrics", wk, len(out))
        return {"week": wk, "metrics": out}

    @staticmethod
    def _definition_version(metric: str) -> Optional[str]:
        if cognition_p4_mode() != "live":
            # Existing rollups are intentionally left labelled as legacy
            # until the controlled path is explicitly enabled.
            return None
        versions = {
            "commitments.fulfillment": "v2",
            "delivery.success": "v1",
            "actions.success": "v2",
            "journal.acted_share": "v1",
            "initiative.acceptance": "v2",
            "responses.correction_rate": "v1",
            "recall.fact_coverage": "v2",
            "latency.jobs_p50_secs": "v1",
        }
        return versions.get(metric)

    def _m_commitments(self, start, end, since, until):
        cs = self._dep("commitments")
        if cs is None:
            return None
        if cognition_p4_mode() == "live":
            try:
                rows = cs.list(limit=10000).get("commitments", [])
            except (AttributeError, TypeError):
                return None
            due_cohort: List[Dict[str, Any]] = []
            for raw in rows:
                row = raw if isinstance(raw, dict) else vars(raw)
                due = self._parse_instant(row.get("due_at"))
                if due is None or not (start <= due < end):
                    continue
                if str(row.get("status") or "").lower() == "cancelled":
                    continue
                due_cohort.append(row)
            if not due_cohort:
                return None
            fulfilled = 0
            late = 0
            for row in due_cohort:
                due = self._parse_instant(row.get("due_at"))
                completed = self._parse_instant(row.get("fulfilled_at"))
                if completed is not None and due is not None:
                    if completed <= due:
                        fulfilled += 1
                    else:
                        late += 1
            return {
                "value": fulfilled / len(due_cohort),
                "numerator": fulfilled,
                "denominator": len(due_cohort),
                "detail": {
                    "metric_definition": "commitments.fulfillment/v2",
                    "cohort": "due_at_in_iso_week",
                    "late": late,
                    "open_or_missed": len(due_cohort) - fulfilled - late,
                },
            }
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

    @staticmethod
    def _parse_instant(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _events(self, domain: str, since: float):
        comp = self._dep("competence")
        if comp is None:
            return None
        return [e for e in comp.events(domain, since=since,
                                       include_shadow=False)]

    def _competence_state(self, domains: List[str], since: float,
                          until: float) -> Tuple[int, List[Dict[str, Any]]]:
        """Correction revision and unresolved provenance gaps for a slice."""
        comp = self._dep("competence")
        if comp is None:
            return 0, []
        revision = 0
        gaps: List[Dict[str, Any]] = []
        try:
            revision = int(comp.reconciliation_revision(
                domains=domains, since=since, until=until))
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            for domain in domains:
                gaps.extend(comp.active_evidence_gaps(
                    domain, since=since, until=until))
        except (AttributeError, TypeError):
            pass
        return revision, gaps

    @staticmethod
    def _unavailable_competence(
            revision: int, gaps: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "value": None, "numerator": None, "denominator": None,
            "detail": {
                "available": False,
                "reason": "competence_evidence_gap",
                "competence_reconciliation_revision": revision,
                "gaps": [{
                    "id": g.get("reconciliation_id"),
                    "domain": g.get("domain"),
                    "since_ts": g.get("since_ts"),
                    "until_ts": g.get("until_ts"),
                    "reason": g.get("reason"),
                } for g in gaps],
            },
        }

    def _m_delivery(self, start, end, since, until):
        revision, gaps = self._competence_state(
            ["delivery"], since, until)
        if gaps:
            return self._unavailable_competence(revision, gaps)
        evs = self._events("delivery", since)
        if evs is None:
            return None
        evs = [e for e in evs if e["ts"] < until]
        if cognition_p4_mode() == "live":
            evs = [e for e in evs if e.get("evidence_status") == "verified"]
        if not evs:
            return None
        ok = sum(1 for e in evs if e["outcome"] == "success")
        return {"value": ok / len(evs), "numerator": ok,
                "denominator": len(evs),
                "detail": {
                    "n": len(evs),
                    "metric_definition": "colony.delivery-success/v1",
                    "competence_reconciliation_revision": revision,
                }}

    def _m_actions(self, start, end, since, until):
        comp = self._dep("competence")
        if comp is None:
            return None
        domains = []
        for row in comp.snapshot():
            dom = row.get("domain") if isinstance(row, dict) else None
            if dom and dom != "delivery":
                domains.append(dom)
        revision, gaps = self._competence_state(domains, since, until)
        if gaps:
            return self._unavailable_competence(revision, gaps)
        per: Dict[str, Dict[str, int]] = {}
        ok = n = 0
        for dom in domains:
            evs = [e for e in comp.events(dom, since=since,
                                          include_shadow=False)
                   if e["ts"] < until]
            if cognition_p4_mode() == "live":
                evs = [e for e in evs
                       if e.get("evidence_status") == "verified"
                       and str(e.get("outcome_contract") or "").lower()
                       not in {"", "legacy.unversioned"}]
            if not evs:
                continue
            d_ok = sum(1 for e in evs if e["outcome"] == "success")
            per[dom] = {"success": d_ok, "n": len(evs)}
            ok += d_ok
            n += len(evs)
        if n == 0:
            return None
        return {"value": ok / n, "numerator": ok, "denominator": n,
                "detail": {
                    "domains": per,
                    "metric_definition": "colony.actions-success/v2",
                    "competence_reconciliation_revision": revision,
                }}

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
        if not owner or comms is None:
            return None
        revision, gaps = self._competence_state(
            ["delivery"], since, until)
        if gaps:
            return self._unavailable_competence(revision, gaps)
        evs = self._events("delivery", since)
        if evs is None:
            return None
        deliveries = [e for e in evs
                      if e["outcome"] == "success" and e["ts"] < until]
        if not deliveries:
            return None
        if cognition_p4_mode() == "live":
            # An unrelated inbound message is not evidence that an initiative
            # was useful. Only an explicit reaction naming the delivery or its
            # source message enters this cohort.
            refs: List[str] = []
            for event in deliveries:
                if event.get("evidence_status") != "verified":
                    continue
                evidence = event.get("evidence") or {}
                if isinstance(evidence, str):
                    try:
                        evidence = json.loads(evidence)
                    except ValueError:
                        evidence = {}
                ref = (evidence.get("delivery_id")
                       if isinstance(evidence, dict) else None)
                ref = ref or event.get("source_ref")
                if ref:
                    refs.append(str(ref))
            refs = list(dict.fromkeys(refs))
            if not refs or not hasattr(comms, "reactions_for_refs"):
                return None
            reactions = comms.reactions_for_refs(
                owner, refs, since_iso=start.isoformat(),
                until_iso=end.isoformat())
            by_ref = {str(row.get("reply_to_ref")): row
                      for row in reactions if row.get("reply_to_ref")}
            accepted_names = {"accepted", "acknowledged", "actioned"}
            negative_names = {"negative", "dismissed", "corrected", "rejected"}
            accepted = sum(
                1 for ref in refs
                if str((by_ref.get(ref) or {}).get("reaction") or "").lower()
                in accepted_names)
            negative = sum(
                1 for ref in refs
                if str((by_ref.get(ref) or {}).get("reaction") or "").lower()
                in negative_names)
            return {
                "value": accepted / len(refs),
                "numerator": accepted,
                "denominator": len(refs),
                "detail": {
                    "deliveries": len(refs),
                    "negative": negative,
                    "unanswered": len(refs) - len(by_ref),
                    "metric_definition": "initiative.acceptance/v2",
                    "binding": "reply_to_ref",
                    "competence_reconciliation_revision": revision,
                },
            }
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
                "detail": {
                    "deliveries": len(deliveries),
                    "metric_definition": "colony.initiative-acceptance/v1",
                    "competence_reconciliation_revision": revision,
                }}

    def _m_corrections(self, start, end, since, until):
        """Corrections over the same receipt-backed outbound response cohort."""

        if cognition_p4_mode() != "live":
            return None
        owner = self.owner_contact_id
        comms = self._dep("comms")
        corrections = self._dep("corrections")
        if (not owner or comms is None or corrections is None
                or not hasattr(comms, "outbound_between")
                or not hasattr(corrections, "between")):
            return None
        outbound = comms.outbound_between(
            owner, start.isoformat(), end.isoformat(), require_receipt=True)
        cohort: Dict[str, Dict[str, Any]] = {}
        for row in outbound:
            ref = row.get("external_ref") or row.get("receipt_ref")
            if ref:
                cohort[str(ref)] = row
        if not cohort:
            return None
        corrected: set[str] = set()
        for item in corrections.between(
                start.isoformat(), end.isoformat(), person_id=owner):
            context_ref = (item.get("context_hash") if isinstance(item, dict)
                           else getattr(item, "context_hash", ""))
            if context_ref in cohort:
                corrected.add(str(context_ref))
        return {
            "value": len(corrected) / len(cohort),
            "numerator": len(corrected),
            "denominator": len(cohort),
            "detail": {
                "metric_definition": "responses.correction_rate/v1",
                "cohort": "receipt_backed_owner_outbound",
                "correction_binding": "context_hash_to_external_ref",
            },
        }

    async def _m_recall(self, since, until):
        """Probe: re-query high-confidence shared facts against graph recall
        and grade by token coverage. Records each probe as a sample."""
        rows = self._probe_rows()
        if rows is None or not rows:
            return None
        picks = random.sample(rows, min(self.probe_count, len(rows)))
        return await self._run_probes(picks, source="benchmark")

    async def run_recall_probe(self, probes: int = 50,
                               seed: Optional[int] = None
                               ) -> Optional[Dict[str, Any]]:
        """On-demand recall probe: the same derivation as the weekly
        recall.fact_coverage metric, but with a seeded, deterministic fact
        pick so before/after comparisons measure the recall path rather than
        sampling noise. Read-only against the graph. Samples are recorded
        with source="manual-probe"; rollups never read recall.probe samples,
        so manual probing cannot distort the weekly scorecard."""
        rows = self._probe_rows()
        if rows is None or not rows:
            return None
        n = max(1, min(int(probes), 100))
        picks = random.Random(seed).sample(rows, min(n, len(rows)))
        out = await self._run_probes(picks, source="manual-probe")
        if out is not None:
            out["detail"]["seed"] = seed
            out["detail"]["source"] = "manual-probe"
        return out

    def _probe_rows(self) -> Optional[List[Dict[str, Any]]]:
        """High-confidence shared facts to probe, or None when a source
        (graph or facts store) is unavailable — honest-skip, never zero."""
        graph = self._dep("graph")
        facts = self._dep("facts")
        if graph is None or facts is None:
            return None
        kwargs: Dict[str, Any] = {"min_confidence": 0.75, "limit": 200}
        if cognition_p4_mode() == "live":
            owner = self.owner_contact_id
            if not owner:
                return None
            # The store may call this key contact_id or subject_person_id; the
            # read is narrowed in both the query and the post-filter.
            kwargs["contact_id"] = owner
        rows = facts.list_facts(**kwargs).get("facts", [])
        if cognition_p4_mode() != "live":
            return rows
        allowed: List[Dict[str, Any]] = []
        owner = self.owner_contact_id
        for raw in rows:
            row = raw if isinstance(raw, dict) else vars(raw)
            subject = (row.get("subject_person_id")
                       or row.get("contact_id") or "")
            if not row.get("id"):
                continue
            shareability = str(
                row.get("shareability") or "owner_private").lower()
            if str(subject) != owner:
                continue
            if shareability not in {"owner_private", "shared", "public"}:
                continue
            allowed.append(row)
        return allowed

    async def _run_probes(self, picks: List[Any],
                          source: str) -> Optional[Dict[str, Any]]:
        """Grade each picked fact against graph recall (token coverage),
        recording one recall.probe sample per fact under `source`."""
        graph = self._dep("graph")
        hits = 0
        judged = 0
        for f in picks:
            fact = (f.get("fact") if isinstance(f, dict)
                    else getattr(f, "fact", "")) or ""
            if not fact.strip():
                continue
            subject = (f.get("subject_person_id") or f.get("contact_id")
                       if isinstance(f, dict) else
                       getattr(f, "subject_person_id", None)
                       or getattr(f, "contact_id", None))
            try:
                # bound each probe so a wedged graph connection can't hang the
                # benchmark (and, through it, the autonomy tick)
                recall_kwargs = {"limit": 5, "min_confidence": 0.1}
                if cognition_p4_mode() == "live":
                    if not subject:
                        continue
                    recall_kwargs["person_id"] = str(subject)
                results = await asyncio.wait_for(
                    graph.recall(fact, **recall_kwargs), timeout=8.0)
            except (Exception, asyncio.TimeoutError):
                continue
            hit = 1.0 if self._covered(fact, results) else 0.0
            hits += int(hit)
            judged += 1
            fact_id = (f.get("id") if isinstance(f, dict)
                       else getattr(f, "id", None))
            if cognition_p4_mode() == "live":
                self.store.add_evidence_sample(
                    "recall.fact_coverage", hit, definition_version="v2",
                    sample_principal="benchmark:recall-probe",
                    source_ref=f"fact:{fact_id}",
                    sample_id=(f"recall-{source}-{fact_id}-"
                               f"{int(_now() * 1000000)}"),
                    meta={"fact_id": fact_id, "subject_person_id": subject},
                )
            else:
                self.store.add_sample(
                    "recall.probe", hit, source=source,
                    meta={"fact_id": fact_id})
        n = judged if cognition_p4_mode() == "live" else len(picks)
        if n == 0:
            return None
        return {"value": hits / n, "numerator": hits, "denominator": n,
                "detail": {
                    "probes": n,
                    **({"metric_definition": "recall.fact_coverage/v2",
                        "viewer_scope": self.owner_contact_id}
                       if cognition_p4_mode() == "live" else {}),
                }}

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

    def _m_calibration(self, since: float) -> Dict[str, Any]:
        """Per-domain prediction calibration from the expectation engine
        (Mind M3a), expressed as accuracy = 1 - Brier so higher is better and
        it fits the benchmark's higher-is-better convention."""
        eng = self._host_attr("_expectations")
        if eng is None:
            return {}
        out: Dict[str, Any] = {}
        try:
            cal = eng.calibration(since=since)
        except Exception:
            return {}
        for domain, r in (cal or {}).items():
            brier = r.get("brier")
            if brier is None:
                continue
            out[f"calibration.{domain}"] = {
                "value": max(0.0, 1.0 - float(brier)),
                "numerator": None, "denominator": None,
                "detail": {"brier": brier, "n": r.get("n"),
                           "hit_rate": r.get("hit_rate")}}
        return out

    def _m_submitted(self, since: float, until: float,
                     skip: Optional[set] = None) -> Dict[str, Any]:
        """Roll up host-submitted samples generically."""
        skip = skip or set()
        by_metric: Dict[str, List[float]] = {}
        for s in self.store.samples_in(since, until):
            if s["metric"] == "recall.probe" or s["metric"] in skip:
                continue
            if cognition_p4_mode() == "live":
                definition = self.store.definition(
                    s["metric"], s.get("definition_version") or "")
                if (definition is None
                        or s.get("evidence_status") not in {"observed", "verified"}
                        or not s.get("sample_principal")
                        or not s.get("source_ref")):
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
        # A persisted score computed before a correction is unsafe to show as
        # current truth. Hide it until compute_week rebuilds that exact slice.
        comp = self._dep("competence")
        if comp is not None:
            all_action_domains: List[str] = []
            try:
                all_action_domains = [
                    str(r.get("domain")) for r in comp.snapshot()
                    if isinstance(r, dict) and r.get("domain")
                    and r.get("domain") != "delivery"]
            except Exception:
                pass
            for wk, metrics in rolls.items():
                try:
                    start, end = week_window(wk)
                except (TypeError, ValueError):
                    continue
                since, until = start.timestamp(), end.timestamp()
                for metric, domains in (
                    ("delivery.success", ["delivery"]),
                    ("initiative.acceptance", ["delivery"]),
                    ("actions.success", all_action_domains),
                ):
                    row = metrics.get(metric)
                    if row is None:
                        continue
                    detail = dict(row.get("detail") or {})
                    metric_domains = list(domains)
                    if metric == "actions.success":
                        stored_domains = detail.get("domains") or {}
                        if isinstance(stored_domains, dict):
                            metric_domains = sorted(
                                set(metric_domains) | set(stored_domains))
                    revision, gaps = self._competence_state(
                        metric_domains, since, until)
                    recorded = int(detail.get(
                        "competence_reconciliation_revision") or 0)
                    if gaps:
                        row.update(self._unavailable_competence(
                            revision, gaps))
                    elif revision > recorded:
                        row["value"] = None
                        row["numerator"] = None
                        row["denominator"] = None
                        detail.update({
                            "available": False,
                            "reason": (
                                "stale_after_competence_reconciliation"),
                            "computed_revision": recorded,
                            "required_revision": revision,
                        })
                        row["detail"] = detail
        trends: Dict[str, Any] = {}
        if len(ordered) >= 2:
            cur, prev = rolls[ordered[0]], rolls[ordered[1]]
            for metric, r in cur.items():
                pv = (prev.get(metric) or {}).get("value")
                if r.get("value") is not None and pv is not None:
                    trends[metric] = round(r["value"] - pv, 4)
        return {
            "format": "colony.selfhood-benchmark/v2",
            "mode": cognition_p4_mode(),
            "weeks": ordered,
            "rollups": rolls,
            "trends": trends,
            "latest": ordered[0] if ordered else None,
            "definitions": self.store.definitions(),
        }

    def canonical_summary(self, weeks: int = 8) -> Dict[str, Any]:
        """Canonical replacement payload for the deprecated legacy CPI API."""

        return {
            "deprecated_cpi": True,
            "canonical": "selfhood_benchmark",
            "canonical_endpoint": "/v1/host/self/benchmark",
            **self.snapshot(weeks=weeks),
        }


def legacy_cpi_payload(benchmark: Optional[SelfhoodBenchmark],
                       weeks: int = 8) -> Dict[str, Any]:
    """A truthful compatibility response; no fabricated CPI dimensions."""

    if benchmark is None:
        return {
            "deprecated": True,
            "available": False,
            "canonical_endpoint": "/v1/host/self/benchmark",
            "reason": "canonical benchmark is not wired",
        }
    return {
        "deprecated": True,
        "available": True,
        **benchmark.canonical_summary(weeks=weeks),
    }
