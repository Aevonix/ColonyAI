"""Contact Engagement Profile — an evolving, per-contact model of HOW the agent
should communicate and engage with each person, giving it a growing edge in
relationships.

Fuses two evidence streams into one profile:
  - psychology (OCEAN / Big Five) inferred from WHAT a contact says
  - communication style observed from HOW they say it (formality, directness,
    warmth, verbosity, emoji, humour)

Each dimension is an exponential moving average with a sample count (-> confidence),
so the profile sharpens as the relationship deepens. From the numeric profile +
qualitative notes (motivators / engaging topics / things to avoid) it derives a
concrete, deterministic "how to engage" brief that is surfaced to the agent every
turn for a known contact.
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
_CONF_FULL_N = 6  # samples for full confidence


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
        if not row:
            return {"contact_id": contact_id, "dims": {}, "qual": {}, "observation_count": 0}
        baseline = self._conn.execute('SELECT observation_count FROM engagement_baselines WHERE contact_id=?', (contact_id,)).fetchone()
        dims_raw = json.loads(row["dims_json"] or "{}")
        dims = {
            k: {"value": v["v"], "confidence": round(min(1.0, v["n"] / _CONF_FULL_N), 2), "n": v["n"]}
            for k, v in dims_raw.items()
        }
        return {
            "contact_id": contact_id,
            "dims": dims,
            "qual": json.loads(row["qual_json"] or "{}"),
            "observation_count": row["observation_count"],
            "updated_at": row["updated_at"],
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
            from colony_sidecar.turns.idempotency import SourceErased
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
            from colony_sidecar.turns.idempotency import SourceErased
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


# ---------------------------------------------------------------------------
# Deterministic "how to engage" brief from a profile (no LLM at surface time).
# ---------------------------------------------------------------------------
_HI, _LO, _MINCONF = 0.30, -0.30, 0.30   # OCEAN thresholds + min confidence to assert
_SHI, _SLO = 0.62, 0.38                   # style thresholds (0..1)

_OCEAN_GUIDANCE = {
    "openness":          ("They're curious and idea-driven — explore concepts, novelty, the big picture.",
                          "They're practical — stay concrete, proven, and to-the-point."),
    "conscientiousness": ("They value reliability and order — be precise, organized, and follow through.",
                          "They're flexible and spontaneous — don't over-structure; keep it loose."),
    "extraversion":      ("They're outgoing — match their energy, be warm and conversational.",
                          "They're reserved — be calm and concise, give them space, don't over-socialize."),
    "agreeableness":     ("They value harmony — be collaborative and soften disagreement.",
                          "They're frank and skeptical — be direct and data-driven, don't sugarcoat."),
    "neuroticism":       ("They run anxious — be reassuring and steady; avoid alarming framing or pressure.",
                          "They're even-keeled — you can be candid about problems and risks."),
}
_STYLE_GUIDANCE = {
    "formality":  ("Keep it professional and polished.", "Keep it casual and relaxed."),
    "directness": ("Lead with the bottom line.", "Ease in with a little context before the ask."),
    "warmth":     ("Use a warm, personable tone.", "Keep the tone neutral and businesslike."),
    "verbosity":  ("They appreciate detail — you can be expansive.", "Be brief — they want the short version."),
    "emoji_ok":   ("Emoji and light formatting are welcome.", "Skip emoji; keep it plain."),
    "humor":      ("Humour and playfulness land well.", "Keep it earnest and straightforward."),
}


def build_guidance(profile: Dict[str, Any]) -> str:
    """Render a concrete, evolving 'how to engage' brief, or '' if too little evidence."""
    dims = profile.get("dims", {})
    if profile.get("observation_count", 0) < 2 and not dims:
        return ""
    bullets: List[str] = []
    for dim in OCEAN:
        d = dims.get(dim)
        if not d or d["confidence"] < _MINCONF:
            continue
        v = d["value"]
        if v >= _HI:
            bullets.append(_OCEAN_GUIDANCE[dim][0])
        elif v <= _LO:
            bullets.append(_OCEAN_GUIDANCE[dim][1])
    for dim in STYLE:
        d = dims.get(dim)
        if not d or d["confidence"] < _MINCONF:
            continue
        v = d["value"]
        if v >= _SHI:
            bullets.append(_STYLE_GUIDANCE[dim][0])
        elif v <= _SLO:
            bullets.append(_STYLE_GUIDANCE[dim][1])

    qual = profile.get("qual", {})
    tail = []
    if qual.get("motivators"):
        tail.append("Motivated by: " + ", ".join(qual["motivators"][-4:]) + ".")
    if qual.get("topics"):
        tail.append("Engages on: " + ", ".join(qual["topics"][-4:]) + ".")
    if qual.get("avoid"):
        tail.append("Avoid: " + ", ".join(qual["avoid"][-4:]) + ".")

    if not bullets and not tail:
        return ""
    out = "\n".join(f"- {b}" for b in bullets)
    if tail:
        out += ("\n" if out else "") + " ".join(tail)
    return out
