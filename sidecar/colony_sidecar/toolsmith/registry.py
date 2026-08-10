"""Persisted registry for self-built tools (Mind M1).

SQLite metadata plus an on-disk tool directory per tool (source, manifest,
test). Deliberately independent of the runtime `skills/` executor registry
(which serves initiative executors and is load-bearing elsewhere); this one
owns the LLM-callable-tool artifact lifecycle only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from colony_sidecar.toolsmith.authority import (
    GraduationAuthorityError,
    GraduationAuthorityV1,
)
from colony_sidecar.toolsmith.integrity import (
    PURE_CAPABILITY_MANIFEST,
    artifact_digest as compute_artifact_digest,
    digest_json,
)
from colony_sidecar.tools.definitions import STATIC_TOOL_NAMES

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")


class ToolStatus:
    DRAFT = "draft"        # written, not yet verified
    VERIFIED = "verified"  # passed sandbox replay, not yet advertised
    SHADOW = "shadow"      # advertised as shadow (simulated, journaled)
    LIVE = "live"          # advertised and executed for real
    RETIRED = "retired"    # demoted (unused or failing)
    REJECTED = "rejected"  # failed verification or owner-rejected

    ALL = (DRAFT, VERIFIED, SHADOW, LIVE, RETIRED, REJECTED)


@dataclass
class Tool:
    tool_id: str
    name: str
    description: str
    status: str
    source_code: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    checksum_sha256: str = ""
    candidate_digest: str = ""
    artifact_digest: str = ""
    capability_manifest: Dict[str, Any] = field(
        default_factory=lambda: dict(PURE_CAPABILITY_MANIFEST))
    origin_kind: str = "mined"          # mined | requested
    evidence: List[str] = field(default_factory=list)
    test_source: str = ""
    verify_detail: Dict[str, Any] = field(default_factory=dict)
    invocations: int = 0
    failures: int = 0
    shadow_runs: int = 0
    last_used_at: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("source_code", None)
        d.pop("test_source", None)
        return d


class ToolRegistry:
    def __init__(self, db_path: str, library_root: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._library = library_root
        os.makedirs(self._library, exist_ok=True)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tools (
                tool_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                source_code TEXT NOT NULL,
                input_schema TEXT,
                checksum_sha256 TEXT,
                candidate_digest TEXT,
                artifact_digest TEXT,
                capability_manifest TEXT,
                origin_kind TEXT,
                evidence TEXT,
                test_source TEXT,
                verify_detail TEXT,
                invocations INTEGER DEFAULT 0,
                failures INTEGER DEFAULT 0,
                shadow_runs INTEGER DEFAULT 0,
                last_used_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tools_status ON tools(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tools_name ON tools(name);
            CREATE TABLE IF NOT EXISTS toolsmith_shadow_comparisons (
                comparison_id TEXT PRIMARY KEY,
                tool_id TEXT NOT NULL,
                capture_id TEXT NOT NULL,
                capture_source TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                incumbent_output_digest TEXT NOT NULL,
                candidate_output_digest TEXT NOT NULL,
                repeat_output_digest TEXT NOT NULL,
                deterministic INTEGER NOT NULL,
                matched INTEGER NOT NULL,
                success INTEGER NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(tool_id, capture_id)
            );
            CREATE INDEX IF NOT EXISTS idx_toolsmith_shadow_tool
                ON toolsmith_shadow_comparisons(tool_id, created_at);
            CREATE TABLE IF NOT EXISTS toolsmith_graduations (
                authority_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE,
                tool_id TEXT NOT NULL,
                candidate_digest TEXT NOT NULL,
                artifact_digest TEXT NOT NULL,
                authority_digest TEXT NOT NULL,
                principal_id TEXT NOT NULL,
                owner_person_id TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                max_uses INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_toolsmith_graduations_tool
                ON toolsmith_graduations(tool_id, created_at);
            """
        )
        self._ensure_column("tools", "candidate_digest", "TEXT")
        self._ensure_column("tools", "artifact_digest", "TEXT")
        self._ensure_column("tools", "capability_manifest", "TEXT")
        self._backfill_integrity_digests()
        self._conn.commit()

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def valid_name(name: str) -> bool:
        return bool(_NAME_RE.match(name or ""))

    def tool_dir(self, tool_id: str) -> str:
        return os.path.join(self._library, tool_id)

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    @staticmethod
    def _artifact_digest_for(tool: Tool) -> str:
        return compute_artifact_digest(
            name=tool.name,
            description=tool.description,
            source_code=tool.source_code,
            input_schema=tool.input_schema,
            test_source=tool.test_source,
            origin_kind=tool.origin_kind,
            evidence=tool.evidence,
            candidate_digest_value=tool.candidate_digest,
            capability_manifest=tool.capability_manifest,
        )

    def _backfill_integrity_digests(self) -> None:
        rows = self._conn.execute("SELECT * FROM tools").fetchall()
        for row in rows:
            tool = self._row_to_tool(row)
            candidate = tool.candidate_digest or digest_json({
                "legacy_tool_id": tool.tool_id,
                "origin_kind": tool.origin_kind,
                "evidence": tool.evidence,
            })
            tool.candidate_digest = candidate
            # Only fill fields introduced by this migration.  Once an
            # artifact digest exists, never silently recompute/bless changed
            # code during startup; artifact_intact() must expose the mismatch.
            artifact = row["artifact_digest"] or self._artifact_digest_for(tool)
            manifest = row["capability_manifest"] or json.dumps(
                PURE_CAPABILITY_MANIFEST, sort_keys=True)
            if (
                not row["candidate_digest"]
                or not row["artifact_digest"]
                or not row["capability_manifest"]
            ):
                self._conn.execute(
                    "UPDATE tools SET candidate_digest=?, artifact_digest=?,"
                    " capability_manifest=? "
                    "WHERE tool_id=?",
                    (candidate, artifact, manifest, tool.tool_id),
                )

    def _row_to_tool(self, r: sqlite3.Row) -> Tool:
        return Tool(
            tool_id=r["tool_id"], name=r["name"], description=r["description"],
            status=r["status"], source_code=r["source_code"],
            input_schema=json.loads(r["input_schema"] or "{}"),
            checksum_sha256=r["checksum_sha256"] or "",
            candidate_digest=r["candidate_digest"] or "",
            artifact_digest=r["artifact_digest"] or "",
            capability_manifest=json.loads(
                r["capability_manifest"] or json.dumps(PURE_CAPABILITY_MANIFEST)),
            origin_kind=r["origin_kind"] or "mined",
            evidence=json.loads(r["evidence"] or "[]"),
            test_source=r["test_source"] or "",
            verify_detail=json.loads(r["verify_detail"] or "{}"),
            invocations=r["invocations"] or 0, failures=r["failures"] or 0,
            shadow_runs=r["shadow_runs"] or 0, last_used_at=r["last_used_at"],
            created_at=r["created_at"], updated_at=r["updated_at"])

    # -- writes -----------------------------------------------------------
    def create_draft(self, *, name: str, description: str, source_code: str,
                     input_schema: Dict[str, Any], test_source: str,
                     origin_kind: str = "mined",
                     evidence: Optional[List[str]] = None,
                     candidate_digest: str = "") -> Optional[Tool]:
        if not self.valid_name(name):
            logger.warning("toolsmith: invalid tool name %r", name)
            return None
        if name in STATIC_TOOL_NAMES:
            logger.warning("toolsmith: reserved first-party tool name %r", name)
            return None
        if self.get_by_name(name) is not None:
            logger.info("toolsmith: tool named %r already exists", name)
            return None
        tool_id = f"tool-{uuid.uuid4().hex[:12]}"
        now = time.time()
        checksum = hashlib.sha256(source_code.encode()).hexdigest()
        provenance_digest = candidate_digest or digest_json({
            "origin_kind": origin_kind,
            "evidence": evidence or [],
            "name": name,
        })
        tool = Tool(
            tool_id=tool_id, name=name, description=description,
            status=ToolStatus.DRAFT, source_code=source_code,
            input_schema=input_schema or {}, checksum_sha256=checksum,
            candidate_digest=provenance_digest,
            capability_manifest=dict(PURE_CAPABILITY_MANIFEST),
            origin_kind=origin_kind, evidence=evidence or [],
            test_source=test_source, created_at=now, updated_at=now)
        tool.artifact_digest = self._artifact_digest_for(tool)
        self._persist_files(tool)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tools (tool_id,name,description,status,"
                "source_code,input_schema,checksum_sha256,candidate_digest,"
                "artifact_digest,capability_manifest,origin_kind,evidence,"
                "test_source,verify_detail,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tool_id, name, description, ToolStatus.DRAFT, source_code,
                 json.dumps(input_schema or {}), checksum, provenance_digest,
                 tool.artifact_digest,
                 json.dumps(tool.capability_manifest, sort_keys=True),
                 origin_kind, json.dumps(evidence or []), test_source, "{}",
                 now, now))
            self._conn.commit()
        return tool

    def _persist_files(self, tool: Tool) -> None:
        d = self.tool_dir(tool.tool_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "tool.py"), "w") as f:
            f.write(tool.source_code)
        with open(os.path.join(d, "test_tool.py"), "w") as f:
            f.write(tool.test_source)
        with open(os.path.join(d, "manifest.json"), "w") as f:
            json.dump({"tool_id": tool.tool_id, "name": tool.name,
                       "description": tool.description,
                       "input_schema": tool.input_schema,
                       "checksum_sha256": tool.checksum_sha256,
                       "candidate_digest": tool.candidate_digest,
                       "artifact_digest": tool.artifact_digest,
                       "capability_manifest": tool.capability_manifest,
                       "origin_kind": tool.origin_kind}, f, indent=2)

    def set_status(self, tool_id: str, status: str, *,
                   verify_detail: Optional[Dict[str, Any]] = None) -> bool:
        if status not in ToolStatus.ALL:
            return False
        # LIVE is a capability publication, not a generic lifecycle edit.  It
        # must be committed atomically with a one-shot authority receipt.
        if status == ToolStatus.LIVE:
            return False
        with self._lock:
            if verify_detail is not None:
                self._conn.execute(
                    "UPDATE tools SET status=?, verify_detail=?, updated_at=?"
                    " WHERE tool_id=?",
                    (status, json.dumps(verify_detail), time.time(), tool_id))
            else:
                self._conn.execute(
                    "UPDATE tools SET status=?, updated_at=? WHERE tool_id=?",
                    (status, time.time(), tool_id))
            self._conn.commit()
        return True

    def artifact_intact(self, tool: Tool) -> bool:
        return bool(
            tool.artifact_digest
            and tool.artifact_digest == self._artifact_digest_for(tool)
        )

    def record_shadow_comparison(
        self,
        *,
        tool: Tool,
        capture_id: str,
        capture_source: str,
        principal_id: str,
        input_digest: str,
        incumbent_output_digest: str,
        candidate_output_digest: str,
        repeat_output_digest: str,
        deterministic: bool,
        matched: bool,
    ) -> Dict[str, Any]:
        """Persist one immutable same-input comparison exactly once."""

        if tool.status != ToolStatus.SHADOW:
            raise ValueError("tool is not in shadow state")
        if not self.artifact_intact(tool):
            raise ValueError("tool artifact digest mismatch")
        success = bool(deterministic and matched)
        payload = {
            "tool_id": tool.tool_id,
            "capture_id": capture_id,
            "capture_source": capture_source,
            "principal_id": principal_id,
            "artifact_digest": tool.artifact_digest,
            "input_digest": input_digest,
            "incumbent_output_digest": incumbent_output_digest,
            "candidate_output_digest": candidate_output_digest,
            "repeat_output_digest": repeat_output_digest,
            "deterministic": bool(deterministic),
            "matched": bool(matched),
            "success": success,
        }
        comparison_id = "cmp_" + digest_json(payload)
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                current_row = self._conn.execute(
                    "SELECT * FROM tools WHERE tool_id=?", (tool.tool_id,)
                ).fetchone()
                if current_row is None:
                    raise ValueError("tool was not found")
                current = self._row_to_tool(current_row)
                if current.status != ToolStatus.SHADOW:
                    raise ValueError("tool is not in shadow state")
                if (
                    current.artifact_digest != tool.artifact_digest
                    or not self.artifact_intact(current)
                ):
                    raise ValueError("tool artifact digest mismatch")
                existing = self._conn.execute(
                    "SELECT * FROM toolsmith_shadow_comparisons "
                    "WHERE tool_id=? AND capture_id=?",
                    (tool.tool_id, capture_id),
                ).fetchone()
                if existing is not None:
                    if existing["comparison_id"] != comparison_id:
                        raise ValueError(
                            "shadow capture replayed with conflicting content"
                        )
                    self._conn.commit()
                    return {
                        **payload,
                        "comparison_id": comparison_id,
                        "replayed": True,
                    }
                self._conn.execute(
                    "INSERT INTO toolsmith_shadow_comparisons ("
                    "comparison_id,tool_id,capture_id,capture_source,principal_id,"
                    "artifact_digest,input_digest,incumbent_output_digest,"
                    "candidate_output_digest,repeat_output_digest,deterministic,"
                    "matched,success,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        comparison_id,
                        tool.tool_id,
                        capture_id,
                        capture_source,
                        principal_id,
                        tool.artifact_digest,
                        input_digest,
                        incumbent_output_digest,
                        candidate_output_digest,
                        repeat_output_digest,
                        int(deterministic),
                        int(matched),
                        int(success),
                        now,
                    ),
                )
                self._conn.execute(
                    "UPDATE tools SET shadow_runs=shadow_runs+1,"
                    " failures=failures+?, last_used_at=?, updated_at=?"
                    " WHERE tool_id=?",
                    (0 if success else 1, now, now, tool.tool_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {**payload, "comparison_id": comparison_id, "replayed": False}

    def graduate_with_authority(
        self,
        authority: GraduationAuthorityV1,
        *,
        shadow_min: int,
    ) -> Dict[str, Any]:
        """Atomically publish one exact artifact and consume one authority."""

        authority.assert_current()
        authority_digest = authority.authority_digest
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                prior = self._conn.execute(
                    "SELECT * FROM toolsmith_graduations WHERE authority_id=? "
                    "OR decision_id=?",
                    (authority.authority_id, authority.decision_id),
                ).fetchone()
                if prior is not None:
                    if (
                        prior["authority_digest"] != authority_digest
                        or prior["tool_id"] != authority.tool_id
                    ):
                        raise GraduationAuthorityError(
                            "authority_replay",
                            "authority_id or decision_id was used for another decision",
                        )
                    self._conn.commit()
                    return {
                        "graduated": authority.tool_id,
                        "authority_id": authority.authority_id,
                        "authority_digest": authority_digest,
                        "replayed": True,
                    }

                row = self._conn.execute(
                    "SELECT * FROM tools WHERE tool_id=?", (authority.tool_id,)
                ).fetchone()
                if row is None:
                    raise GraduationAuthorityError(
                        "tool_not_found", "tool was not found"
                    )
                tool = self._row_to_tool(row)
                if tool.name in STATIC_TOOL_NAMES:
                    raise GraduationAuthorityError(
                        "tool_name_reserved",
                        "tool name collides with a first-party capability",
                    )
                if tool.status != ToolStatus.SHADOW:
                    raise GraduationAuthorityError(
                        "tool_not_shadow", "tool is not in shadow state"
                    )
                if not self.artifact_intact(tool):
                    raise GraduationAuthorityError(
                        "artifact_integrity_failure", "tool artifact digest mismatch"
                    )
                if tool.candidate_digest != authority.candidate_digest:
                    raise GraduationAuthorityError(
                        "candidate_digest_mismatch", "candidate digest changed"
                    )
                if tool.artifact_digest != authority.artifact_digest:
                    raise GraduationAuthorityError(
                        "artifact_digest_mismatch", "artifact digest changed"
                    )
                clean_row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM toolsmith_shadow_comparisons "
                    "WHERE tool_id=? AND success=1",
                    (tool.tool_id,),
                ).fetchone()
                clean_comparisons = int(clean_row["n"] if clean_row else 0)
                if clean_comparisons < shadow_min or tool.failures:
                    raise GraduationAuthorityError(
                        "shadow_evidence_insufficient",
                        "tool lacks the required clean same-input comparisons",
                    )
                now = time.time()
                self._conn.execute(
                    "INSERT INTO toolsmith_graduations (authority_id,decision_id,"
                    "tool_id,candidate_digest,artifact_digest,authority_digest,"
                    "principal_id,owner_person_id,issued_at,expires_at,max_uses,"
                    "status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        authority.authority_id,
                        authority.decision_id,
                        authority.tool_id,
                        authority.candidate_digest,
                        authority.artifact_digest,
                        authority_digest,
                        authority.principal_id,
                        authority.owner_person_id,
                        authority.issued_at,
                        authority.expires_at,
                        authority.max_uses,
                        "consumed",
                        now,
                    ),
                )
                changed = self._conn.execute(
                    "UPDATE tools SET status=?, updated_at=? "
                    "WHERE tool_id=? AND status=?",
                    (ToolStatus.LIVE, now, tool.tool_id, ToolStatus.SHADOW),
                ).rowcount
                if changed != 1:
                    raise GraduationAuthorityError(
                        "graduation_conflict", "tool state changed during graduation"
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {
            "graduated": authority.tool_id,
            "authority_id": authority.authority_id,
            "authority_digest": authority_digest,
            "replayed": False,
        }

    def audit_projection(self, tool_id: str) -> Dict[str, Any]:
        """Return digest-only comparison and publication receipts."""

        with self._lock:
            comparisons = self._conn.execute(
                "SELECT comparison_id,capture_id,capture_source,principal_id,"
                "artifact_digest,input_digest,incumbent_output_digest,"
                "candidate_output_digest,repeat_output_digest,deterministic,"
                "matched,success,created_at FROM toolsmith_shadow_comparisons "
                "WHERE tool_id=? ORDER BY created_at ASC",
                (tool_id,),
            ).fetchall()
            graduations = self._conn.execute(
                "SELECT authority_id,decision_id,candidate_digest,artifact_digest,"
                "authority_digest,principal_id,owner_person_id,issued_at,expires_at,"
                "max_uses,status,created_at FROM toolsmith_graduations "
                "WHERE tool_id=? ORDER BY created_at ASC",
                (tool_id,),
            ).fetchall()
        return {
            "shadow_comparisons": [dict(row) for row in comparisons],
            "graduations": [dict(row) for row in graduations],
        }

    def clean_comparison_count(self, tool_id: str) -> int:
        """Count only receipt-backed successful comparisons, never old counters."""

        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM toolsmith_shadow_comparisons "
                "WHERE tool_id=? AND success=1",
                (tool_id,),
            ).fetchone()
        return int(row["n"] if row is not None else 0)

    def record_invocation(self, tool_id: str, *, success: bool,
                          shadow: bool = False) -> None:
        with self._lock:
            if shadow:
                self._conn.execute(
                    "UPDATE tools SET shadow_runs=shadow_runs+1,"
                    " failures=failures+?, last_used_at=?, updated_at=?"
                    " WHERE tool_id=?",
                    (0 if success else 1, time.time(), time.time(), tool_id))
            else:
                self._conn.execute(
                    "UPDATE tools SET invocations=invocations+1,"
                    " failures=failures+?, last_used_at=?, updated_at=?"
                    " WHERE tool_id=?",
                    (0 if success else 1, time.time(), time.time(), tool_id))
            self._conn.commit()

    # -- reads ------------------------------------------------------------
    def get(self, tool_id: str) -> Optional[Tool]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM tools WHERE tool_id=?", (tool_id,)).fetchone()
        return self._row_to_tool(r) if r else None

    def get_by_name(self, name: str) -> Optional[Tool]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM tools WHERE name=?", (name,)).fetchone()
        return self._row_to_tool(r) if r else None

    def list(self, status: Optional[str] = None,
             limit: int = 200) -> List[Tool]:
        q = "SELECT * FROM tools"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"
            params.append(status)
        q += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._row_to_tool(r) for r in rows]

    def advertised(self) -> List[Tool]:
        """Tools the reasoning loop should see: shadow + live."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tools WHERE status IN (?,?)",
                (ToolStatus.SHADOW, ToolStatus.LIVE)).fetchall()
        return [self._row_to_tool(r) for r in rows]
