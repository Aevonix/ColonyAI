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
            """
        )
        self._conn.commit()

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def valid_name(name: str) -> bool:
        return bool(_NAME_RE.match(name or ""))

    def tool_dir(self, tool_id: str) -> str:
        return os.path.join(self._library, tool_id)

    def _row_to_tool(self, r: sqlite3.Row) -> Tool:
        return Tool(
            tool_id=r["tool_id"], name=r["name"], description=r["description"],
            status=r["status"], source_code=r["source_code"],
            input_schema=json.loads(r["input_schema"] or "{}"),
            checksum_sha256=r["checksum_sha256"] or "",
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
                     evidence: Optional[List[str]] = None) -> Optional[Tool]:
        if not self.valid_name(name):
            logger.warning("toolsmith: invalid tool name %r", name)
            return None
        if self.get_by_name(name) is not None:
            logger.info("toolsmith: tool named %r already exists", name)
            return None
        tool_id = f"tool-{uuid.uuid4().hex[:12]}"
        now = time.time()
        checksum = hashlib.sha256(source_code.encode()).hexdigest()
        tool = Tool(
            tool_id=tool_id, name=name, description=description,
            status=ToolStatus.DRAFT, source_code=source_code,
            input_schema=input_schema or {}, checksum_sha256=checksum,
            origin_kind=origin_kind, evidence=evidence or [],
            test_source=test_source, created_at=now, updated_at=now)
        self._persist_files(tool)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tools (tool_id,name,description,status,"
                "source_code,input_schema,checksum_sha256,origin_kind,"
                "evidence,test_source,verify_detail,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tool_id, name, description, ToolStatus.DRAFT, source_code,
                 json.dumps(input_schema or {}), checksum, origin_kind,
                 json.dumps(evidence or []), test_source, "{}", now, now))
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
                       "origin_kind": tool.origin_kind}, f, indent=2)

    def set_status(self, tool_id: str, status: str, *,
                   verify_detail: Optional[Dict[str, Any]] = None) -> bool:
        if status not in ToolStatus.ALL:
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
