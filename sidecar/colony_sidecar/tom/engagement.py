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


class EngagementStore:
    """SQLite-backed evolving engagement profile per contact."""

    def __init__(self, db_path: str) -> None:
        self._db_path = str(db_path)
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
        self._conn.commit()

    # -- read ---------------------------------------------------------------
    def _row(self, contact_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM engagement_profiles WHERE contact_id=?", (contact_id,)
        ).fetchone()

    def get_profile(self, contact_id: str) -> Dict[str, Any]:
        row = self._row(contact_id)
        if not row:
            return {"contact_id": contact_id, "dims": {}, "qual": {}, "observation_count": 0}
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
    ) -> None:
        """Fold one observation into the contact's evolving profile (EMA per dim)."""
        if not contact_id:
            return
        row = self._row(contact_id)
        dims = json.loads(row["dims_json"]) if row else {}
        qual = json.loads(row["qual_json"]) if row else {}

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

        oc = (row["observation_count"] if row else 0) + 1
        self._conn.execute(
            """INSERT INTO engagement_profiles (contact_id, dims_json, qual_json, observation_count, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(contact_id) DO UPDATE SET
                 dims_json=excluded.dims_json, qual_json=excluded.qual_json,
                 observation_count=excluded.observation_count, updated_at=excluded.updated_at""",
            (contact_id, json.dumps(dims), json.dumps(qual), oc, _now()),
        )
        self._conn.commit()

    def purge(self, contact_id: str) -> None:
        self._conn.execute("DELETE FROM engagement_profiles WHERE contact_id=?", (contact_id,))
        self._conn.commit()


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
