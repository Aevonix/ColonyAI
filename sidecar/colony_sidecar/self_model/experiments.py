"""Receipt-attributed controlled experiments for adaptive parameters.

P4 replaces the historical "move a global knob, then compare a later weekly
rollup" heuristic with an inspectable chain:

``proposal -> bounded authority -> exposure assignment -> receipt outcome ->
minimum-sample/power gate -> explicit causal status -> adopt or revert``

``COLONY_COGNITION_P4_MODE`` defaults to ``off``. In ``shadow`` the complete
evidence path runs but no adaptive parameter is changed. In ``live`` every
behavioral variant must fit a configured reversible pregrant or consume the
existing bounded owner-approval authority. The old global-rollup evaluator is
retained only while the mode is ``off`` for rollback compatibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from statistics import NormalDist
from typing import Any, Dict, List, Mapping, Optional, Tuple

from colony_sidecar.self_model.benchmark import cognition_p4_mode

logger = logging.getLogger(__name__)

STATUSES = (
    "proposed", "starting", "running", "completed", "adopted", "reverted",
    "aborted",
)
CAUSAL_STATUSES = ("observed", "suggestive", "supported")
OWNER_REACTIONS = ("", "positive", "neutral", "negative")


def experiments_enabled() -> bool:
    return os.environ.get(
        "COLONY_EXPERIMENTS_ENABLED", "true").strip().lower() != "false"


def _max_running() -> int:
    try:
        return max(1, min(10, int(os.environ.get(
            "COLONY_EXPERIMENTS_MAX_RUNNING", "2"))))
    except ValueError:
        return 2


def experiment_pregrants_from_env() -> Dict[str, Tuple[float, float]]:
    """Load exact reversible ranges; an absent/invalid policy grants nothing."""

    raw = os.environ.get("COLONY_EXPERIMENT_PREGRANTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("COLONY_EXPERIMENT_PREGRANTS_JSON is invalid JSON") from exc
    if not isinstance(decoded, dict) or len(decoded) > 100:
        raise ValueError("experiment pregrants must be a small JSON object")
    result: Dict[str, Tuple[float, float]] = {}
    for ref, bounds in decoded.items():
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 192:
            raise ValueError("experiment pregrant contains an invalid parameter")
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError("experiment pregrant range must be [lo, hi]")
        lo = _finite(bounds[0], field="pregrant lo")
        hi = _finite(bounds[1], field="pregrant hi")
        result[ref.strip()] = (min(lo, hi), max(lo, hi))
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


class ExperimentApprovalRequired(ValueError):
    """A proposal is durable but cannot start until its exact digest wins."""

    def __init__(self, experiment: Dict[str, Any]) -> None:
        self.experiment = experiment
        request_id = experiment.get("approval_request_id") or "unknown"
        super().__init__(f"bounded owner approval required ({request_id})")


class ExperimentStore:
    """Additive SQLite ledger for experiments, exposures, and outcomes."""

    _EXPERIMENT_ADDITIONS = {
        "metric_version": "TEXT",
        "execution_mode": "TEXT",
        "assignment_mode": "TEXT",
        "control_ratio": "REAL",
        "min_control_samples": "INTEGER",
        "min_variant_samples": "INTEGER",
        "min_total_samples": "INTEGER",
        "min_power": "REAL",
        "min_effect": "REAL",
        "owner_negative_limit": "INTEGER",
        "causal_status": "TEXT",
        "authority_mode": "TEXT",
        "approval_request_id": "TEXT",
        "action_digest": "TEXT",
        "mutation_applied": "INTEGER DEFAULT 0",
        "source_ref": "TEXT",
        "evidence_summary": "TEXT",
    }

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
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
            CREATE INDEX IF NOT EXISTS idx_exp_status ON experiments(status);
            CREATE TABLE IF NOT EXISTS experiment_exposures (
                exposure_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                unit_hash TEXT NOT NULL,
                cohort TEXT NOT NULL,
                selected_value REAL NOT NULL,
                sample_principal TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                receipt_ref TEXT,
                exposed_at REAL NOT NULL,
                exposure_digest TEXT NOT NULL,
                UNIQUE(experiment_id, unit_hash),
                FOREIGN KEY(experiment_id) REFERENCES experiments(id)
            );
            CREATE INDEX IF NOT EXISTS idx_exposure_exp_cohort
                ON experiment_exposures(experiment_id,cohort,exposed_at);
            CREATE TABLE IF NOT EXISTS experiment_outcomes (
                outcome_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                exposure_id TEXT NOT NULL,
                metric TEXT NOT NULL,
                metric_version TEXT NOT NULL,
                value REAL NOT NULL,
                sample_principal TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                receipt_ref TEXT NOT NULL,
                owner_reaction TEXT,
                recorded_at REAL NOT NULL,
                outcome_digest TEXT NOT NULL,
                UNIQUE(experiment_id, exposure_id),
                FOREIGN KEY(experiment_id) REFERENCES experiments(id),
                FOREIGN KEY(exposure_id) REFERENCES experiment_exposures(exposure_id)
            );
            CREATE INDEX IF NOT EXISTS idx_outcome_exp
                ON experiment_outcomes(experiment_id,recorded_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outcome_receipt_once
                ON experiment_outcomes(experiment_id,receipt_ref);
            CREATE TABLE IF NOT EXISTS experiment_mutations (
                mutation_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                ref TEXT NOT NULL,
                prior_value REAL,
                requested_value REAL NOT NULL,
                applied_value REAL,
                operation TEXT NOT NULL,
                authority_mode TEXT NOT NULL,
                created_at REAL NOT NULL,
                mutation_digest TEXT NOT NULL,
                FOREIGN KEY(experiment_id) REFERENCES experiments(id)
            );
            CREATE TRIGGER IF NOT EXISTS experiment_exposure_no_update
                BEFORE UPDATE ON experiment_exposures BEGIN
                    SELECT RAISE(ABORT, 'experiment exposure is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS experiment_exposure_no_delete
                BEFORE DELETE ON experiment_exposures BEGIN
                    SELECT RAISE(ABORT, 'experiment exposure is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS experiment_outcome_no_update
                BEFORE UPDATE ON experiment_outcomes BEGIN
                    SELECT RAISE(ABORT, 'experiment outcome is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS experiment_outcome_no_delete
                BEFORE DELETE ON experiment_outcomes BEGIN
                    SELECT RAISE(ABORT, 'experiment outcome is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS experiment_mutation_no_update
                BEFORE UPDATE ON experiment_mutations BEGIN
                    SELECT RAISE(ABORT, 'experiment mutation is immutable');
                END;
            CREATE TRIGGER IF NOT EXISTS experiment_mutation_no_delete
                BEFORE DELETE ON experiment_mutations BEGIN
                    SELECT RAISE(ABORT, 'experiment mutation is immutable');
                END;
            """
        )
        columns = {str(row[1]) for row in self._conn.execute(
            "PRAGMA table_info(experiments)").fetchall()}
        for name, sql_type in self._EXPERIMENT_ADDITIONS.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE experiments ADD COLUMN {name} {sql_type}")
        self._conn.commit()

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        result = dict(row)
        if result.get("evidence_summary"):
            try:
                result["evidence_summary"] = json.loads(
                    result["evidence_summary"])
            except ValueError:
                pass
        return result

    def add(self, row: Dict[str, Any]) -> None:
        columns = ",".join(row.keys())
        placeholders = ",".join("?" for _ in row)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO experiments ({columns}) VALUES ({placeholders})",
                list(row.values()))
            self._conn.commit()

    def update(self, exp_id: str, **fields: Any) -> None:
        if not fields:
            return
        encoded = dict(fields)
        if isinstance(encoded.get("evidence_summary"), (dict, list)):
            encoded["evidence_summary"] = _canonical(
                encoded["evidence_summary"])
        sets = ",".join(f"{key}=?" for key in encoded)
        with self._lock:
            self._conn.execute(
                f"UPDATE experiments SET {sets} WHERE id=?",
                [*encoded.values(), exp_id])
            self._conn.commit()

    def get(self, exp_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
        return self._decode(row)

    def list(self, status: Optional[str] = None,
             limit: int = 50) -> List[Dict[str, Any]]:
        query = "SELECT * FROM experiments"
        params: List[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._decode(row) or {} for row in rows]

    def running_for_ref(self, ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiments WHERE ref=? AND status IN "
                "('proposed','starting','running') ORDER BY created_at DESC LIMIT 1",
                (ref,),
            ).fetchone()
        return self._decode(row)

    def running_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM experiments WHERE status IN "
                "('starting','running')"
            ).fetchone()
        return int(row["n"])

    def add_exposure(self, row: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM experiment_exposures WHERE experiment_id=? "
                "AND unit_hash=?", (row["experiment_id"], row["unit_hash"])
            ).fetchone()
            if existing is not None:
                stable = (
                    "experiment_id", "unit_hash", "cohort", "selected_value",
                    "sample_principal", "source_ref", "receipt_ref",
                )
                if any(existing[key] != row[key] for key in stable):
                    raise ValueError("exposure retry changed immutable assignment")
                return dict(existing)
            columns = ",".join(row.keys())
            placeholders = ",".join("?" for _ in row)
            self._conn.execute(
                f"INSERT INTO experiment_exposures ({columns}) "
                f"VALUES ({placeholders})", list(row.values()))
            self._conn.commit()
            saved = self._conn.execute(
                "SELECT * FROM experiment_exposures WHERE exposure_id=?",
                (row["exposure_id"],)).fetchone()
        assert saved is not None
        return dict(saved)

    def exposure(self, exposure_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiment_exposures WHERE exposure_id=?",
                (exposure_id,),).fetchone()
        return dict(row) if row else None

    def exposure_for_unit(self, experiment_id: str,
                          unit_hash: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiment_exposures WHERE experiment_id=? "
                "AND unit_hash=?", (experiment_id, unit_hash)).fetchone()
        return dict(row) if row else None

    def exposures(self, experiment_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiment_exposures WHERE experiment_id=? "
                "ORDER BY exposed_at,exposure_id", (experiment_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_outcome(self, row: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM experiment_outcomes WHERE experiment_id=? "
                "AND exposure_id=?",
                (row["experiment_id"], row["exposure_id"]),
            ).fetchone()
            if existing is not None:
                stable = (
                    "experiment_id", "exposure_id", "metric",
                    "metric_version", "value", "sample_principal",
                    "source_ref", "receipt_ref", "owner_reaction",
                )
                if any(existing[key] != row[key] for key in stable):
                    raise ValueError("outcome retry changed immutable evidence")
                return dict(existing)
            reused = self._conn.execute(
                "SELECT exposure_id FROM experiment_outcomes "
                "WHERE experiment_id=? AND receipt_ref=?",
                (row["experiment_id"], row["receipt_ref"]),
            ).fetchone()
            if reused is not None:
                raise ValueError(
                    "one verifier receipt cannot judge multiple exposures")
            columns = ",".join(row.keys())
            placeholders = ",".join("?" for _ in row)
            self._conn.execute(
                f"INSERT INTO experiment_outcomes ({columns}) "
                f"VALUES ({placeholders})", list(row.values()))
            self._conn.commit()
            saved = self._conn.execute(
                "SELECT * FROM experiment_outcomes WHERE outcome_id=?",
                (row["outcome_id"],)).fetchone()
        assert saved is not None
        return dict(saved)

    def outcomes(self, experiment_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.*,e.cohort,e.selected_value "
                "FROM experiment_outcomes o JOIN experiment_exposures e "
                "ON e.exposure_id=o.exposure_id WHERE o.experiment_id=? "
                "ORDER BY o.recorded_at,o.outcome_id", (experiment_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def outcome_for_exposure(self, experiment_id: str,
                             exposure_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiment_outcomes WHERE experiment_id=? "
                "AND exposure_id=?", (experiment_id, exposure_id)).fetchone()
        return dict(row) if row else None

    def add_mutation(self, row: Dict[str, Any]) -> None:
        columns = ",".join(row.keys())
        placeholders = ",".join("?" for _ in row)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO experiment_mutations ({columns}) "
                f"VALUES ({placeholders})", list(row.values()))
            self._conn.commit()

    def mutations(self, experiment_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiment_mutations WHERE experiment_id=? "
                "ORDER BY created_at,mutation_id", (experiment_id,)).fetchall()
        return [dict(row) for row in rows]


class ExperimentEngine:
    """The sole adaptive-parameter writer and causal evidence reducer."""

    def __init__(
        self,
        store: ExperimentStore,
        *,
        params: Any = None,
        benchmark: Any = None,
        journal: Any = None,
        approval_authority: Any = None,
        pregranted_ranges: Optional[Mapping[str, Tuple[float, float]]] = None,
    ) -> None:
        self.store = store
        self._params = params
        self._benchmark = benchmark
        self._journal = journal
        self._approval_authority = approval_authority
        self._pregrants = self._normalize_pregrants(pregranted_ranges or {})
        self._writer_token = None
        if params is not None and hasattr(params, "claim_experiment_writer"):
            self._writer_token = params.claim_experiment_writer(
                f"ExperimentEngine:{id(self)}")
        self._recover_incomplete_starts()

    @staticmethod
    def _normalize_pregrants(raw: Mapping[str, Tuple[float, float]]
                             ) -> Dict[str, Tuple[float, float]]:
        result: Dict[str, Tuple[float, float]] = {}
        for ref, bounds in raw.items():
            if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
                raise ValueError("experiment pregrant bounds must be [lo, hi]")
            lo, hi = _finite(bounds[0], field="pregrant lo"), _finite(
                bounds[1], field="pregrant hi")
            result[str(ref)] = (min(lo, hi), max(lo, hi))
        return result

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
            model = getattr(host, "_self_model", None)
            return getattr(model, "journal", None) if model is not None else None
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

    def _known_param(self, ref: str) -> Tuple[Any, Dict[str, Any]]:
        params = self._p()
        if params is None:
            raise ValueError("adaptive params store not wired")
        known = {row["name"]: row for row in params.snapshot()}
        if ref not in known:
            raise ValueError(f"unknown adaptive param {ref!r}")
        return params, known[ref]

    def propose(
        self,
        *,
        hypothesis: str,
        ref: str,
        variant: float,
        metric: str,
        max_regression: float = 0.05,
        window_days: int = 7,
        kind: str = "param",
        source: str = "api",
        metric_version: str = "",
        assignment_mode: str = "",
        control_ratio: float = 0.5,
        min_control_samples: int = 20,
        min_variant_samples: int = 20,
        min_total_samples: int = 40,
        min_power: float = 0.8,
        min_effect: float = 0.0,
        owner_negative_limit: int = 1,
    ) -> Dict[str, Any]:
        if not experiments_enabled():
            raise ValueError("experiments disabled "
                             "(COLONY_EXPERIMENTS_ENABLED=false)")
        if kind != "param":
            raise ValueError(f"kind {kind!r} not runnable (param only)")
        normalized_ref = (ref or "").strip()
        normalized_metric = (metric or "").strip().lower()
        params, param = self._known_param(normalized_ref)
        if self.store.running_for_ref(normalized_ref) is not None:
            raise ValueError(f"an experiment on {normalized_ref!r} is already open")
        if self.store.running_count() >= _max_running():
            raise ValueError(f"running-experiment cap reached ({_max_running()})")

        mode = cognition_p4_mode()
        bench = self._b()
        if bench is None:
            raise ValueError("benchmark not wired (no evidence judge)")
        baseline_week: Optional[str] = None
        baseline_metric: Optional[float] = None
        version = (metric_version or "").strip().lower()
        if mode == "off":
            for week in sorted(bench.store.rollups(weeks=4), reverse=True):
                row = bench.store.rollups(weeks=4)[week].get(normalized_metric)
                if row and row.get("value") is not None:
                    baseline_week = week
                    baseline_metric = float(row["value"])
                    break
            if baseline_metric is None:
                raise ValueError(
                    f"no rollup exists for metric {normalized_metric!r}; "
                    "a baseline is required before experimenting")
            assignment = "global"
            version = version or "legacy.unversioned"
        else:
            if not version or bench.store.definition(
                    normalized_metric, version) is None:
                raise ValueError("registered metric definition/version is required")
            assignment = (assignment_mode or "cohort").strip().lower()
            if assignment not in {"cohort", "global"}:
                raise ValueError("assignment_mode must be cohort or global")

        variant_value = _finite(variant, field="variant")
        max_reg = _finite(max_regression, field="max_regression")
        if max_reg < 0:
            raise ValueError("max_regression must be non-negative")
        ratio = _finite(control_ratio, field="control_ratio")
        if not 0.05 <= ratio <= 0.95:
            raise ValueError("control_ratio must be between 0.05 and 0.95")
        power = _finite(min_power, field="min_power")
        if not 0.0 <= power <= 1.0:
            raise ValueError("min_power must be between 0 and 1")
        effect = _finite(min_effect, field="min_effect")
        if effect < 0:
            raise ValueError("min_effect must be non-negative")
        control_n = max(1, min(100000, int(min_control_samples)))
        variant_n = max(1, min(100000, int(min_variant_samples)))
        total_n = max(control_n + variant_n,
                      min(200000, int(min_total_samples)))
        negative_limit = max(1, min(1000, int(owner_negative_limit)))
        duration = max(1, min(28, int(window_days)))

        exp_id = f"exp-{uuid.uuid4().hex[:12]}"
        now = time.time()
        self.store.add({
            "id": exp_id,
            "hypothesis": (hypothesis or "")[:500],
            "kind": kind,
            "ref": normalized_ref,
            "variant": variant_value,
            "baseline_param": float(param["effective"]),
            "metric": normalized_metric,
            "metric_version": version,
            "baseline_metric": baseline_metric,
            "baseline_week": baseline_week,
            "max_regression": max_reg,
            "window_days": duration,
            "status": "proposed",
            "created_at": now,
            "source": (source or "unknown")[:128],
            "source_ref": (source or "unknown")[:512],
            "execution_mode": mode,
            "assignment_mode": assignment,
            "control_ratio": ratio,
            "min_control_samples": control_n,
            "min_variant_samples": variant_n,
            "min_total_samples": total_n,
            "min_power": power,
            "min_effect": effect,
            "owner_negative_limit": negative_limit,
            "causal_status": "observed",
            "authority_mode": "pending",
            "mutation_applied": 0,
        })
        return self.store.get(exp_id) or {}

    def propose_and_start(self, **kwargs: Any) -> Dict[str, Any]:
        proposal = self.propose(**kwargs)
        return self.start(proposal["id"])

    def _binding(self, exp: Mapping[str, Any]) -> Any:
        from colony_sidecar.initiatives.approval_authority import ActionBinding

        constraints = {
            name: _digest(exp.get(name)) for name in (
                "ref", "variant", "metric", "metric_version",
                "max_regression", "window_days", "assignment_mode",
            )
        }
        scope = {
            "version": 1,
            "job_type": "cognition_experiment",
            "action_name": "cognition_experiment_mutation",
            "risk": "mutating",
            "constraints": constraints,
        }
        action = {
            "version": 1,
            "experiment_id": exp["id"],
            "hypothesis": exp["hypothesis"],
            "ref": exp["ref"],
            "baseline_param": exp["baseline_param"],
            "variant": exp["variant"],
            "metric": exp["metric"],
            "metric_version": exp.get("metric_version"),
            "max_regression": exp["max_regression"],
            "window_days": exp["window_days"],
            "assignment_mode": exp.get("assignment_mode"),
        }
        return ActionBinding(
            action_digest=_digest(action), scope=scope,
            scope_digest=_digest(scope))

    def _authorize(self, exp: Dict[str, Any]) -> str:
        mode = exp.get("execution_mode") or cognition_p4_mode()
        if mode == "off":
            return "legacy_compatibility"
        if mode == "shadow":
            return "shadow_no_effect"
        bounds = self._pregrants.get(str(exp["ref"]))
        if bounds and bounds[0] <= float(exp["variant"]) <= bounds[1]:
            return "bounded_pregrant"
        if self._approval_authority is None:
            raise ValueError(
                "bounded approval authority is required for a live mutation")
        binding = self._binding(exp)
        self.store.update(exp["id"], action_digest=binding.action_digest)
        if exp.get("approval_request_id"):
            request = self._approval_authority.get_request(
                exp["approval_request_id"])
            if (request and request.get("status") == "approved"
                    and request.get("action_digest") == binding.action_digest):
                return "owner_approved"
        grant = self._approval_authority.consume_grant(
            binding=binding, operation_id=exp["id"])
        if grant is not None:
            return "bounded_grant"
        request = self._approval_authority.ensure_request(
            job_id=exp["id"], binding=binding)
        self.store.update(
            exp["id"], approval_request_id=request["request_id"],
            action_digest=binding.action_digest)
        raise ExperimentApprovalRequired(self.store.get(exp["id"]) or exp)

    def _set_param(self, exp: Mapping[str, Any], value: float,
                   *, operation: str, authority_mode: str) -> float:
        params = self._p()
        if params is None:
            raise ValueError("adaptive params store not wired")
        prior_value = self._current_param(exp["ref"])
        kwargs = {
            "reason": f"experiment {exp['id']} {operation}",
            "source": f"experiment:{exp['id']}",
        }
        if self._writer_token is not None:
            kwargs["writer_token"] = self._writer_token
        applied = params.set(exp["ref"], float(value), **kwargs)
        if applied is None:
            raise ValueError(f"param store refused {exp['ref']!r}")
        mutation = {
            "experiment_id": exp["id"], "ref": exp["ref"],
            "prior_value": prior_value,
            "requested_value": float(value), "applied_value": float(applied),
            "operation": operation, "authority_mode": authority_mode,
            "created_at": time.time(),
        }
        mutation["mutation_digest"] = _digest(mutation)
        mutation["mutation_id"] = "mut-" + mutation["mutation_digest"][:24]
        self.store.add_mutation(mutation)
        return float(applied)

    def _current_param(self, ref: str) -> Optional[float]:
        params = self._p()
        if params is None:
            return None
        for row in params.snapshot():
            if row["name"] == ref:
                return float(row["effective"])
        return None

    def _recover_incomplete_starts(self) -> None:
        """Fail safe across a crash between mutation intent and running state."""

        for exp in self.store.list(status="starting", limit=100):
            current = self._current_param(exp["ref"])
            if current is None:
                raise RuntimeError(
                    f"cannot recover incomplete experiment {exp['id']}: "
                    "adaptive parameter is unavailable")
            reason = "recovered incomplete experiment start"
            if (current is not None and exp.get("baseline_param") is not None
                    and abs(current - float(exp["variant"])) <= 1e-9):
                try:
                    self._set_param(
                        exp, float(exp["baseline_param"]),
                        operation="recover_incomplete_start",
                        authority_mode=exp.get("authority_mode") or "recovery")
                    reason += "; restored baseline"
                except Exception as exc:
                    # Keep the starting record visible and fail construction;
                    # silently booting with an untracked variant is unsafe.
                    raise RuntimeError(
                        f"could not restore incomplete experiment {exp['id']}: "
                        f"{exc}") from exc
            elif (current is not None and exp.get("baseline_param") is not None
                  and abs(current - float(exp["baseline_param"])) > 1e-9):
                reason += "; parameter was superseded and was not overwritten"
            self.store.update(
                exp["id"], status="aborted", decided_at=time.time(),
                decision_reason=reason, mutation_applied=0,
                causal_status="observed")

    def start(self, exp_id: str) -> Dict[str, Any]:
        exp = self.store.get(exp_id)
        if not exp or exp["status"] != "proposed":
            raise ValueError("experiment is not proposed")
        current = self._current_param(exp["ref"])
        if current is None:
            raise ValueError("adaptive parameter disappeared")
        if abs(current - float(exp["baseline_param"])) > 1e-9:
            self.store.update(
                exp_id, status="aborted", decided_at=time.time(),
                decision_reason="superseded before start: parameter changed")
            raise ValueError("parameter changed after proposal")
        authority_mode = self._authorize(exp)
        mutation_applied = 0
        now = time.time()
        self.store.update(
            exp_id, status="starting", started_at=now,
            ends_at=now + int(exp["window_days"]) * 86400,
            authority_mode=authority_mode,
            mutation_applied=0)
        try:
            if exp["execution_mode"] in {"off", "live"} and \
                    exp["assignment_mode"] == "global":
                self._set_param(
                    exp, float(exp["variant"]), operation="apply_variant",
                    authority_mode=authority_mode)
                mutation_applied = 1
        except Exception:
            self.store.update(
                exp_id, status="aborted", decided_at=time.time(),
                decision_reason="variant application failed", mutation_applied=0)
            raise
        self.store.update(
            exp_id, status="running", mutation_applied=mutation_applied)
        started = self.store.get(exp_id) or {}
        self._log(
            f"experiment {exp_id} started ({started['execution_mode']}, "
            f"{started['assignment_mode']})",
            reasoning=started["hypothesis"],
            outcome=f"authority={authority_mode}", ref=exp_id)
        return started

    def abort(self, exp_id: str, reason: str = "manual abort") -> bool:
        exp = self.store.get(exp_id)
        if not exp or exp["status"] not in {"proposed", "starting", "running"}:
            return False
        self._revert(exp, status="aborted", reason=reason)
        return True

    def assign_exposure(
        self,
        exp_id: str,
        *,
        unit_id: str,
        sample_principal: str,
        source_ref: str,
        receipt_ref: str = "",
        exposed_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        exp = self.store.get(exp_id)
        if not exp:
            raise ValueError("experiment was not found")
        unit = (unit_id or "").strip()
        principal = (sample_principal or "").strip()
        source = (source_ref or "").strip()
        if not unit or not principal or not source:
            raise ValueError("unit_id, sample_principal, and source_ref are required")
        unit_hash = hashlib.sha256(unit.encode("utf-8")).hexdigest()
        if exp["assignment_mode"] == "global":
            cohort = "variant"
        else:
            draw = int(hashlib.sha256(
                f"{exp_id}:{unit_hash}".encode("utf-8")).hexdigest()[:13], 16)
            fraction = draw / float(0xFFFFFFFFFFFFF)
            cohort = "control" if fraction < float(exp["control_ratio"]) \
                else "variant"
        selected = (float(exp["baseline_param"]) if cohort == "control"
                    else float(exp["variant"]))
        payload = {
            "experiment_id": exp_id, "unit_hash": unit_hash,
            "cohort": cohort, "selected_value": selected,
            "sample_principal": principal[:192], "source_ref": source[:512],
            "receipt_ref": (receipt_ref or "")[:512] or None,
            "exposed_at": float(exposed_at) if exposed_at is not None else time.time(),
        }
        payload["exposure_digest"] = _digest(payload)
        payload["exposure_id"] = "xps-" + _digest(
            {"experiment_id": exp_id, "unit_hash": unit_hash})[:24]
        if exp["status"] != "running" and self.store.exposure_for_unit(
                exp_id, unit_hash) is None:
            raise ValueError("experiment is not running")
        return self.store.add_exposure(payload)

    def record_outcome(
        self,
        exp_id: str,
        *,
        exposure_id: str,
        value: float,
        sample_principal: str,
        source_ref: str,
        receipt_ref: str,
        owner_reaction: str = "",
        outcome_id: str = "",
        recorded_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        exp = self.store.get(exp_id)
        if not exp:
            raise ValueError("experiment was not found")
        exposure = self.store.exposure(exposure_id)
        if not exposure or exposure["experiment_id"] != exp_id:
            raise ValueError("outcome must name an exposure from this experiment")
        principal = (sample_principal or "").strip()
        source = (source_ref or "").strip()
        receipt = (receipt_ref or "").strip()
        if not principal or not source or not receipt:
            raise ValueError(
                "sample_principal, source_ref, and receipt_ref are required")
        reaction = (owner_reaction or "").strip().lower()
        if reaction not in OWNER_REACTIONS:
            raise ValueError("owner_reaction is invalid")
        payload = {
            "experiment_id": exp_id, "exposure_id": exposure_id,
            "metric": exp["metric"],
            "metric_version": exp.get("metric_version") or "legacy.unversioned",
            "value": _finite(value, field="outcome value"),
            "sample_principal": principal[:192], "source_ref": source[:512],
            "receipt_ref": receipt[:512], "owner_reaction": reaction or None,
            "recorded_at": (float(recorded_at) if recorded_at is not None
                            else time.time()),
        }
        payload["outcome_digest"] = _digest(payload)
        payload["outcome_id"] = ((outcome_id or "").strip()
                                 or "out-" + payload["outcome_digest"][:24])
        if (exp["status"] != "running"
                and self.store.outcome_for_exposure(
                    exp_id, exposure_id) is None):
            raise ValueError("experiment is not running")
        saved = self.store.add_outcome(payload)
        if exp["status"] != "running":
            return saved
        negative = sum(
            1 for row in self.store.outcomes(exp_id)
            if row.get("owner_reaction") == "negative")
        if negative >= int(exp.get("owner_negative_limit") or 1):
            self._revert(
                exp, status="aborted",
                reason=(f"owner-negative abort: {negative} receipt-linked "
                        "negative reaction(s)"))
        return saved

    def evidence(self, exp_id: str) -> Dict[str, Any]:
        return {
            "experiment": self.store.get(exp_id),
            "exposures": self.store.exposures(exp_id),
            "outcomes": self.store.outcomes(exp_id),
            "mutations": self.store.mutations(exp_id),
        }

    @staticmethod
    def _stats(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
        groups: Dict[str, List[float]] = {"control": [], "variant": []}
        for row in outcomes:
            cohort = row.get("cohort")
            if cohort in groups:
                groups[cohort].append(float(row["value"]))

        def summarize(values: List[float]) -> Dict[str, float | int | None]:
            if not values:
                return {"n": 0, "mean": None, "variance": None}
            mean = sum(values) / len(values)
            variance = (sum((value - mean) ** 2 for value in values)
                        / (len(values) - 1)) if len(values) > 1 else 0.0
            return {"n": len(values), "mean": mean, "variance": variance}

        return {key: summarize(values) for key, values in groups.items()}

    @staticmethod
    def _power(control: Dict[str, Any], variant: Dict[str, Any],
               effect: float) -> float:
        n_control, n_variant = int(control["n"]), int(variant["n"])
        if n_control < 2 or n_variant < 2:
            return 0.0
        variance = (float(control["variance"] or 0.0) / n_control
                    + float(variant["variance"] or 0.0) / n_variant)
        if variance <= 1e-15:
            return 1.0 if effect > 0 else 0.05
        z_effect = effect / math.sqrt(variance)
        return max(0.0, min(1.0, NormalDist().cdf(z_effect - 1.9599639845)))

    def _directional_effect(self, exp: Dict[str, Any], stats: Dict[str, Any]
                            ) -> Tuple[Optional[float], str]:
        control = stats["control"]["mean"]
        variant = stats["variant"]["mean"]
        if control is None or variant is None:
            return None, "higher"
        direction = "higher"
        bench = self._b()
        if bench is not None:
            definition = bench.store.definition(
                exp["metric"], exp.get("metric_version") or "")
            if definition:
                direction = definition["direction"]
        raw = float(variant) - float(control)
        return (raw if direction == "higher" else -raw), direction

    def evaluate(self) -> List[Dict[str, Any]]:
        legacy_decided = self._evaluate_legacy()
        decided: List[Dict[str, Any]] = []
        now = time.time()
        for exp in self.store.list(status="running", limit=100):
            if (exp.get("execution_mode") or "off") == "off":
                continue
            if exp.get("mutation_applied"):
                current = self._current_param(exp["ref"])
                if current is not None and abs(
                        current - float(exp["variant"])) > 1e-9:
                    self.store.update(
                        exp["id"], status="aborted", decided_at=now,
                        causal_status="observed",
                        decision_reason="superseded: parameter changed outside "
                                        "ExperimentEngine")
                    decided.append(self.store.get(exp["id"]) or {})
                    continue

            outcomes = self.store.outcomes(exp["id"])
            negative = sum(
                1 for row in outcomes if row.get("owner_reaction") == "negative")
            if negative >= int(exp.get("owner_negative_limit") or 1):
                self._revert(
                    exp, status="aborted",
                    reason=f"owner-negative abort: {negative} reaction(s)")
                decided.append(self.store.get(exp["id"]) or {})
                continue
            stats = self._stats(outcomes)
            effect, direction = self._directional_effect(exp, stats)
            control_n = int(stats["control"]["n"])
            variant_n = int(stats["variant"]["n"])
            total = control_n + variant_n
            minimums_met = (
                control_n >= int(exp["min_control_samples"] or 1)
                and variant_n >= int(exp["min_variant_samples"] or 1)
                and total >= int(exp["min_total_samples"] or 2))
            power = self._power(stats["control"], stats["variant"],
                                abs(effect or 0.0))
            causal = "observed"
            if effect is not None and control_n >= 2 and variant_n >= 2:
                causal = "suggestive"
            if (minimums_met and power >= float(exp["min_power"] or 0.0)
                    and effect is not None
                    and effect >= float(exp["min_effect"] or 0.0)):
                causal = "supported"
            summary = {
                "control": stats["control"], "variant": stats["variant"],
                "total": total, "direction": direction,
                "directional_effect": effect, "estimated_power": power,
                "minimums_met": minimums_met,
            }
            self.store.update(
                exp["id"], causal_status=causal, evidence_summary=summary)

            # A sufficiently sampled regression is a safety decision and does
            # not wait for the nominal end date.
            if (minimums_met and effect is not None
                    and effect < -float(exp["max_regression"])):
                self._revert(
                    exp, status="reverted",
                    reason=(f"receipt-linked variant regression "
                            f"{abs(effect):.6f} exceeded guard "
                            f"{float(exp['max_regression']):.6f}"))
                decided.append(self.store.get(exp["id"]) or {})
                continue
            if now < float(exp.get("ends_at") or 0):
                continue
            if exp.get("assignment_mode") != "cohort":
                self._revert(
                    exp, status="reverted",
                    reason="no control cohort; causal attribution unavailable")
            elif not minimums_met:
                self._revert(
                    exp, status="reverted",
                    reason=("minimum sample gate unmet: "
                            f"control={control_n}, variant={variant_n}, total={total}"))
            elif power < float(exp["min_power"] or 0.0):
                self._revert(
                    exp, status="reverted",
                    reason=(f"minimum power gate unmet: {power:.4f} < "
                            f"{float(exp['min_power']):.4f}"))
            else:
                reason = (f"controlled evidence complete; causal_status={causal}; "
                          f"directional_effect={float(effect or 0.0):.6f}; "
                          f"power={power:.4f}")
                self.store.update(
                    exp["id"], status="completed", decided_at=now,
                    decision_reason=reason, causal_status=causal)
                self._log(
                    f"experiment {exp['id']} completed controlled evidence",
                    outcome=reason, ref=exp["id"])
            decided.append(self.store.get(exp["id"]) or {})
        return [*legacy_decided, *decided]

    def _evaluate_legacy(self) -> List[Dict[str, Any]]:
        decided: List[Dict[str, Any]] = []
        bench = self._b()
        for exp in self.store.list(status="running", limit=20):
            if (exp.get("execution_mode") or "off") != "off":
                continue
            current = self._current_param(exp["ref"])
            if current is not None and abs(current - float(exp["variant"])) > 1e-9:
                self.store.update(
                    exp["id"], status="aborted", decided_at=time.time(),
                    decision_reason="superseded: parameter changed outside the experiment")
                decided.append(self.store.get(exp["id"]) or {})
                continue
            if time.time() < float(exp.get("ends_at") or 0) or bench is None:
                continue
            week, value = self._latest_metric(bench, exp["metric"])
            if value is None or week == exp.get("baseline_week"):
                continue
            higher_is_better = not str(exp["metric"]).startswith("latency.")
            delta = value - float(exp["baseline_metric"])
            regression = -delta if higher_is_better else delta
            if regression > float(exp["max_regression"]):
                self._revert(
                    exp, status="reverted",
                    reason=(f"{exp['metric']} {exp['baseline_metric']:.3f} -> "
                            f"{value:.3f} ({week}); regression {regression:.3f} "
                            f"> guard {exp['max_regression']}"))
            else:
                self.store.update(
                    exp["id"], status="adopted", decided_at=time.time(),
                    causal_status="observed",
                    decision_reason=(f"legacy non-causal comparison: {exp['metric']} "
                                     f"{exp['baseline_metric']:.3f} -> "
                                     f"{value:.3f} ({week}); within guard"))
            decided.append(self.store.get(exp["id"]) or {})
        return decided

    @staticmethod
    def _latest_metric(bench: Any, metric: str) -> Tuple[Optional[str],
                                                         Optional[float]]:
        rolls = bench.store.rollups(weeks=4)
        for week in sorted(rolls, reverse=True):
            row = rolls[week].get(metric)
            if row and row.get("value") is not None:
                return week, float(row["value"])
        return None, None

    def _revert(self, exp: Dict[str, Any], *, status: str,
                reason: str) -> None:
        if exp.get("mutation_applied") and exp.get("baseline_param") is not None:
            current = self._current_param(exp["ref"])
            # Never overwrite a third-party superseding mutation.
            if current is not None and abs(
                    current - float(exp["variant"])) <= 1e-9:
                self._set_param(
                    exp, float(exp["baseline_param"]), operation="revert",
                    authority_mode=exp.get("authority_mode") or "unknown")
        self.store.update(
            exp["id"], status=status, decided_at=time.time(),
            decision_reason=reason[:1000], mutation_applied=0)
        self._log(
            f"experiment {exp['id']} {status}", reasoning=reason[:500],
            ref=exp["id"])

    def snapshot(self, limit: int = 30) -> Dict[str, Any]:
        experiments = self.store.list(limit=limit)
        return {
            "mode": cognition_p4_mode(),
            "running": [row for row in experiments
                        if row["status"] in {"starting", "running"}],
            "proposed": [row for row in experiments if row["status"] == "proposed"],
            "recent": [row for row in experiments
                       if row["status"] not in {
                           "starting", "running", "proposed"}],
            "enabled": experiments_enabled(),
            "max_running": _max_running(),
            "adaptive_parameter_writer": "ExperimentEngine",
        }
