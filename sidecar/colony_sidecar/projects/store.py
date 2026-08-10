"""ProjectStore -- SQLite persistence for projects + steps (survives restarts)."""

from __future__ import annotations

import sqlite3
import threading
import time
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from colony_sidecar.projects.models import Project, Step


class ProjectStore:
    _AUTHORITY_BOUND_PROJECT_SOURCES = frozenset({
        "cognition_spine", "governed_action",
    })
    _AUTHORITY_BOUND_PROJECT_IMMUTABLE_FIELDS = (
        "title", "objective", "source", "concern_id",
        "source_event_refs", "thought_job_id", "thought_result_ref",
        "goal_proposal_id", "evidence_refs", "policy_decision_refs",
        "subject_person_id", "viewer_scope", "shareability",
        "capability_allowlist", "goal_fingerprint", "entity_ids",
    )
    _WORK_ORDER_PROJECT_AUTHORITY_FIELDS = (
        _AUTHORITY_BOUND_PROJECT_IMMUTABLE_FIELDS
    )

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT,
                    source TEXT, status TEXT, outcome TEXT DEFAULT 'pending',
                    entity_ids TEXT, reason TEXT,
                    replans INTEGER DEFAULT 0, next_review_at REAL,
                    concern_id TEXT, source_event_refs TEXT,
                    thought_job_id TEXT, thought_result_ref TEXT,
                    goal_proposal_id TEXT, evidence_refs TEXT,
                    policy_decision_refs TEXT, subject_person_id TEXT,
                    viewer_scope TEXT DEFAULT 'owner',
                    shareability TEXT DEFAULT 'owner_private',
                    capability_allowlist TEXT, goal_fingerprint TEXT,
                    created_at REAL, updated_at REAL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    ordinal INTEGER, description TEXT, action_kind TEXT,
                    depends_on TEXT, status TEXT, attempts INTEGER DEFAULT 0,
                    result TEXT, work_order_ref TEXT, work_order_digest TEXT,
                    work_order_issued_at REAL DEFAULT 0, result_ref TEXT,
                    boundary_subject TEXT,
                    confidence REAL DEFAULT 0.6,
                    created_at REAL, updated_at REAL
                )""")
            # Additive migrations only: old databases remain directly
            # readable and rollback can ignore the new columns/tables.
            project_cols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(projects)").fetchall()}
            for name, definition in (
                ("outcome", "TEXT DEFAULT 'pending'"),
                ("concern_id", "TEXT"),
                ("source_event_refs", "TEXT"),
                ("thought_job_id", "TEXT"),
                ("thought_result_ref", "TEXT"),
                ("goal_proposal_id", "TEXT"),
                ("evidence_refs", "TEXT"),
                ("policy_decision_refs", "TEXT"),
                ("subject_person_id", "TEXT"),
                ("viewer_scope", "TEXT DEFAULT 'owner'"),
                ("shareability", "TEXT DEFAULT 'owner_private'"),
                ("capability_allowlist", "TEXT"),
                ("goal_fingerprint", "TEXT"),
            ):
                if name not in project_cols:
                    self._conn.execute(
                        f"ALTER TABLE projects ADD COLUMN {name} {definition}")
            cols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(steps)").fetchall()}
            for name, definition in (
                ("confidence", "REAL DEFAULT 0.6"),
                ("work_order_ref", "TEXT"),
                ("work_order_digest", "TEXT"),
                ("work_order_issued_at", "REAL DEFAULT 0"),
                ("result_ref", "TEXT"),
            ):
                if name not in cols:
                    self._conn.execute(
                        f"ALTER TABLE steps ADD COLUMN {name} {definition}")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS project_work_orders (
                    work_order_ref TEXT PRIMARY KEY,
                    work_order_id TEXT NOT NULL UNIQUE,
                    work_order_digest TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS project_execution_results (
                    result_ref TEXT PRIMARY KEY,
                    work_order_ref TEXT NOT NULL UNIQUE,
                    work_order_id TEXT NOT NULL UNIQUE,
                    work_order_digest TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    terminal_outcome TEXT NOT NULL,
                    verification_result TEXT NOT NULL,
                    effect_class TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS project_execution_attempts (
                    attempt_ref TEXT PRIMARY KEY,
                    work_order_ref TEXT NOT NULL,
                    work_order_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    transport_status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(work_order_id, run_id),
                    UNIQUE(work_order_id, attempt_number)
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS project_event_outbox (
                    event_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    journal_seq INTEGER,
                    journal_event_id TEXT,
                    journal_recorded_at TEXT,
                    projection_receipt_digest TEXT,
                    journal_acknowledged INTEGER NOT NULL DEFAULT 0,
                    projection_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK (state IN ('pending','projected'))
                )""")
            outbox_cols = {r[1] for r in self._conn.execute(
                "PRAGMA table_info(project_event_outbox)"
            ).fetchall()}
            if "journal_acknowledged" not in outbox_cols:
                self._conn.execute(
                    "ALTER TABLE project_event_outbox ADD COLUMN "
                    "journal_acknowledged INTEGER NOT NULL DEFAULT 0"
                )
            if "projection_receipt_digest" not in outbox_cols:
                self._conn.execute(
                    "ALTER TABLE project_event_outbox ADD COLUMN "
                    "projection_receipt_digest TEXT"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_project_event_outbox_state "
                "ON project_event_outbox(state, created_at, event_key)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_project ON steps(project_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_goal_fingerprint "
                "ON projects(goal_fingerprint)")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _json(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)

    @staticmethod
    def _digest(payload: Dict[str, Any]) -> str:
        return hashlib.sha256(
            ProjectStore._json(payload).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def project_event_envelope_digest(
        *, event_key: str, event_type: str, occurred_at: str,
        payload: Dict[str, Any],
    ) -> str:
        """Bind payload and all journal-routing envelope fields together."""

        return ProjectStore._digest({
            "schema": "ProjectEventEnvelopeV1",
            "version": 1,
            "event_key": str(event_key),
            "event_type": str(event_type),
            "occurred_at": str(occurred_at),
            "payload": payload,
        })

    @staticmethod
    def project_event_projection_receipt_digest(
        *,
        event_key: str,
        event_type: str,
        event_digest: str,
        occurred_at: str,
        journal_seq: int,
        journal_event_id: str,
        journal_recorded_at: str,
    ) -> str:
        """Bind a staged envelope to the exact journal projection receipt."""

        key = str(event_key or "").strip()
        kind = str(event_type or "").strip()
        envelope_digest = str(event_digest or "").strip()
        occurred = str(occurred_at or "").strip()
        event_id = str(journal_event_id or "").strip()
        recorded = str(journal_recorded_at or "").strip()
        if not key or len(key) > 256 or not kind or len(kind) > 128:
            raise ValueError("journal projection envelope identity is invalid")
        if len(envelope_digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in envelope_digest
        ):
            raise ValueError("journal projection envelope digest is invalid")
        if type(journal_seq) is not int or journal_seq < 1:
            raise ValueError("journal projection sequence is invalid")
        if (
            not event_id
            or len(event_id) > 256
            or any(character.isspace() for character in event_id)
        ):
            raise ValueError("journal projection event ID is invalid")
        try:
            occurred_time = datetime.fromisoformat(
                occurred.replace("Z", "+00:00")
            )
            recorded_time = datetime.fromisoformat(
                recorded.replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("journal projection timestamps are invalid") from exc
        if occurred_time.tzinfo is None or recorded_time.tzinfo is None:
            raise ValueError("journal projection timestamps require a timezone")
        return ProjectStore._digest({
            "schema": "ProjectEventProjectionReceiptV1",
            "version": 1,
            "event_key": key,
            "event_type": kind,
            "event_digest": envelope_digest,
            "occurred_at": occurred,
            "journal_seq": journal_seq,
            "journal_event_id": event_id,
            "journal_recorded_at": recorded,
        })

    @staticmethod
    def _cognition_mode_at_stage() -> str:
        """Capture the server-controlled evidence mode in the staged record.

        The reducer may consume the journal much later under a different
        runtime mode.  Binding the mode into the immutable outbox payload
        prevents outcomes produced while the legacy writer was authoritative
        from becoming new receipt-derived learning after a live cutover.
        """

        value = os.environ.get(
            "COLONY_COGNITION_EVIDENCE", "off",
        ).strip().lower()
        return value if value in {"off", "shadow", "live"} else "off"

    def _cognition_mode_for_event_locked(
        self, event_key: str,
    ) -> Optional[str]:
        """Return the first-stage mode, never the mode of a later replay."""

        row = self._conn.execute(
            "SELECT payload_json FROM project_event_outbox WHERE event_key=?",
            (str(event_key),),
        ).fetchone()
        if row is None:
            return self._cognition_mode_at_stage()
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        mode = str(payload.get("cognition_mode_at_stage") or "").lower()
        return mode if mode in {"off", "shadow", "live"} else None

    def _stage_event_locked(
        self,
        *,
        event_key: str,
        event_type: str,
        payload: Dict[str, Any],
        occurred_at: str,
    ) -> None:
        """Stage one immutable journal projection in the owning transaction."""

        key = str(event_key or "").strip()
        kind = str(event_type or "").strip().lower()
        when = str(occurred_at or "").strip()
        if not key or len(key) > 256:
            raise ValueError("project event key is invalid")
        if not kind or len(kind) > 128:
            raise ValueError("project event type is invalid")
        try:
            parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("project event occurred_at is invalid") from exc
        if parsed.tzinfo is None:
            raise ValueError("project event occurred_at requires a timezone")
        encoded = self._json(payload)
        existing = self._conn.execute(
            "SELECT * FROM project_event_outbox WHERE event_key=?", (key,),
        ).fetchone()
        if existing is not None:
            digest = self.project_event_envelope_digest(
                event_key=key,
                event_type=kind,
                occurred_at=str(existing["occurred_at"]),
                payload=payload,
            )
            if (
                existing["event_type"] != kind
                or existing["event_digest"] != digest
                or existing["payload_json"] != encoded
            ):
                raise ValueError("immutable project event replay mismatch")
            return
        digest = self.project_event_envelope_digest(
            event_key=key,
            event_type=kind,
            occurred_at=when,
            payload=payload,
        )
        stamp = time.time()
        self._conn.execute(
            """INSERT INTO project_event_outbox (
                   event_key,event_type,event_digest,payload_json,occurred_at,
                   state,created_at,updated_at
               ) VALUES (?,?,?,?,?,'pending',?,?)""",
            (key, kind, digest, encoded, when, stamp, stamp),
        )

    def _stage_project_terminal_locked(self, project: Project) -> None:
        if project.status not in {"completed", "abandoned"}:
            return
        identity = self._digest({
            "project_id": project.id,
            "lifecycle_status": project.status,
            "outcome": project.outcome,
        })
        event_key = f"project-terminal:{identity}"
        mode_at_stage = self._cognition_mode_for_event_locked(event_key)
        rows = self._conn.execute(
            "SELECT id,status,result_ref FROM steps WHERE project_id=? "
            "ORDER BY ordinal ASC,id ASC",
            (project.id,),
        ).fetchall()
        results: list[Dict[str, Any]] = []
        verified_success = bool(rows) and project.outcome == "succeeded"
        for row in rows:
            result_ref = str(row["result_ref"] or "")
            if row["status"] != "done" or not result_ref:
                verified_success = False
                continue
            result_row = self._conn.execute(
                "SELECT * FROM project_execution_results WHERE result_ref=?",
                (result_ref,),
            ).fetchone()
            if result_row is None:
                verified_success = False
                continue
            result_payload = json.loads(result_row["payload_json"])
            work_order_row = self._conn.execute(
                "SELECT * FROM project_work_orders WHERE work_order_id=?",
                (result_row["work_order_id"],),
            ).fetchone()
            receipt_refs = result_payload.get("receipt_refs") or []
            if (
                work_order_row is None
                or work_order_row["project_id"] != project.id
                or work_order_row["step_id"] != row["id"]
                or work_order_row["work_order_digest"]
                != result_row["work_order_digest"]
                or result_payload.get("work_order_id")
                != result_row["work_order_id"]
                or result_payload.get("work_order_digest")
                != result_row["work_order_digest"]
                or
                result_row["terminal_outcome"] != "succeeded"
                or result_row["verification_result"] != "verified"
                or not isinstance(receipt_refs, list)
                or not receipt_refs
            ):
                verified_success = False
            results.append({
                "step_id": str(row["id"]),
                "result_ref": result_ref,
                "result_digest": self._digest(result_payload),
            })
        material = {
            "schema": "ProjectTerminalEvidenceV2",
            "version": 2,
            "project_id": project.id,
            "lifecycle_status": project.status,
            "outcome": project.outcome,
            "status": (
                "verified"
                if verified_success else
                "failed"
                if project.status == "abandoned" else
                "unverified"
            ),
            "reason_code": str(project.reason or "")[:128],
            "source": str(project.source or "")[:64],
            "subject_person_id": project.subject_person_id or "owner",
            "viewer_scope": project.viewer_scope or "owner",
            "shareability": project.shareability or "owner_private",
            "concern_id": str(project.concern_id or "")[:128],
            "goal_proposal_id": str(project.goal_proposal_id or "")[:256],
            "result_refs": results,
            "evidence_status": (
                "verified" if verified_success else "unverified"
            ),
        }
        if mode_at_stage is not None:
            material["cognition_mode_at_stage"] = mode_at_stage
        material["project_digest"] = self._digest(material)
        self._stage_event_locked(
            event_key=event_key,
            event_type=f"project.{project.status}",
            payload=material,
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )

    # -- projects ---------------------------------------------------------
    def insert_authority_bound_project(
        self, p: Project,
    ) -> tuple[Project, bool]:
        """Insert one immutable authority-bound Project, or replay it exactly.

        Unlike :meth:`save_project`, this operation never upserts.  A replay
        returns the persisted lifecycle row byte-for-byte as reconstructed by
        ``Project.from_row``; it cannot rewind status, rewrite timestamps, or
        widen any WorkOrder authority field.
        """

        if p.source not in self._AUTHORITY_BOUND_PROJECT_SOURCES:
            raise ValueError("exact project insert requires an authority-bound source")
        row = p.to_row()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_row = self._conn.execute(
                    "SELECT * FROM projects WHERE id=?", (p.id,),
                ).fetchone()
                if existing_row is not None:
                    existing = Project.from_row(dict(existing_row))
                    if any(
                        getattr(existing, field) != getattr(p, field)
                        for field in self._WORK_ORDER_PROJECT_AUTHORITY_FIELDS
                    ):
                        raise ValueError(
                            "immutable authority-bound project replay mismatch"
                        )
                    self._conn.commit()
                    return existing, False
                if (
                    p.status != "planning"
                    or p.outcome != "pending"
                    or p.reason
                    or p.replans != 0
                    or p.next_review_at != 0.0
                ):
                    raise ValueError(
                        "exact project insert requires an initial planning lifecycle"
                    )
                cols = ", ".join(row)
                placeholders = ", ".join(["?"] * len(row))
                self._conn.execute(
                    f"INSERT INTO projects ({cols}) VALUES ({placeholders})",
                    list(row.values()),
                )
                self._conn.commit()
                return Project.from_row(row), True
            except Exception:
                self._conn.rollback()
                raise

    def save_project(self, p: Project) -> Project:
        p.updated_at = time.time()
        row = p.to_row()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_row = self._conn.execute(
                    "SELECT * FROM projects WHERE id=?", (p.id,),
                ).fetchone()
                if existing_row is not None:
                    existing = Project.from_row(dict(existing_row))
                    bound_sources = self._AUTHORITY_BOUND_PROJECT_SOURCES
                    if existing.source in bound_sources or p.source in bound_sources:
                        if any(getattr(existing, field) != getattr(p, field)
                               for field in self._AUTHORITY_BOUND_PROJECT_IMMUTABLE_FIELDS):
                            if (
                                existing.source == "cognition_spine"
                                and p.source == "cognition_spine"
                            ):
                                raise ValueError(
                                    "immutable cognition project provenance mismatch"
                                )
                            raise ValueError(
                                "immutable authority-bound project provenance mismatch"
                            )
                cols = ", ".join(row); ph = ", ".join(["?"] * len(row))
                updates = ", ".join(
                    f"{k}=excluded.{k}" for k in row if k != "id"
                )
                self._conn.execute(
                    f"INSERT INTO projects ({cols}) VALUES ({ph}) "
                    f"ON CONFLICT(id) DO UPDATE SET {updates}", list(row.values()))
                self._stage_project_terminal_locked(p)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return p

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return Project.from_row(dict(r)) if r else None

    def list_projects(self, status: Optional[str] = None,
                      limit: int = 50, offset: int = 0) -> List[Project]:
        q = "SELECT * FROM projects"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend((limit, max(0, int(offset))))
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Project.from_row(dict(r)) for r in rows]

    def count(self, status: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) AS n FROM projects"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        with self._lock:
            r = self._conn.execute(q, params).fetchone()
        return int(r["n"])

    def find_by_goal_fingerprint(self, fingerprint: str) -> Optional[Project]:
        """Find a non-terminal project for one normalized autonomous goal."""

        if not str(fingerprint or "").strip():
            return None
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM projects WHERE goal_fingerprint=?
                   AND status NOT IN ('completed','abandoned')
                   ORDER BY created_at ASC LIMIT 1""",
                (str(fingerprint),),
            ).fetchone()
        return Project.from_row(dict(row)) if row else None

    def find_by_source_event_refs(
        self, references: List[str],
    ) -> Optional[Project]:
        """Find the first Project already bound to any exact source event.

        A transport-attested owner goal may create its Project before the
        ordinary cognition reducer reaches the same journal event.  Matching
        the immutable reference prevents that later model pass from creating a
        second Project with different wording and therefore a different goal
        fingerprint.
        """

        wanted = {
            str(reference) for reference in references
            if str(reference or "").strip()
        }
        if not wanted:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects ORDER BY created_at ASC,id ASC"
            ).fetchall()
        for row in rows:
            project = Project.from_row(dict(row))
            if wanted.intersection(project.source_event_refs):
                return project
        return None

    def due_for_review(self, now: Optional[float] = None,
                       limit: int = 20, offset: int = 0) -> List[Project]:
        """Active projects whose next_review_at has passed."""
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects WHERE status='active' AND "
                "coalesce(next_review_at, 0) <= ? "
                "ORDER BY next_review_at ASC LIMIT ? OFFSET ?",
                (now, limit, max(0, int(offset)))).fetchall()
        return [Project.from_row(dict(r)) for r in rows]

    # -- steps --------------------------------------------------------------
    def save_step(self, s: Step) -> Step:
        s.updated_at = time.time()
        row = s.to_row()
        with self._lock:
            cols = ", ".join(row); ph = ", ".join(["?"] * len(row))
            updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
            self._conn.execute(
                f"INSERT INTO steps ({cols}) VALUES ({ph}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", list(row.values()))
            self._conn.commit()
        return s

    def steps_for(self, project_id: str) -> List[Step]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE project_id=? "
                "ORDER BY ordinal ASC,id ASC",
                (project_id,)).fetchall()
        return [Step.from_row(dict(r)) for r in rows]

    def delete_steps(self, project_id: str,
                     statuses: Optional[List[str]] = None) -> int:
        """Delete steps of a project (optionally only certain statuses)."""
        q = "DELETE FROM steps WHERE project_id=?"
        params: List[Any] = [project_id]
        if statuses:
            q += f" AND status IN ({','.join(['?'] * len(statuses))})"
            params.extend(statuses)
        with self._lock:
            cur = self._conn.execute(q, params)
            self._conn.commit()
        return cur.rowcount

    # -- immutable WorkOrder / execution-result ledger --------------------
    def prepare_work_order(self, project: Project, step: Step, order: Any) -> str:
        """Insert missing parents and bind a WorkOrder without stale upserts.

        Queue polling may hold an older in-memory Project/Step.  This method
        never overwrites an existing lifecycle row: it validates authority
        fields and terminal state, creates the immutable WorkOrder, and binds
        its reference to the step in one SQLite transaction.
        """

        order.validate()
        if step.project_id != project.id or (
            order.project_id != project.id or order.step_id != step.id
        ):
            raise ValueError("WorkOrder parent identity mismatch")
        if project.status in {"completed", "abandoned"} or step.status in {
            "done", "failed", "skipped",
        }:
            raise ValueError("cannot prepare work for a terminal project step")
        ref = f"work-order:{order.work_order_id}"
        payload_json = self._json(order.payload())
        project_row = project.to_row()
        step_row = step.to_row()
        issued_at = datetime.fromisoformat(
            str(order.issued_at).replace("Z", "+00:00")
        ).timestamp()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_project_row = self._conn.execute(
                    "SELECT * FROM projects WHERE id=?", (project.id,),
                ).fetchone()
                if existing_project_row is None:
                    cols = ", ".join(project_row)
                    ph = ", ".join(["?"] * len(project_row))
                    self._conn.execute(
                        f"INSERT INTO projects ({cols}) VALUES ({ph})",
                        list(project_row.values()),
                    )
                else:
                    existing_project = Project.from_row(
                        dict(existing_project_row)
                    )
                    if existing_project.status in {"completed", "abandoned"}:
                        raise ValueError("persisted WorkOrder project is terminal")
                    if any(
                        getattr(existing_project, field)
                        != getattr(project, field)
                        for field in self._WORK_ORDER_PROJECT_AUTHORITY_FIELDS
                    ) or (
                        existing_project.status != project.status
                        or existing_project.outcome != project.outcome
                    ):
                        raise ValueError(
                            "stale WorkOrder project differs from persisted parent"
                        )

                existing_step_row = self._conn.execute(
                    "SELECT * FROM steps WHERE id=?", (step.id,),
                ).fetchone()
                if existing_step_row is None:
                    cols = ", ".join(step_row)
                    ph = ", ".join(["?"] * len(step_row))
                    self._conn.execute(
                        f"INSERT INTO steps ({cols}) VALUES ({ph})",
                        list(step_row.values()),
                    )
                    existing_step = step
                else:
                    existing_step = Step.from_row(dict(existing_step_row))
                    if existing_step.status in {"done", "failed", "skipped"}:
                        raise ValueError("persisted WorkOrder step is terminal")
                    step_authority = (
                        "project_id", "ordinal", "description", "action_kind",
                        "depends_on", "boundary_subject", "confidence",
                    )
                    if any(
                        getattr(existing_step, field) != getattr(step, field)
                        for field in step_authority
                    ) or existing_step.status != step.status:
                        raise ValueError(
                            "stale WorkOrder step differs from persisted parent"
                        )
                    stored_issued = float(
                        existing_step.work_order_issued_at or 0.0
                    )
                    if stored_issued and abs(stored_issued - issued_at) > 0.001:
                        raise ValueError("persisted WorkOrder issue time mismatch")

                existing_order = self._conn.execute(
                    "SELECT * FROM project_work_orders WHERE work_order_id=?",
                    (order.work_order_id,),
                ).fetchone()
                if existing_order is not None:
                    row = dict(existing_order)
                    if (
                        row["work_order_digest"] != order.work_order_digest
                        or row["payload_json"] != payload_json
                        or row["work_order_ref"] != ref
                    ):
                        raise ValueError("immutable WorkOrder replay mismatch")
                else:
                    self._conn.execute(
                        """INSERT INTO project_work_orders (
                            work_order_ref, work_order_id, work_order_digest,
                            project_id, step_id, schema_version, payload_json,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            ref, order.work_order_id, order.work_order_digest,
                            order.project_id, order.step_id, int(order.version),
                            payload_json, time.time(),
                        ),
                    )

                if (
                    existing_step.work_order_ref
                    and existing_step.work_order_ref != ref
                ) or (
                    existing_step.work_order_digest
                    and existing_step.work_order_digest
                    != order.work_order_digest
                ):
                    raise ValueError("step WorkOrder authority replay mismatch")
                changed = self._conn.execute(
                    """UPDATE steps SET work_order_ref=?,work_order_digest=?,
                              work_order_issued_at=?,updated_at=?
                       WHERE id=? AND status NOT IN ('done','failed','skipped')""",
                    (
                        ref, order.work_order_digest, issued_at, time.time(),
                        step.id,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("persisted WorkOrder step became terminal")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return ref

    def save_work_order(self, order: Any) -> str:
        """Persist one immutable authority envelope and return its ledger ref."""

        order.validate()
        ref = f"work-order:{order.work_order_id}"
        payload_json = self._json(order.payload())
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM project_work_orders WHERE work_order_id=?",
                (order.work_order_id,),
            ).fetchone()
            if existing:
                row = dict(existing)
                if (
                    row["work_order_digest"] != order.work_order_digest
                    or row["payload_json"] != payload_json
                    or row["work_order_ref"] != ref
                ):
                    raise ValueError("immutable WorkOrder replay mismatch")
                return ref
            self._conn.execute(
                """INSERT INTO project_work_orders (
                    work_order_ref, work_order_id, work_order_digest,
                    project_id, step_id, schema_version, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ref, order.work_order_id, order.work_order_digest,
                    order.project_id, order.step_id, int(order.version),
                    payload_json, time.time(),
                ),
            )
            self._conn.commit()
        return ref

    def get_work_order(self, ref_or_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM project_work_orders
                   WHERE work_order_ref=? OR work_order_id=?""",
                (ref_or_id, ref_or_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def work_orders_awaiting_reconciliation(
        self, *, limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """Return bounded, non-terminal WorkOrders that may need polling.

        Queue execution and Project pursuit are separate durable lifecycles.
        A Project can become blocked (or ordinary pursuit can be cancelled)
        after its WorkOrder has already reached a terminal queue state.  Such
        a result is still an observed fact and must be projected into the
        execution ledger independently of whether the Project is currently
        eligible to advance.

        This query deliberately includes blocked Projects, but excludes
        terminal Project/Step parents because ``save_execution_result`` must
        never reopen those lifecycles.  Rows without a result are ordered
        first so idempotent re-polls cannot starve fresh terminal outcomes.
        """

        bound = max(1, min(100, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                """SELECT wo.*
                     FROM project_work_orders wo
                     JOIN projects p ON p.id=wo.project_id
                     JOIN steps s ON s.id=wo.step_id
                                 AND s.project_id=wo.project_id
                    WHERE p.status NOT IN ('completed','abandoned')
                      AND s.status NOT IN ('done','failed','skipped')
                    ORDER BY CASE WHEN coalesce(s.result_ref, '') = ''
                                      THEN 0 ELSE 1 END,
                             wo.created_at ASC,wo.work_order_id ASC
                    LIMIT ?""",
                (bound,),
            ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_payload = item.pop("payload_json")
            try:
                item["payload"] = json.loads(raw_payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                # Keep malformed authority addressable so one corrupt row is
                # reported without starving valid rows later in the batch.
                item["payload"] = None
            results.append(item)
        return results

    def save_execution_result(
        self,
        order: Any,
        result: Any,
        *,
        transport_status: str,
    ) -> str:
        """Append a run attempt and update one stable logical result row.

        Duplicate delivery of the same run is idempotent.  Reusing a run ID
        or attempt number for different content is rejected.  Older attempts
        can never overwrite the latest logical result.
        """

        order.validate()
        from colony_sidecar.execution_results import (
            EFFECT_CLASSES,
            EXECUTION_RESULT_VERSION,
            TERMINAL_OUTCOMES,
            VERIFICATION_RESULTS,
            bounded_refs,
        )

        if (
            result.schema != "ExecutionResultV1"
            or type(result.version) is not int
            or result.version != EXECUTION_RESULT_VERSION
        ):
            raise ValueError("execution result schema/version is invalid")
        if result.work_order_id != order.work_order_id:
            raise ValueError("execution result WorkOrder ID mismatch")
        if result.work_order_digest != order.work_order_digest:
            raise ValueError("execution result WorkOrder digest mismatch")
        if int(result.work_order_version) != int(order.version):
            raise ValueError("execution result WorkOrder version mismatch")
        if result.result_ref != f"execution-result:{order.work_order_id}":
            raise ValueError("execution result reference mismatch")
        if (
            type(result.attempt_number) is not int
            or result.attempt_number < 1
            or result.attempt_number > int(order.max_attempts)
        ):
            raise ValueError("execution attempt exceeds WorkOrder budget")
        if result.terminal_outcome not in TERMINAL_OUTCOMES:
            raise ValueError("execution result terminal outcome is invalid")
        if (
            result.effect_class not in EFFECT_CLASSES
            or result.effect_class != order.effect_class
        ):
            raise ValueError("execution result effect class mismatch")
        if result.verification_result not in VERIFICATION_RESULTS:
            raise ValueError("execution result verification state is invalid")
        receipts = bounded_refs(result.receipt_refs)
        if receipts != tuple(result.receipt_refs):
            raise ValueError("execution result receipt refs are not canonical")
        if result.terminal_outcome == "succeeded":
            if result.verification_result == "not_applicable":
                raise ValueError("succeeded execution requires verification state")
            if result.verification_result == "verified" and (
                not receipts
                or not str(result.verifier_identity or "").strip()
                or result.verifier_identity == result.executor_identity
            ):
                raise ValueError(
                    "verified execution requires independent receipt evidence"
                )
        elif result.verification_result != "not_applicable":
            raise ValueError(
                "non-success execution cannot claim success verification"
            )
        for value, field, maximum in (
            (result.run_id, "run_id", 256),
            (result.executor_identity, "executor_identity", 256),
            (result.verifier_identity, "verifier_identity", 256),
            (result.summary, "summary", 4000),
            (result.error, "error", 2000),
        ):
            text = str(value or "").strip()
            if len(text) > maximum or (
                field in {"run_id", "executor_identity"} and not text
            ):
                raise ValueError(f"execution result {field} is invalid")
        try:
            started_at = datetime.fromisoformat(
                str(result.started_at).replace("Z", "+00:00")
            )
            ended_at = datetime.fromisoformat(
                str(result.ended_at).replace("Z", "+00:00")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("execution result timestamps are invalid") from exc
        if (
            started_at.tzinfo is None
            or ended_at.tzinfo is None
            or ended_at < started_at
        ):
            raise ValueError("execution result timestamps are invalid")
        transport = str(transport_status or "").strip().lower()
        if transport not in {"completed", "neutral", "failed", "cancelled"}:
            raise ValueError("execution result transport status is invalid")
        if transport == "failed" and result.terminal_outcome != "failed":
            raise ValueError("execution result conflicts with failed transport")
        if transport == "cancelled" and result.terminal_outcome != "cancelled":
            raise ValueError("execution result conflicts with cancelled transport")

        work_order_ref = f"work-order:{order.work_order_id}"
        payload_json = self._json(result.payload())
        attempt_ref = "execution-attempt:%s" % hashlib.sha256(
            f"{order.work_order_id}\0{result.run_id}".encode("utf-8")
        ).hexdigest()[:24]
        now = time.time()

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                work_order = self._conn.execute(
                    """SELECT wo.work_order_digest,wo.project_id,wo.step_id,
                              p.status AS project_status,
                              s.status AS step_status
                       FROM project_work_orders wo
                       JOIN projects p ON p.id=wo.project_id
                       JOIN steps s ON s.id=wo.step_id
                                    AND s.project_id=wo.project_id
                       WHERE wo.work_order_ref=?""",
                    (work_order_ref,),
                ).fetchone()
                if (
                    not work_order
                    or work_order["work_order_digest"] != order.work_order_digest
                    or work_order["project_id"] != order.project_id
                    or work_order["step_id"] != order.step_id
                ):
                    raise ValueError("execution result has no matching WorkOrder ledger row")

                existing_attempt = self._conn.execute(
                    """SELECT * FROM project_execution_attempts
                       WHERE work_order_id=? AND (run_id=? OR attempt_number=?)""",
                    (order.work_order_id, result.run_id, int(result.attempt_number)),
                ).fetchone()
                if existing_attempt:
                    row = dict(existing_attempt)
                    if (
                        row["run_id"] != result.run_id
                        or int(row["attempt_number"]) != int(result.attempt_number)
                        or row["transport_status"] != transport
                        or row["result_json"] != payload_json
                    ):
                        raise ValueError("execution attempt replay mismatch")
                else:
                    if (
                        work_order["project_status"] in {
                            "completed", "abandoned",
                        }
                        or work_order["step_status"] in {
                            "done", "failed", "skipped",
                        }
                    ):
                        raise ValueError(
                            "new execution attempt cannot mutate a terminal "
                            "project step"
                        )
                    self._conn.execute(
                        """INSERT INTO project_execution_attempts (
                            attempt_ref, work_order_ref, work_order_id, run_id,
                            attempt_number, transport_status, result_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            attempt_ref, work_order_ref, order.work_order_id,
                            result.run_id, int(result.attempt_number),
                            transport, payload_json, now,
                        ),
                    )

                logical = self._conn.execute(
                    "SELECT * FROM project_execution_results WHERE result_ref=?",
                    (result.result_ref,),
                ).fetchone()
                if logical is None:
                    self._conn.execute(
                        """INSERT INTO project_execution_results (
                            result_ref, work_order_ref, work_order_id,
                            work_order_digest, run_id, attempt_number,
                            terminal_outcome, verification_result, effect_class,
                            payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            result.result_ref, work_order_ref, order.work_order_id,
                            order.work_order_digest, result.run_id,
                            int(result.attempt_number), result.terminal_outcome,
                            result.verification_result, result.effect_class,
                            payload_json, now, now,
                        ),
                    )
                else:
                    current = dict(logical)
                    current_attempt = int(current["attempt_number"])
                    if int(result.attempt_number) == current_attempt:
                        if current["run_id"] != result.run_id or current["payload_json"] != payload_json:
                            raise ValueError("logical execution result replay mismatch")
                    elif int(result.attempt_number) > current_attempt:
                        self._conn.execute(
                            """UPDATE project_execution_results SET
                                run_id=?, attempt_number=?, terminal_outcome=?,
                                verification_result=?, effect_class=?,
                                payload_json=?, updated_at=? WHERE result_ref=?""",
                            (
                                result.run_id, int(result.attempt_number),
                                result.terminal_outcome, result.verification_result,
                                result.effect_class, payload_json, now,
                                result.result_ref,
                            ),
                        )
                self._conn.execute(
                    """UPDATE steps SET result_ref=?, work_order_ref=?,
                       work_order_digest=?, updated_at=? WHERE id=?""",
                    (
                        result.result_ref, work_order_ref,
                        order.work_order_digest, now, order.step_id,
                    ),
                )
                project_row = self._conn.execute(
                    "SELECT subject_person_id,viewer_scope,shareability "
                    "FROM projects WHERE id=?",
                    (order.project_id,),
                ).fetchone()
                result_payload = result.payload()
                status = (
                    "verified"
                    if result.terminal_outcome == "succeeded"
                    and result.verification_result == "verified"
                    and bool(result.receipt_refs)
                    else result.terminal_outcome
                    if result.terminal_outcome in {
                        "failed", "cancelled", "skipped",
                    }
                    else "unverified"
                )
                identity = self._digest({
                    "work_order_id": order.work_order_id,
                    "run_id": result.run_id,
                })
                event_key = f"project-execution:{identity}"
                mode_at_stage = self._cognition_mode_for_event_locked(event_key)
                evidence = {
                    "schema": "ProjectExecutionEvidenceV2",
                    "version": 2,
                    "project_id": order.project_id,
                    "step_id": order.step_id,
                    "work_order_id": order.work_order_id,
                    "work_order_digest": order.work_order_digest,
                    "result_ref": result.result_ref,
                    "run_id": result.run_id,
                    "attempt_number": int(result.attempt_number),
                    "terminal_outcome": result.terminal_outcome,
                    "verification_result": result.verification_result,
                    "effect_class": result.effect_class,
                    "transport_status": transport,
                    "status": status,
                    "result_digest": self._digest(result_payload),
                    "receipt_refs": list(result.receipt_refs),
                    "verifier_identity": str(result.verifier_identity or "")[:256],
                    "subject_person_id": (
                        str(project_row["subject_person_id"] or "owner")
                        if project_row is not None else "owner"
                    ),
                    "viewer_scope": (
                        str(project_row["viewer_scope"] or "owner")
                        if project_row is not None else "owner"
                    ),
                    "shareability": (
                        str(project_row["shareability"] or "owner_private")
                        if project_row is not None else "owner_private"
                    ),
                }
                if mode_at_stage is not None:
                    evidence["cognition_mode_at_stage"] = mode_at_stage
                evidence["evidence_digest"] = self._digest(evidence)
                self._stage_event_locked(
                    event_key=event_key,
                    event_type="work_order.result",
                    payload=evidence,
                    occurred_at=result.ended_at,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return result.result_ref

    def get_execution_result(self, ref_or_work_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM project_execution_results
                   WHERE result_ref=? OR work_order_id=?""",
                (ref_or_work_order_id, ref_or_work_order_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def execution_attempts_for(self, work_order_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM project_execution_attempts
                   WHERE work_order_id=? ORDER BY attempt_number ASC""",
                (work_order_id,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json"))
            results.append(item)
        return results

    # -- crash-recoverable host-journal outbox ---------------------------
    def pending_project_events(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        bound = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM project_event_outbox WHERE state='pending' "
                "ORDER BY created_at ASC,event_key ASC LIMIT ?",
                (bound,),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_payload = item.pop("payload_json")
            try:
                item["payload"] = json.loads(raw_payload)
                item["payload_error"] = ""
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                # Keep the row addressable so the projector can persist a
                # bounded last_error instead of crashing the entire drain.
                item["payload"] = None
                item["payload_error"] = f"invalid project outbox JSON:{exc}"
            events.append(item)
        return events

    def unacknowledged_project_events(
        self, *, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        bound = max(1, min(500, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM project_event_outbox WHERE state='projected' "
                "AND journal_acknowledged=0 "
                "ORDER BY updated_at ASC,event_key ASC LIMIT ?",
                (bound,),
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_payload = item.pop("payload_json")
            try:
                item["payload"] = json.loads(raw_payload)
                item["payload_error"] = ""
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                item["payload"] = None
                item["payload_error"] = f"invalid project outbox JSON:{exc}"
            events.append(item)
        return events

    def complete_project_event(
        self,
        event_key: str,
        journal_record: Dict[str, Any],
    ) -> Dict[str, Any]:
        key = str(event_key or "").strip()
        raw_seq = journal_record.get("seq")
        raw_event_id = journal_record.get("ulid")
        raw_recorded_at = journal_record.get("recordedAt")
        if (
            not key
            or type(raw_seq) is not int
            or type(raw_event_id) is not str
            or type(raw_recorded_at) is not str
        ):
            raise ValueError("journal projection receipt is invalid")
        seq = raw_seq
        event_id = raw_event_id.strip()
        recorded_at = raw_recorded_at.strip()
        if event_id != raw_event_id or recorded_at != raw_recorded_at:
            raise ValueError("journal projection receipt is not canonical")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM project_event_outbox WHERE event_key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    raise ValueError("project event outbox row is unavailable")
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("project event outbox payload is invalid") from exc
                if not isinstance(payload, dict):
                    raise ValueError("project event outbox payload is invalid")
                expected_envelope = self.project_event_envelope_digest(
                    event_key=str(row["event_key"]),
                    event_type=str(row["event_type"]),
                    occurred_at=str(row["occurred_at"]),
                    payload=payload,
                )
                if str(row["event_digest"] or "") != expected_envelope:
                    raise ValueError("project event outbox envelope digest mismatch")
                receipt_digest = self.project_event_projection_receipt_digest(
                    event_key=str(row["event_key"]),
                    event_type=str(row["event_type"]),
                    event_digest=expected_envelope,
                    occurred_at=str(row["occurred_at"]),
                    journal_seq=seq,
                    journal_event_id=event_id,
                    journal_recorded_at=recorded_at,
                )
                if row["state"] == "projected":
                    if (
                        int(row["journal_seq"] or 0) != seq
                        or str(row["journal_event_id"] or "") != event_id
                        or str(row["journal_recorded_at"] or "") != recorded_at
                        or str(row["projection_receipt_digest"] or "")
                        != receipt_digest
                    ):
                        raise ValueError("project event projection replay mismatch")
                else:
                    self._conn.execute(
                        """UPDATE project_event_outbox SET state='projected',
                           journal_seq=?,journal_event_id=?,journal_recorded_at=?,
                           projection_receipt_digest=?,
                           projection_attempts=projection_attempts+1,
                           last_error=NULL,updated_at=? WHERE event_key=?
                           AND state='pending'""",
                        (
                            seq, event_id, recorded_at, receipt_digest,
                            time.time(), key,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.project_event(key) or {}

    def acknowledge_project_event(self, event_key: str) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM project_event_outbox WHERE event_key=?",
                    (str(event_key),),
                ).fetchone()
                if row is None or row["state"] != "projected":
                    raise ValueError(
                        "project event acknowledgement target is unavailable"
                    )
                try:
                    payload = json.loads(str(row["payload_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("project event outbox payload is invalid") from exc
                if not isinstance(payload, dict):
                    raise ValueError("project event outbox payload is invalid")
                expected_envelope = self.project_event_envelope_digest(
                    event_key=str(row["event_key"]),
                    event_type=str(row["event_type"]),
                    occurred_at=str(row["occurred_at"]),
                    payload=payload,
                )
                if str(row["event_digest"] or "") != expected_envelope:
                    raise ValueError("project event outbox envelope digest mismatch")
                expected_receipt = self.project_event_projection_receipt_digest(
                    event_key=str(row["event_key"]),
                    event_type=str(row["event_type"]),
                    event_digest=expected_envelope,
                    occurred_at=str(row["occurred_at"]),
                    journal_seq=row["journal_seq"],
                    journal_event_id=str(row["journal_event_id"] or ""),
                    journal_recorded_at=str(row["journal_recorded_at"] or ""),
                )
                if (
                    str(row["projection_receipt_digest"] or "")
                    != expected_receipt
                ):
                    raise ValueError(
                        "project event projection receipt digest mismatch"
                    )
                changed = self._conn.execute(
                    """UPDATE project_event_outbox SET journal_acknowledged=1,
                       last_error=NULL,updated_at=? WHERE event_key=?
                       AND state='projected' AND projection_receipt_digest=?""",
                    (time.time(), str(event_key), expected_receipt),
                ).rowcount
                if changed != 1:
                    raise ValueError(
                        "project event acknowledgement target is unavailable"
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def fail_project_event(self, event_key: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE project_event_outbox SET
                   projection_attempts=projection_attempts+1,last_error=?,updated_at=?
                   WHERE event_key=?""",
                (str(error or "projection_failed")[:500], time.time(), event_key),
            )
            self._conn.commit()

    def project_event(self, event_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM project_event_outbox WHERE event_key=?",
                (str(event_key),),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        raw_payload = item.pop("payload_json")
        try:
            item["payload"] = json.loads(raw_payload)
            item["payload_error"] = ""
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            item["payload"] = None
            item["payload_error"] = f"invalid project outbox JSON:{exc}"
        return item

    def project_event_outbox_status(self) -> Dict[str, Any]:
        with self._lock:
            counts = self._conn.execute(
                "SELECT state,COUNT(*) AS n FROM project_event_outbox "
                "GROUP BY state"
            ).fetchall()
            error = self._conn.execute(
                "SELECT event_key,last_error FROM project_event_outbox "
                "WHERE last_error IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            acknowledgement_pending = int(self._conn.execute(
                "SELECT COUNT(*) AS n FROM project_event_outbox "
                "WHERE state='projected' AND journal_acknowledged=0"
            ).fetchone()["n"])
        return {
            "counts": {row["state"]: int(row["n"]) for row in counts},
            "acknowledgement_pending": acknowledgement_pending,
            "last_error": str(error["last_error"] or "") if error else "",
            "last_error_event_key": str(error["event_key"] or "") if error else "",
        }
