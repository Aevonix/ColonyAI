"""Colony Identity Bootstrap — IdentityBootstrap orchestrator.

Determines bootstrap mode (FIRST_BOOT / REGEN / VERIFY), runs seeders
in parallel groups, executes the 16-point self-check, and persists a
BootstrapReport to ~/.colony/bootstrap.db.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import json
from datetime import datetime, timezone, timedelta
from typing import Any, List, Optional

from colony_sidecar.identity_bootstrap.builder import IdentityBootstrapBuilder
from colony_sidecar.identity_bootstrap.models import BootstrapAnomaly, BootstrapReport
from colony_sidecar.identity_bootstrap.verifier import BootstrapVerifier
from colony_sidecar.identity_bootstrap.self_reflection import SelfReflectionComponent

logger = logging.getLogger(__name__)

# Regen if last bootstrap was more than 30 days ago
_REGEN_THRESHOLD_DAYS = 30

# ── Persistence ───────────────────────────────────────────────────────────────

_BOOTSTRAP_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS bootstrap_runs (
    run_id          TEXT PRIMARY KEY,
    colony_id       TEXT NOT NULL,
    mode            TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT NOT NULL,
    success         INTEGER NOT NULL,
    corpus_version  TEXT NOT NULL,
    report_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bootstrap_colony ON bootstrap_runs(colony_id, completed_at DESC);
"""


class _BootstrapDB:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(_BOOTSTRAP_DB_SCHEMA)
            self._conn.commit()

    def save_report(self, report: BootstrapReport, colony_version: str = "") -> None:
        import uuid
        run_id = str(uuid.uuid4())
        report_dict = report.to_dict()
        if colony_version:
            report_dict["colony_version"] = colony_version
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO bootstrap_runs
                    (run_id, colony_id, mode, started_at, completed_at, success,
                     corpus_version, report_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    report.colony_id,
                    report.mode,
                    report.started_at,
                    report.completed_at,
                    1 if report.success else 0,
                    report.corpus_version,
                    json.dumps(report_dict),
                ),
            )
            self._conn.commit()

    def last_colony_version(self, colony_id: str) -> Optional[str]:
        """Return the colony_version stored in the most recent bootstrap run."""
        last = self.last_run(colony_id)
        if last is None:
            return None
        try:
            data = json.loads(last.get("report_json", "{}"))
            return data.get("colony_version") or None
        except Exception:
            return None

    def last_run(self, colony_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bootstrap_runs WHERE colony_id = ? ORDER BY completed_at DESC LIMIT 1",
                (colony_id,),
            ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._conn.execute("PRAGMA table_info(bootstrap_runs)").fetchall()]
        return dict(zip(cols, row))


# ── IdentityBootstrap ─────────────────────────────────────────────────────────

class IdentityBootstrap:
    """Orchestrates the Colony Identity Bootstrap process."""

    def __init__(
        self,
        colony_graph: Optional[Any] = None,
        chain_manager: Optional[Any] = None,
        queue_manager: Optional[Any] = None,
        skill_registry: Optional[Any] = None,
        metrics_collector: Optional[Any] = None,
    ) -> None:
        self._graph = colony_graph
        self._chain_manager = chain_manager
        self._queue_manager = queue_manager
        self._skill_registry = skill_registry
        self._metrics = metrics_collector

        colony_home = os.environ.get(
            "COLONY_HOME",
            os.path.join(os.path.expanduser("~"), ".colony"),
        )
        db_path = os.path.join(colony_home, "bootstrap.db")
        try:
            self._db = _BootstrapDB(db_path)
        except Exception as exc:
            logger.warning("IdentityBootstrap: could not open bootstrap.db: %s", exc)
            self._db = None

    def _build_corpus(self):
        builder = IdentityBootstrapBuilder(chain_manager=self._chain_manager)
        return builder.build()

    async def _determine_mode(self, corpus) -> str:
        """Determine FIRST_BOOT, REGEN, or VERIFY."""
        colony_id = corpus.colony_id

        # Check bootstrap DB first
        if self._db is not None:
            try:
                last = self._db.last_run(colony_id)
                if last is not None:
                    # Fix 6: version guard — only run full bootstrap if colony_version changed
                    last_colony_ver = self._db.last_colony_version(colony_id)
                    if last_colony_ver and last_colony_ver == corpus.colony_version:
                        logger.debug(
                            "_determine_mode: colony_version=%s unchanged → VERIFY",
                            corpus.colony_version,
                        )
                        return "VERIFY"

                    completed_at = last.get("completed_at", "")
                    if completed_at:
                        try:
                            last_dt = datetime.fromisoformat(completed_at.rstrip("Z"))
                            if last_dt.tzinfo is None:
                                last_dt = last_dt.replace(tzinfo=timezone.utc)
                            age_days = (datetime.now(timezone.utc) - last_dt).days
                            if age_days >= _REGEN_THRESHOLD_DAYS:
                                return "REGEN"
                            return "VERIFY"
                        except ValueError:
                            pass
            except Exception as exc:
                logger.debug("_determine_mode: db error: %s", exc)

        # Fallback: check world model
        try:
            import colony.api.routers.world as wm_mod
            backend = getattr(wm_mod, "_wm_backend", None)
            if backend is not None:
                entity = await backend.get_entity(f"colony-self-{colony_id}")
                if entity is not None:
                    # Check age
                    created = getattr(entity, "created_at", None)
                    if created is not None:
                        if isinstance(created, str):
                            try:
                                created = datetime.fromisoformat(created.rstrip("Z"))
                            except ValueError:
                                created = None
                        if created is not None:
                            if isinstance(created, datetime) and created.tzinfo is None:
                                created = created.replace(tzinfo=timezone.utc)
                            age_days = (datetime.now(timezone.utc) - created).days
                            if age_days >= _REGEN_THRESHOLD_DAYS:
                                return "REGEN"
                    return "VERIFY"
        except Exception as exc:
            logger.debug("_determine_mode: wm check failed: %s", exc)

        return "FIRST_BOOT"

    async def _run_seeders(self, corpus) -> tuple[List[str], List[str]]:
        """Run all seeders and return (seeded_systems, failed_systems)."""
        from colony_sidecar.identity_bootstrap.seeders.world_model import WorldModelSeeder
        from colony_sidecar.identity_bootstrap.seeders.relationship import RelationshipSeeder
        from colony_sidecar.identity_bootstrap.seeders.memory import MemorySeeder
        from colony_sidecar.identity_bootstrap.seeders.chain import ChainSeeder
        from colony_sidecar.identity_bootstrap.seeders.goals import GoalsSeeder
        from colony_sidecar.identity_bootstrap.seeders.briefings import BriefingsSeeder
        from colony_sidecar.identity_bootstrap.seeders.sessions import SessionsSeeder
        from colony_sidecar.identity_bootstrap.seeders.task_queue import TaskQueueSeeder
        from colony_sidecar.identity_bootstrap.seeders.neo4j_cognition import Neo4jCognitionSeeder
        from colony_sidecar.identity_bootstrap.seeders.skills import SkillsSeeder

        seeded: List[str] = []
        failed: List[str] = []

        async def _run_one(seeder) -> None:
            try:
                await seeder.seed(corpus)
                seeded.append(seeder.name)
            except Exception as exc:
                logger.warning("seeder %s raised: %s", seeder.name, exc)
                failed.append(seeder.name)

        # Group 1: independent seeders
        group1 = [
            WorldModelSeeder(),
            RelationshipSeeder(),
            MemorySeeder(colony_graph=self._graph),
            ChainSeeder(chain_manager=self._chain_manager),
            GoalsSeeder(),
            BriefingsSeeder(),
            SessionsSeeder(),
        ]
        await asyncio.gather(*[_run_one(s) for s in group1])

        # Group 2: Neo4j — runs after world_model (world model data must exist first)
        neo4j_seeder = Neo4jCognitionSeeder(
            colony_graph=self._graph,
            metrics_collector=self._metrics,
        )
        await _run_one(neo4j_seeder)

        # Group 3: task_queue and skills — independent of group 1 results
        group3 = [
            TaskQueueSeeder(queue_manager=self._queue_manager),
            SkillsSeeder(skill_registry=self._skill_registry),
        ]
        await asyncio.gather(*[_run_one(s) for s in group3])

        return seeded, failed

    async def run(self) -> BootstrapReport:
        """Full bootstrap: determine mode, seed if needed, verify, persist."""
        started_at = datetime.now(timezone.utc).isoformat()
        corpus = self._build_corpus()
        mode = await self._determine_mode(corpus)

        logger.info(
            "IdentityBootstrap starting (mode=%s, colony=%s, version=%s)",
            mode,
            corpus.colony_id,
            corpus.colony_version,
        )

        seeded_systems: List[str] = []
        failed_systems: List[str] = []

        if mode in ("FIRST_BOOT", "REGEN"):
            seeded_systems, failed_systems = await self._run_seeders(corpus)

        # Always run verifier
        verifier = BootstrapVerifier(corpus, colony_graph=self._graph)
        anomalies = await verifier.run_all()

        verified_systems = [
            s for s in [
                "world_model", "contacts", "memory", "goals", "briefings",
                "sessions", "corpus", "chain",
            ]
            if not any(a.system == s and a.severity == "CRITICAL" for a in anomalies)
        ]

        # Self-reflection
        reflector = SelfReflectionComponent(metrics_collector=self._metrics)
        await reflector.reflect(corpus, anomalies)

        critical_count = sum(1 for a in anomalies if a.severity == "CRITICAL")
        success = critical_count == 0

        completed_at = datetime.now(timezone.utc).isoformat()

        report = BootstrapReport(
            colony_id=corpus.colony_id,
            mode=mode,
            started_at=started_at,
            completed_at=completed_at,
            seeded_systems=seeded_systems,
            verified_systems=verified_systems,
            failed_systems=failed_systems,
            anomalies=anomalies,
            corpus_version=corpus.corpus_version,
            success=success,
        )

        # Only persist a new run record when something actually changed (mode != VERIFY).
        # VERIFY mode with unchanged corpus_version generates no new graph nodes —
        # saving 740 runs per 4 days is pure noise.
        if self._db is not None and mode != "VERIFY":
            try:
                self._db.save_report(report, colony_version=corpus.colony_version)
            except Exception as exc:
                logger.warning("IdentityBootstrap: failed to persist report: %s", exc)

        logger.info(
            "IdentityBootstrap complete (mode=%s, success=%s, checks=%d/%d passed)",
            mode,
            success,
            18 - len(anomalies),
            18,
        )
        return report

    async def verify_only(self) -> BootstrapReport:
        """VERIFY mode only — no seeding."""
        started_at = datetime.now(timezone.utc).isoformat()
        corpus = self._build_corpus()

        verifier = BootstrapVerifier(corpus, colony_graph=self._graph)
        anomalies = await verifier.run_all()

        verified_systems = [
            s for s in [
                "world_model", "contacts", "memory", "goals", "briefings",
                "sessions", "corpus", "chain",
            ]
            if not any(a.system == s and a.severity == "CRITICAL" for a in anomalies)
        ]

        critical_count = sum(1 for a in anomalies if a.severity == "CRITICAL")
        success = critical_count == 0
        completed_at = datetime.now(timezone.utc).isoformat()

        return BootstrapReport(
            colony_id=corpus.colony_id,
            mode="VERIFY",
            started_at=started_at,
            completed_at=completed_at,
            seeded_systems=[],
            verified_systems=verified_systems,
            failed_systems=[],
            anomalies=anomalies,
            corpus_version=corpus.corpus_version,
            success=success,
        )
