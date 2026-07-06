"""RelationshipProfiler -- per-person standing, psyche, and approach guidance.

The analysis half of docs/RELATIONSHIPS.md: once attribution flows (the
ParticipantResolver), every store already accumulates per-person signal.
This module composes those signals into one compact RelationshipBrief per
contact and derives concrete approach guidance the agent can act on
(preferred channel, best time to reach, engagement style, cautions).

Deliberately deterministic: the LLM already contributes upstream (the ToM
engagement extractor builds the OCEAN/style psyche profile from real
observations); the profiler only composes and derives. Briefs are cached in
``colony-relationships.db`` and refreshed by the autonomy phase when a
contact accrues new interactions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EXCLUDED_IDS = ("system", "default")


def profile_min_interactions() -> int:
    try:
        return max(1, int(os.environ.get(
            "COLONY_RELATIONSHIP_PROFILE_MIN_INTERACTIONS", "5")))
    except ValueError:
        return 5


def approach_guidance_enabled() -> bool:
    return os.environ.get(
        "COLONY_APPROACH_GUIDANCE", "true").strip().lower() != "false"


@dataclass
class RelationshipBrief:
    contact_id: str
    display_name: str = ""
    trust_tier: str = "unknown"
    interaction_count: int = 0
    last_interaction_at: str = ""
    relationship_score: Optional[float] = None
    channels: Dict[str, int] = field(default_factory=dict)
    preferred_channel: str = ""
    best_hours_local: List[int] = field(default_factory=list)
    timezone: str = ""
    affect_valence: Optional[float] = None
    affect_trend: str = ""
    psyche_guidance: List[str] = field(default_factory=list)
    psyche_motivators: List[str] = field(default_factory=list)
    rapport_topics: List[str] = field(default_factory=list)
    cautions: List[str] = field(default_factory=list)
    profiled_at: float = 0.0
    interactions_at_profile: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def render(self, *, include_approach: bool = True) -> str:
        """Compact prompt-injectable text form."""
        lines: List[str] = []
        head = f"{self.display_name or self.contact_id} ({self.trust_tier})"
        stats = f"{self.interaction_count} interactions"
        if self.last_interaction_at:
            stats += f", last {self.last_interaction_at[:10]}"
        if self.preferred_channel:
            stats += f", mostly via {self.preferred_channel}"
        lines.append(f"{head}: {stats}.")
        if self.affect_trend or self.affect_valence is not None:
            mood = []
            if self.affect_valence is not None:
                mood.append(f"valence {self.affect_valence:+.2f}")
            if self.affect_trend:
                mood.append(self.affect_trend)
            lines.append("Recent mood: " + ", ".join(mood) + ".")
        if self.rapport_topics:
            lines.append("Rapport topics: " + ", ".join(self.rapport_topics[:5]) + ".")
        if include_approach and approach_guidance_enabled():
            if self.psyche_guidance:
                lines.append("Approach: " + " ".join(self.psyche_guidance[:4]))
            if self.psyche_motivators:
                lines.append("Motivated by: " + ", ".join(self.psyche_motivators[:3]) + ".")
            if self.best_hours_local:
                hrs = ", ".join(f"{h:02d}:00" for h in self.best_hours_local[:2])
                tzs = f" ({self.timezone})" if self.timezone else ""
                lines.append(f"Usually reachable around {hrs}{tzs}.")
            for c in self.cautions[:2]:
                lines.append("Caution: " + c)
        return "\n".join(lines)


class RelationshipProfiler:
    def __init__(self, *, contacts_store: Any, comms_log: Any = None,
                 affect_store: Any = None, facts_store: Any = None,
                 engagement_store: Any = None,
                 db_path: Optional[str] = None) -> None:
        self._contacts = contacts_store
        self._comms = comms_log
        self._affect = affect_store
        self._facts = facts_store
        self._engagement = engagement_store
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS relationship_briefs (
                    contact_id TEXT PRIMARY KEY,
                    brief_json TEXT NOT NULL,
                    profiled_at REAL NOT NULL,
                    interactions_at_profile INTEGER DEFAULT 0
                )""")
            self._conn.commit()

    # -- cache --------------------------------------------------------------
    def cached(self, contact_id: str) -> Optional[RelationshipBrief]:
        with self._lock:
            row = self._conn.execute(
                "SELECT brief_json FROM relationship_briefs WHERE contact_id=?",
                (contact_id,)).fetchone()
        if row is None:
            return None
        try:
            return RelationshipBrief(**json.loads(row["brief_json"]))
        except Exception:
            return None

    def _save(self, brief: RelationshipBrief) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO relationship_briefs
                     (contact_id, brief_json, profiled_at, interactions_at_profile)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(contact_id) DO UPDATE SET
                     brief_json=excluded.brief_json,
                     profiled_at=excluded.profiled_at,
                     interactions_at_profile=excluded.interactions_at_profile""",
                (brief.contact_id, json.dumps(brief.to_dict()),
                 brief.profiled_at, brief.interactions_at_profile))
            self._conn.commit()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT contact_id, profiled_at, interactions_at_profile "
                "FROM relationship_briefs ORDER BY profiled_at DESC").fetchall()
        return [dict(r) for r in rows]

    # -- profiling ------------------------------------------------------------
    async def profile(self, contact_id: str) -> Optional[RelationshipBrief]:
        """Compose a fresh brief for one contact (None for non-persons)."""
        if not contact_id or contact_id in _EXCLUDED_IDS:
            return None
        try:
            contact = await self._contacts.get(contact_id)
        except Exception:
            contact = None
        if contact is None:
            return None

        brief = RelationshipBrief(
            contact_id=contact_id,
            display_name=str(contact.display_name or ""),
            trust_tier=str(contact.trust_tier or "unknown"),
            interaction_count=int(getattr(contact, "interaction_count", 0) or 0),
            last_interaction_at=str(getattr(contact, "last_interaction_at", "") or ""),
            relationship_score=getattr(contact, "relationship_score", None),
            timezone=str(getattr(contact, "timezone", "") or ""),
            profiled_at=time.time(),
        )
        brief.interactions_at_profile = brief.interaction_count

        # Channel usage + best hours (comms provenance).
        if self._comms is not None:
            try:
                stats = self._comms.stats(contact_id)
                brief.channels = stats.get("channels", {})
                if brief.channels:
                    brief.preferred_channel = max(
                        brief.channels, key=brief.channels.get)
                hours = stats.get("hours_utc") or []
                if sum(hours) >= 5:
                    offset = self._tz_offset_hours(brief.timezone)
                    ranked = sorted(range(24), key=lambda h: -hours[h])
                    brief.best_hours_local = [
                        (h + offset) % 24 for h in ranked[:2] if hours[h] > 0]
            except Exception:
                logger.debug("comms stats failed for %s", contact_id,
                             exc_info=True)

        # Affect.
        if self._affect is not None:
            try:
                st = self._affect.get_state(contact_id) or {}
                if st.get("event_count"):
                    brief.affect_valence = round(
                        float(st.get("current_valence", 0.0)), 3)
                    brief.affect_trend = str(st.get("trend", "") or "")
                    if brief.affect_valence <= -0.3 or (
                            brief.affect_trend == "declining"):
                        brief.cautions.append(
                            "recent mood is negative; lead carefully")
            except Exception:
                pass

        # Rapport topics from shared facts.
        if self._facts is not None:
            try:
                facts = self._facts.list_facts(contact_id=contact_id, limit=50)
                rows = facts.get("facts", facts) if isinstance(facts, dict) else facts
                topics: Dict[str, int] = {}
                for f in rows or []:
                    text = str((f or {}).get("fact", ""))
                    for w in text.split():
                        w = w.strip(".,!?;:\"'()").lower()
                        if len(w) >= 5 and w.isalpha():
                            topics[w] = topics.get(w, 0) + 1
                brief.rapport_topics = [w for w, n in sorted(
                    topics.items(), key=lambda kv: -kv[1]) if n >= 2][:6]
            except Exception:
                pass

        # Psyche: the engagement extractor's profile + its guidance renderer.
        if self._engagement is not None:
            try:
                from colony_sidecar.tom.engagement import build_guidance
                prof = self._engagement.get_profile(contact_id) or {}
                guidance = build_guidance(prof)
                brief.psyche_guidance = [
                    ln.lstrip("- ").strip()
                    for ln in guidance.splitlines() if ln.strip()][:6]
                qual = prof.get("qual") or {}
                brief.psyche_motivators = list(
                    (qual.get("motivators") or [])[:4])
            except Exception:
                pass

        # Cadence caution: silent for much longer than their usual gap.
        try:
            if brief.interaction_count >= 5 and brief.last_interaction_at:
                from datetime import datetime, timezone as _tz
                last = datetime.fromisoformat(
                    brief.last_interaction_at.replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=_tz.utc)
                silent_days = (datetime.now(_tz.utc) - last).days
                if silent_days >= 30:
                    brief.cautions.append(
                        f"no contact in {silent_days} days")
        except Exception:
            pass

        self._save(brief)
        return brief

    async def refresh_due(self, *, limit: int = 20) -> Dict[str, Any]:
        """(Re)profile contacts that accrued enough new interactions since
        their last profile. Called by the autonomy phase."""
        report = {"profiled": 0, "skipped": 0, "errors": 0}
        min_new = profile_min_interactions()
        try:
            contacts = await self._contacts.list(limit=500)
        except Exception:
            logger.debug("profiler contact list failed", exc_info=True)
            return report
        done = 0
        for c in contacts or []:
            if done >= limit:
                break
            cid = getattr(c, "contact_id", None)
            n = int(getattr(c, "interaction_count", 0) or 0)
            if not cid or cid in _EXCLUDED_IDS or n < min_new:
                continue
            prior = self.cached(cid)
            if prior is not None and (
                    n - prior.interactions_at_profile) < min_new:
                report["skipped"] += 1
                continue
            try:
                if await self.profile(cid) is not None:
                    report["profiled"] += 1
                    done += 1
            except Exception:
                report["errors"] += 1
                logger.debug("profile failed for %s", cid, exc_info=True)
        return report

    @staticmethod
    def _tz_offset_hours(tz_name: str) -> int:
        if not tz_name:
            return 0
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            off = datetime.now(ZoneInfo(tz_name)).utcoffset()
            return int(off.total_seconds() // 3600) if off else 0
        except Exception:
            return 0
