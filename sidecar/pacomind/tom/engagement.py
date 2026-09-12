"""Read current attributed engagement observations; retain numeric history only.

Canonical source claims own preferences; AppraisalStore projects them. Legacy
aggregates remain inspectable but no longer produce psychological certainty.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Numeric dimensions. OCEAN are signed (-1 low .. +1 high); style are 0..1.
OCEAN = ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")
STYLE = ("formality", "directness", "warmth", "verbosity", "emoji_ok", "humor")
_ALL_DIMS = OCEAN + STYLE
_QUAL_KEYS = ("motivators", "topics", "avoid")
_QUAL_CAP = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


from .source_lineage import SourceLinkedStore


class EngagementStore(SourceLinkedStore):
    """SQLite-backed evolving engagement profile per contact."""

    def __init__(self, db_path: str, *, source_ledger=None) -> None:
        self._db_path = str(db_path)
        self._source_ledger = source_ledger
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS engagement_profiles (
                contact_id TEXT PRIMARY KEY,
                dims_json TEXT NOT NULL DEFAULT '{}',
                qual_json TEXT NOT NULL DEFAULT '{}',
                observation_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        # Capture old aggregate profiles once, without inventing source links.
        # New observations are separate so deletion can replay their EMA exactly.
        with self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            first = not self._conn.execute("SELECT 1 FROM sqlite_master WHERE name='engagement_baselines'").fetchone()
            self._conn.execute("""CREATE TABLE IF NOT EXISTS engagement_baselines (
                contact_id TEXT PRIMARY KEY, dims_json TEXT NOT NULL, qual_json TEXT NOT NULL,
                observation_count INTEGER NOT NULL, updated_at TEXT NOT NULL,
                evidence_basis TEXT NOT NULL DEFAULT 'legacy_unlinked')""")
            self._conn.execute("""CREATE TABLE IF NOT EXISTS engagement_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id TEXT NOT NULL,
                observation_json TEXT NOT NULL, source_lineage_json TEXT, created_at TEXT NOT NULL)""")
            self._conn.execute('CREATE INDEX IF NOT EXISTS idx_engagement_observation_contact ON engagement_observations(contact_id)')
            if first:
                self._conn.execute("""INSERT INTO engagement_baselines
                    (contact_id,dims_json,qual_json,observation_count,updated_at)
                    SELECT contact_id,dims_json,qual_json,observation_count,updated_at FROM engagement_profiles""")

    def purge_erased_sources(self, turn_ids=None, *, contact_id=None) -> int:
        sql = 'SELECT id,contact_id,source_lineage_json FROM engagement_observations WHERE source_lineage_json IS NOT NULL'
        rows = self._conn.execute(sql + (' AND contact_id=?' if contact_id else ''),
                                  (contact_id,) if contact_id else ()).fetchall()
        invalid = self._invalid_sources(rows, turn_ids)
        with self._conn:
            self._conn.executemany('DELETE FROM engagement_observations WHERE id=?', [(row['id'],) for row in invalid])
            for person in {row['contact_id'] for row in invalid}:
                self._recompute_profile(person)
        return len(invalid)

    # -- read ---------------------------------------------------------------
    def _row(self, contact_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM engagement_profiles WHERE contact_id=?", (contact_id,)
        ).fetchone()

    def get_profile(self, contact_id: str) -> Dict[str, Any]:
        self.purge_erased_sources(contact_id=contact_id)
        row = self._row(contact_id)
        baseline = self._conn.execute('SELECT observation_count FROM engagement_baselines WHERE contact_id=?', (contact_id,)).fetchone()
        dims_raw = json.loads(row["dims_json"] or "{}") if row else {}
        observations = []
        if self._source_ledger is not None:
            from pacomind.identity import get_owner_contact_id
            from pacomind.self_model.appraisals import AppraisalStore
            observations = AppraisalStore(self._source_ledger, owner_id=get_owner_contact_id()).view(
                contact_id, viewer_contact_id=contact_id)['records']
        return {
            "contact_id": contact_id, "dims": {}, "qual": {},
            "observations": observations, "observation_count": len(observations),
            "legacy_profile": {"dims": dims_raw, "qual": json.loads(row["qual_json"] or "{}") if row else {},
                "observation_count": row["observation_count"] if row else 0, "governing": False},
            "updated_at": row["updated_at"] if row else None,
            "legacy_unlinked_observations": baseline[0] if baseline else 0,
        }

    # -- write --------------------------------------------------------------
    def update_from_observation(
        self,
        contact_id: str,
        ocean: Optional[Dict[str, Any]] = None,
        style: Optional[Dict[str, Any]] = None,
        motivators: Optional[List[str]] = None,
        topics: Optional[List[str]] = None,
        avoid: Optional[List[str]] = None,
        source_lineage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Fold one observation into the contact's evolving profile (EMA per dim)."""
        if not contact_id:
            return
        if source_lineage is not None and not self._source_visible(contact_id, source_lineage):
            from pacomind.turns.idempotency import SourceErased
            raise SourceErased('source_erased')
        self.purge_erased_sources(contact_id=contact_id)
        observation = dict(ocean=ocean, style=style, motivators=motivators, topics=topics, avoid=avoid)
        with self._conn:
            self._conn.execute("""INSERT INTO engagement_observations
                (contact_id,observation_json,source_lineage_json,created_at) VALUES (?,?,?,?)""",
                (contact_id, json.dumps(observation), json.dumps(source_lineage) if source_lineage is not None else None, _now()))
            row = self._row(contact_id)
            dims = json.loads(row['dims_json']) if row else {}
            qual = json.loads(row['qual_json']) if row else {}
            self._fold(dims, qual, **observation)
            self._write_profile(contact_id, dims, qual, (row['observation_count'] if row else 0) + 1, _now())
        # Erasure can commit while the projection database is being written.
        if source_lineage is not None and not self._source_visible(contact_id, source_lineage):
            self.purge_erased_sources(contact_id=contact_id)
            from pacomind.turns.idempotency import SourceErased
            raise SourceErased('source_erased')

    @staticmethod
    def _fold(dims, qual, *, ocean=None, style=None, motivators=None, topics=None, avoid=None):
        obs = dict(ocean or {})
        obs.update(style or {})
        for dim, val in obs.items():
            if dim not in _ALL_DIMS or val is None:
                continue
            try:
                val = float(val)
            except (TypeError, ValueError):
                continue
            lo = -1.0 if dim in OCEAN else 0.0
            val = max(lo, min(1.0, val))
            cur = dims.get(dim)
            if cur is None:
                dims[dim] = {"v": round(val, 4), "n": 1}
            else:
                n = cur["n"] + 1
                alpha = max(0.15, 1.0 / n)  # responsive early, stabilises later
                v = cur["v"] + alpha * (val - cur["v"])
                dims[dim] = {"v": round(v, 4), "n": n}

        for key, items in (("motivators", motivators), ("topics", topics), ("avoid", avoid)):
            if not items:
                continue
            existing = list(qual.get(key, []))
            seen = {s.lower() for s in existing}
            for it in items:
                it = (it or "").strip()
                if it and it.lower() not in seen:
                    existing.append(it)
                    seen.add(it.lower())
            qual[key] = existing[-_QUAL_CAP:]  # keep most recent

    def _recompute_profile(self, contact_id):
        baseline = self._conn.execute('SELECT * FROM engagement_baselines WHERE contact_id=?', (contact_id,)).fetchone()
        rows = self._conn.execute('SELECT * FROM engagement_observations WHERE contact_id=? ORDER BY id', (contact_id,)).fetchall()
        if not baseline and not rows:
            self._conn.execute('DELETE FROM engagement_profiles WHERE contact_id=?', (contact_id,))
            return
        dims = json.loads(baseline['dims_json']) if baseline else {}
        qual = json.loads(baseline['qual_json']) if baseline else {}
        count = baseline['observation_count'] if baseline else 0
        updated = baseline['updated_at'] if baseline else None
        for row in rows:
            self._fold(dims, qual, **json.loads(row['observation_json']))
            count += 1
            updated = row['created_at']
        self._write_profile(contact_id, dims, qual, count, updated)

    def _write_profile(self, contact_id, dims, qual, count, updated):
        self._conn.execute(
            """INSERT INTO engagement_profiles (contact_id, dims_json, qual_json, observation_count, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(contact_id) DO UPDATE SET
                 dims_json=excluded.dims_json, qual_json=excluded.qual_json,
                 observation_count=excluded.observation_count, updated_at=excluded.updated_at""",
            (contact_id, json.dumps(dims), json.dumps(qual), count, updated),
        )

    def purge(self, contact_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM engagement_profiles WHERE contact_id=?", (contact_id,))
            self._conn.execute("DELETE FROM engagement_observations WHERE contact_id=?", (contact_id,))
            self._conn.execute("DELETE FROM engagement_baselines WHERE contact_id=?", (contact_id,))


def build_guidance(profile: Dict[str, Any]) -> str:
    """Only current attributed preferences; numeric legacy profiles never govern."""
    lines = []
    for item in profile.get('observations', [])[:4]:
        if item.get('kind') != 'preference' or item.get('status') != 'current' or not item.get('sources'):
            continue
        lines.append('Stated preference (attributed, correctable): ' + str(item.get('text', '')))
    return '\n'.join(lines)
