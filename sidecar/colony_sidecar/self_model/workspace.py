"""Cognitive workspace: continuity of thought between interactions (Mind M2).

A bounded store of active concerns, each carrying a salience score that
events raise and time decays. When idle capacity exists the scheduler pops
the most salient concern and runs one bounded thinking job; the outcome
updates memory, resolves the concern, proposes an initiative or experiment,
or concludes "nothing to do" (which decays salience faster so rumination
cannot persist). A nightly sleep window lets the heavy standing agenda run
when the cluster is idle.

This is the difference between "runs phases every N hours" and "has
something on her mind." Generic in ColonyAI; the deployment feeds it events
and supplies the thinker (the LLM reasoning path).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

CONCERN_KINDS = ("question", "goal", "thread", "anomaly", "maintenance")


def workspace_mode() -> str:
    m = os.environ.get("COLONY_WORKSPACE", "off").strip().lower()
    return m if m in ("off", "shadow", "live") else "off"


def workspace_enabled() -> bool:
    return workspace_mode() in ("shadow", "live")


def _capacity() -> int:
    try:
        return int(os.environ.get("COLONY_WORKSPACE_CAPACITY", "24"))
    except ValueError:
        return 24


def _decay_half_life_hours() -> float:
    try:
        return float(os.environ.get("COLONY_WORKSPACE_HALFLIFE_HOURS", "12"))
    except ValueError:
        return 12.0


def _evict_floor() -> float:
    try:
        return float(os.environ.get("COLONY_WORKSPACE_EVICT_FLOOR", "0.05"))
    except ValueError:
        return 0.05


def _thought_budget() -> int:
    try:
        return int(os.environ.get("COLONY_WORKSPACE_THOUGHT_BUDGET", "8"))
    except ValueError:
        return 8


def _resolved_ttl_hours() -> float:
    """How long a resolved concern suppresses re-raising the same dedup_key.

    A concern is often raised FROM a still-open source by a periodic ingest;
    once someone resolves the concern, recreating it on the very next tick
    makes the resolve cosmetic. Within this window the resolved row answers
    the upsert instead. If the source is genuinely still open after the
    window, the concern legitimately returns."""
    try:
        return float(os.environ.get("COLONY_WORKSPACE_RESOLVED_TTL_HOURS", "24"))
    except ValueError:
        return 24.0


def in_sleep_window(now: Optional[datetime] = None) -> bool:
    """COLONY_SLEEP_WINDOW = 'HH:MM-HH:MM' in the deployment's local time
    (uses the process tz). Empty disables. Wrap-around (22:00-06:00) ok."""
    win = os.environ.get("COLONY_SLEEP_WINDOW", "").strip()
    if not win or "-" not in win:
        return False
    try:
        a, b = win.split("-", 1)
        ah, am = [int(x) for x in a.split(":")]
        bh, bm = [int(x) for x in b.split(":")]
    except ValueError:
        return False
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start, end = ah * 60 + am, bh * 60 + bm
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end   # wraps midnight


@dataclass
class Concern:
    concern_id: str
    kind: str
    summary: str
    salience: float
    sources: List[str] = field(default_factory=list)
    thoughts_spent: int = 0
    max_thoughts: int = 8
    status: str = "active"           # active | resolved | evicted
    last_note: str = ""
    created_at: float = 0.0
    last_touched: float = 0.0
    last_thought_at: Optional[float] = None
    # memory ids the thinker actually consulted while reasoning about this
    # concern (a measured provenance link, most-recent first) -- NOT a
    # render-time similarity guess. Empty until it has been thought about.
    memory_refs: List[str] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        return {
            "concern_id": self.concern_id, "kind": self.kind,
            "summary": self.summary, "salience": round(self.salience, 4),
            "sources": self.sources, "thoughts_spent": self.thoughts_spent,
            "max_thoughts": self.max_thoughts, "status": self.status,
            "last_note": self.last_note, "created_at": self.created_at,
            "last_touched": self.last_touched,
            "last_thought_at": self.last_thought_at,
            "memory_refs": self.memory_refs,
        }


class ConcernStore:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS concerns (
                concern_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                summary TEXT NOT NULL,
                salience REAL NOT NULL,
                sources TEXT,
                dedup_key TEXT,
                thoughts_spent INTEGER DEFAULT 0,
                max_thoughts INTEGER DEFAULT 8,
                status TEXT DEFAULT 'active',
                last_note TEXT,
                created_at REAL NOT NULL,
                last_touched REAL NOT NULL,
                last_thought_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_concern_status_sal
                ON concerns(status, salience);
            CREATE INDEX IF NOT EXISTS idx_concern_dedup ON concerns(dedup_key);
            """
        )
        # migration: memory_refs was added after first ship; add it to older
        # DBs. ADD COLUMN is backward-compatible (existing rows read NULL).
        try:
            self._conn.execute("ALTER TABLE concerns ADD COLUMN memory_refs TEXT")
        except sqlite3.OperationalError:
            pass  # column already present
        self._conn.commit()

    def _row(self, r: sqlite3.Row) -> Concern:
        keys = r.keys()
        mrefs = json.loads(r["memory_refs"] or "[]") if "memory_refs" in keys else []
        return Concern(
            concern_id=r["concern_id"], kind=r["kind"], summary=r["summary"],
            salience=r["salience"], sources=json.loads(r["sources"] or "[]"),
            thoughts_spent=r["thoughts_spent"] or 0,
            max_thoughts=r["max_thoughts"] or 8, status=r["status"],
            last_note=r["last_note"] or "", created_at=r["created_at"],
            last_touched=r["last_touched"], last_thought_at=r["last_thought_at"],
            memory_refs=mrefs)

    def upsert(self, *, kind: str, summary: str, salience: float,
               dedup_key: str, sources: List[str],
               max_thoughts: int) -> Concern:
        now = time.time()
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM concerns WHERE dedup_key=? AND status='active'",
                (dedup_key,)).fetchone()
            if r is not None:
                merged = list(dict.fromkeys(
                    json.loads(r["sources"] or "[]") + sources))[:30]
                new_sal = min(1.0, max(r["salience"], salience) + 0.05)
                self._conn.execute(
                    "UPDATE concerns SET salience=?, sources=?, "
                    "last_touched=? WHERE concern_id=?",
                    (new_sal, json.dumps(merged), now, r["concern_id"]))
                self._conn.commit()
                return self._row(self._conn.execute(
                    "SELECT * FROM concerns WHERE concern_id=?",
                    (r["concern_id"],)).fetchone())
            # Recently-resolved same key: return the resolved row untouched
            # instead of minting a fresh concern, so a resolve sticks even
            # while the underlying source is still open (see _resolved_ttl).
            r2 = self._conn.execute(
                "SELECT * FROM concerns WHERE dedup_key=? AND "
                "status='resolved' ORDER BY last_touched DESC LIMIT 1",
                (dedup_key,)).fetchone()
            if r2 is not None and (now - (r2["last_touched"] or 0)) < \
                    _resolved_ttl_hours() * 3600.0:
                return self._row(r2)
            cid = f"c-{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO concerns (concern_id,kind,summary,salience,"
                "sources,dedup_key,max_thoughts,created_at,last_touched)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, kind, summary, min(1.0, salience), json.dumps(sources),
                 dedup_key, max_thoughts, now, now))
            self._conn.commit()
            return self._row(self._conn.execute(
                "SELECT * FROM concerns WHERE concern_id=?", (cid,)).fetchone())

    def active(self, limit: int = 100) -> List[Concern]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM concerns WHERE status='active'"
                " ORDER BY salience DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, concern_id: str) -> Optional[Concern]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM concerns WHERE concern_id=?",
                (concern_id,)).fetchone()
        return self._row(r) if r else None

    def set_salience(self, concern_id: str, salience: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE concerns SET salience=?, last_touched=? "
                "WHERE concern_id=?",
                (max(0.0, min(1.0, salience)), time.time(), concern_id))
            self._conn.commit()

    def record_thought(self, concern_id: str, note: str, *,
                       resolved: bool, salience: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE concerns SET thoughts_spent=thoughts_spent+1,"
                " last_note=?, last_thought_at=?, last_touched=?,"
                " salience=?, status=? WHERE concern_id=?",
                (note[:500], now, now, max(0.0, min(1.0, salience)),
                 "resolved" if resolved else "active", concern_id))
            self._conn.commit()

    def set_memory_refs(self, concern_id: str, ids: List[str], cap: int = 12) -> None:
        """Record which memories the thinker consulted about this concern, most
        recent first, deduped and capped. A real, measured provenance link."""
        clean = [str(i) for i in (ids or []) if i]
        if not clean:
            return
        with self._lock:
            r = self._conn.execute(
                "SELECT memory_refs FROM concerns WHERE concern_id=?",
                (concern_id,)).fetchone()
            if r is None:
                return
            prior = []
            try:
                prior = json.loads((r["memory_refs"] if "memory_refs" in r.keys() else None) or "[]")
            except Exception:
                prior = []
            merged = list(dict.fromkeys(clean + prior))[:cap]
            self._conn.execute(
                "UPDATE concerns SET memory_refs=? WHERE concern_id=?",
                (json.dumps(merged), concern_id))
            self._conn.commit()

    def resolve_by_dedup(self, dedup_key: str, note: str) -> int:
        """Resolve every ACTIVE concern carrying this dedup_key (reverse
        cascade: when the source itself gets settled directly, the concern
        raised from it must leave her mind too). Returns count resolved."""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT concern_id FROM concerns WHERE dedup_key=? "
                "AND status='active'", (dedup_key,)).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE concerns SET status='resolved', salience=0.0,"
                    " last_note=?, last_touched=? WHERE concern_id=?",
                    (note[:500], now, r["concern_id"]))
            self._conn.commit()
            return len(rows)

    def evict_below(self, floor: float, keep: int) -> int:
        """Evict active concerns under the floor, and any beyond the capacity
        cap (lowest salience first). Returns count evicted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT concern_id, salience FROM concerns "
                "WHERE status='active' ORDER BY salience DESC").fetchall()
            to_evict = [r["concern_id"] for r in rows[keep:]]
            to_evict += [r["concern_id"] for r in rows[:keep]
                         if r["salience"] < floor]
            for cid in set(to_evict):
                self._conn.execute(
                    "UPDATE concerns SET status='evicted' WHERE concern_id=?",
                    (cid,))
            self._conn.commit()
            return len(set(to_evict))


class WorkspaceEngine:
    """Salience dynamics + the thinking scheduler.

    The thinker is an injected async callable `thinker(concern) -> dict` with
    keys: progress(bool), resolve(bool), note(str), action(optional dict
    {"kind": "initiative"|"experiment"|"memory"|"none", ...}). Kept out of
    this module so ColonyAI stays model-agnostic and tests inject a fake.
    """

    def __init__(self, store: ConcernStore, *,
                 thinker: Optional[Callable[[Concern], Awaitable[Dict[str, Any]]]] = None,
                 journal: Any = None,
                 on_action: Optional[Callable[[Concern, Dict[str, Any]], Awaitable[None]]] = None) -> None:
        self.store = store
        self._thinker = thinker
        self._journal = journal
        self._on_action = on_action

    # -- salience ---------------------------------------------------------
    def bump(self, *, kind: str, summary: str, dedup_key: str,
             salience: float = 0.5, sources: Optional[List[str]] = None,
             max_thoughts: Optional[int] = None) -> Concern:
        kind = kind if kind in CONCERN_KINDS else "thread"
        return self.store.upsert(
            kind=kind, summary=summary[:300], salience=salience,
            dedup_key=dedup_key[:200], sources=sources or [],
            max_thoughts=max_thoughts or _thought_budget())

    def decay(self) -> int:
        """Exponential time-decay of every active concern; evict the floor
        and anything over capacity. Returns the number evicted."""
        hl = _decay_half_life_hours() * 3600.0
        now = time.time()
        for c in self.store.active(limit=500):
            dt = max(0.0, now - c.last_touched)
            factor = 0.5 ** (dt / hl) if hl > 0 else 1.0
            self.store.set_salience(c.concern_id, c.salience * factor)
        return self.store.evict_below(_evict_floor(), _capacity())

    def top(self) -> Optional[Concern]:
        active = self.store.active(limit=1)
        return active[0] if active else None

    # -- thinking ---------------------------------------------------------
    async def think_once(self) -> Optional[Dict[str, Any]]:
        """Pop the most salient thinkable concern and run one thought.
        Returns the outcome dict, or None if nothing to think about."""
        if self._thinker is None:
            return None
        concern = None
        for c in self.store.active(limit=20):
            if c.thoughts_spent < c.max_thoughts:
                concern = c
                break
        if concern is None:
            return None
        try:
            outcome = await self._thinker(concern) or {}
        except Exception as exc:
            logger.warning("workspace thinker failed: %s", exc)
            return None
        progressed = bool(outcome.get("progress"))
        resolved = bool(outcome.get("resolve"))
        note = str(outcome.get("note", ""))[:500]
        # progress sustains salience; no progress decays it harder so
        # rumination on a stuck concern fades instead of looping forever.
        new_sal = concern.salience * (0.9 if progressed else 0.6)
        self.store.record_thought(concern.concern_id, note,
                                  resolved=resolved, salience=new_sal)
        # persist the memory ids the thinker actually recalled about this
        # concern (provenance for the memory-field beams; render draws a beam
        # only when a ref id is also a point on the sampled field).
        refs = outcome.get("memory_refs")
        if refs:
            try:
                self.store.set_memory_refs(concern.concern_id, refs)
            except Exception:
                logger.debug("set_memory_refs failed", exc_info=True)
        self._log(f"thought on {concern.kind}: {concern.summary[:60]} "
                  f"-> {'resolved' if resolved else 'progress' if progressed else 'no progress'}",
                  note)
        action = outcome.get("action")
        if action and self._on_action is not None and workspace_mode() == "live":
            try:
                await self._on_action(concern, action)
            except Exception:
                logger.debug("workspace on_action failed", exc_info=True)
        return {"concern_id": concern.concern_id, "resolved": resolved,
                "progress": progressed, "note": note, "action": action}

    def _log(self, desc: str, note: str) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record("workspace", desc, reasoning=note,
                                  decision="noted", outcome="thought")
        except Exception:
            logger.debug("workspace journal write failed", exc_info=True)

    # -- read side --------------------------------------------------------
    def snapshot(self, limit: int = 24) -> Dict[str, Any]:
        active = self.store.active(limit=limit)
        return {"mode": workspace_mode(),
                "capacity": _capacity(),
                "sleeping": in_sleep_window(),
                "concerns": [c.public() for c in active]}
