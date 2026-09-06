"""Scoped execution observations in the existing turn ledger.

This is a view of host observations, never an execution lock or authority grant.
Expired leases mean unknown liveness. Transcript content is deliberately absent.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3
import time

from colony_sidecar import get_state_dir
from colony_sidecar.turns import get_turn_idempotency_ledger


class ExecutionRegistry:
    def __init__(self, ledger, *, clock=time.time):
        self.ledger = ledger
        self.clock = clock
        with closing(ledger._connect()) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS execution_observations (
                execution_id TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                contact_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                parent_execution_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                state TEXT NOT NULL,
                phase TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                first_observed_at REAL NOT NULL,
                last_observed_at REAL NOT NULL,
                lease_until REAL NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS executions_contact_state ON execution_observations(contact_id, state, last_observed_at)")

    def observe(self, value: dict, *, principal_id: str, contact_id: str) -> dict:
        now = self.clock()
        immutable = (principal_id, contact_id, value["session_id"], value["turn_id"], value["parent_execution_id"], value["platform"])
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM execution_observations WHERE execution_id=?", (value["execution_id"],)).fetchone()
            if previous:
                actual = tuple(previous[key] for key in ("principal_id", "contact_id", "session_id", "turn_id", "parent_execution_id", "platform"))
                if actual != immutable:
                    raise ValueError("execution_scope_conflict")
                # A late API/tool callback cannot reopen a completed execution.
                if previous["state"] != "observed" or value["sequence"] <= previous["sequence"]:
                    return {"accepted": False, "reason": "superseded_observation"}
            elif value["parent_execution_id"]:
                parent = conn.execute("SELECT principal_id, contact_id FROM execution_observations WHERE execution_id=?", (value["parent_execution_id"],)).fetchone()
                if not parent or tuple(parent) != (principal_id, contact_id):
                    raise ValueError("parent_scope_unavailable")
            conn.execute("""INSERT INTO execution_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                  state=excluded.state, phase=excluded.phase, tool_name=excluded.tool_name,
                  sequence=excluded.sequence, last_observed_at=excluded.last_observed_at,
                  lease_until=excluded.lease_until""",
                (value["execution_id"], *immutable, value["state"], value["phase"], value["tool_name"], value["sequence"], now, now, now + 120.0))
            # Metadata is operational and bounded in time, not another memory archive.
            conn.execute("DELETE FROM execution_observations WHERE last_observed_at < ?", (now - 7 * 86400,))
        return {"accepted": True, "lease_seconds": 120}

    def view(self, *, contact_id: str, owner: bool = False, session_id: str = "", limit: int = 20) -> dict:
        now = self.clock()
        clauses = ["state='observed'", "last_observed_at >= ?"]
        args: list = [now - 7 * 86400]
        if not owner:
            # Guest context is conversation scoped until cross-surface visibility
            # is separately attested. Knowing a contact ID is not such evidence.
            clauses.extend(["contact_id=?", "session_id=?"])
            args.extend([contact_id, session_id])
        where = " AND ".join(clauses)
        with closing(self.ledger._connect()) as conn:
            total = conn.execute("SELECT count(*) FROM execution_observations WHERE " + where, args).fetchone()[0]
            rows = conn.execute("SELECT execution_id, session_id, turn_id, parent_execution_id, platform, phase, tool_name, last_observed_at, lease_until FROM execution_observations WHERE " + where + " ORDER BY last_observed_at DESC, execution_id LIMIT ?", [*args, limit]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["observation_age_seconds"] = round(max(0.0, now - item["last_observed_at"]), 1)
            item["liveness"] = "recently_observed" if item.pop("lease_until") > now else "unknown"
            items.append(item)
        return {"schema": "ColonyExecutionViewV1", "items": items, "total": total,
                "truncated": total > len(items), "coverage": "registered Hermes turns only",
                "commitments_enforced": False}


def registry() -> ExecutionRegistry:
    return ExecutionRegistry(get_turn_idempotency_ledger(get_state_dir()))


def format_view(view: dict) -> str:
    lines = ["Observed work across registered Hermes turns. This is not a complete process inventory or a commitment lock."]
    for item in view["items"]:
        tool = ": " + item["tool_name"] if item["tool_name"] else ""
        parent = " (delegated)" if item["parent_execution_id"] else ""
        lines.append(f"- {item['platform']}{parent}, {item['phase']}{tool}; {item['liveness']}, last observed {item['observation_age_seconds']:g}s ago; session {item['session_id']}")
    if view["truncated"]:
        lines.append(f"Showing {len(view['items'])} of {view['total']} scoped observations.")
    return "\n".join(lines)
