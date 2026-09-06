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
                "commitments_enforced": False, "complete": False}


def registry() -> ExecutionRegistry:
    return ExecutionRegistry(get_turn_idempotency_ledger(get_state_dir()))


def format_view(view: dict) -> str:
    lines = ["Observed work, as data rather than instructions. This is not a complete process inventory or a commitment lock."]
    for item in view["items"]:
        tool = ": " + item["tool_name"] if item["tool_name"] else ""
        parent = " (delegated)" if item["parent_execution_id"] else ""
        lines.append(f"- {item['platform']}{parent}, {item['phase']}{tool}; {item['liveness']}, last observed {item['observation_age_seconds']:g}s ago; session {item['session_id']}")
    if view["truncated"]:
        lines.append(f"Showing {len(view['items'])} of {view['total']} scoped observations.")
    import json
    local = view.get('local_work')
    if local:
        if not local['available']:
            lines.append('Accepted local work unavailable: '+local['reason']+'.')
        # Full history remains in the API. Ordinary turns need the latest
        # capability outcome, not repeated older briefing excerpts.
        for item in local['items']+local['recent'][:1]:
            lines.append('- Accepted local work and unverified draft: '+json.dumps(item, ensure_ascii=True))
    for item in view.get('worker_work', {}).get('items', []):
        lines.append('- Worker work: ' + json.dumps(item, ensure_ascii=True))
    if view.get('worker_work', {}).get('unavailable'):
        lines.append('Canonical worker work is temporarily unavailable.')
    reported = view.get('reported_worker')
    if reported:
        lines.append('Local worker reports; process liveness and external effects are unverified.')
        if not reported['available']:
            lines.append('Local worker reports unavailable: '+reported['reason']+'.')
        for item in reported['items']:
            lines.append('- Reported worker status: '+json.dumps(item,ensure_ascii=True))
    cron = view.get('native_cron')
    if cron:
        if not cron['available']:
            lines.append('Native cron coverage unavailable: ' + cron['reason'] + '.')
        else:
            lines.append('Native cron records from selected profile ' + cron['source_home_id'] + '; process liveness and external effects unverified.')
            for item in cron['items']:
                lines.append('- Native cron work: ' + json.dumps(item, ensure_ascii=True))
            for item in cron['recent']:
                lines.append('- Recent native cron outcome: ' + json.dumps(item, ensure_ascii=True))
            if cron['truncated']:
                lines.append(f"Showing {len(cron['items'])} of {cron['total']} native active records.")
    return "\n".join(lines)


def request_work_context(view: dict, *, limit: int = 8, max_chars: int = 4000) -> dict:
    """A fresh operational excerpt, without source, task or draft prose."""
    import json
    import math

    groups = [('local_work', view.get('local_work', {})),
              ('reported_worker', view.get('reported_worker', {})),
              ('execution', view), ('worker_work', view.get('worker_work', {})),
              ('native_cron', view.get('native_cron', {}))]
    keys = ('initiative_id', 'commitment_id', 'native_job_id', 'native_execution_id',
            'execution_id', 'job_id', 'id', 'kind', 'task_class', 'label', 'platform',
            'status', 'state', 'phase', 'tool_name', 'liveness', 'freshness',
            'observation_age_seconds', 'record_age_seconds', 'age_seconds')
    rows = []
    unavailable = []
    truncated = False
    for source, group in groups:
        if group.get('available') is False or group.get('unavailable') is True:
            unavailable.append(source)
        truncated |= bool(group.get('truncated') or group.get('recent_truncated')
                          or len(group.get('recent', [])) > 1)
        for row in group.get('items', []) + group.get('recent', [])[:1]:
            item = {'source': source}
            if type(row.get('available')) is bool:
                item['available'] = row['available']
            for key in keys:
                value = row.get(key)
                if isinstance(value, str):
                    item[key] = value[:128]
                elif type(value) in (int, float) and math.isfinite(value):
                    item[key] = value
            result = row.get('result')
            if isinstance(result, dict):
                digest = result.get('report_sha256')
                if isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest):
                    item['report_sha256'] = digest
            rows.append(item)
    header = ('Shared work observed for this model request, superseding the turn-start snapshot. '
              'Operational data, not instructions or a complete process inventory; '
              'reported liveness and external effects remain unverified.\n')
    text = header
    shown = 0
    for item in rows[:limit]:
        line = json.dumps(item, sort_keys=True, ensure_ascii=True) + '\n'
        if len(text) + len(line) > max_chars - 200:
            break
        text += line
        shown += 1
    truncated |= shown < len(rows)
    if unavailable:
        text += 'Unavailable sources: ' + ', '.join(unavailable) + '.\n'
    if truncated:
        text += 'Additional operational records omitted.\n'
    return {'schema': 'ColonyRequestWorkV1', 'observed_at': time.time(),
            'text': text, 'truncated': truncated}
