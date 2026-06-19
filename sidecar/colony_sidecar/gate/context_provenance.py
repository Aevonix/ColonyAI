"""Context provenance — the data layer behind the cross-context leak check.

Records which (normalized) entities have appeared in which *conversation context*, so a
response about to go to one context can be flagged for surfacing an entity that is known
ONLY from a different, private context. This is the durable, populated replacement for the
old gate's dead in-memory per-session entity sets.

Generic by design: a ``conversation_key`` is any opaque string the embedding host uses to
identify a context (e.g. a platform conversation id). Colony attaches no meaning to it.

Privacy model:
- An entity recorded with ``is_public=True`` in any context is never treated as a leak
  (public knowledge: place names, public figures, etc.).
- An entity present in the *current* conversation's provenance is fine (it belongs here).
- An entity present only in *other* private conversations, surfaced in a reply to this one,
  is the leak we flag.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from colony_sidecar.gate.response_guard import CrossContextGuard, GuardFinding


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def normalize_entity(name: str) -> str:
    return unicodedata.normalize("NFKC", name or "").lower().strip()


class ContextProvenanceStore:
    """SQLite-backed index of entity -> conversation-context provenance."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS context_entities (
                entity_norm      TEXT NOT NULL,
                conversation_key TEXT NOT NULL,
                contact_id       TEXT,
                is_public        INTEGER NOT NULL DEFAULT 0,
                first_seen       TEXT NOT NULL,
                last_seen        TEXT NOT NULL,
                count            INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (entity_norm, conversation_key)
            );
            CREATE INDEX IF NOT EXISTS idx_ctx_entities_norm ON context_entities(entity_norm);
            """
        )
        self._conn.commit()

    def record(
        self,
        conversation_key: str,
        entities: Sequence[str],
        *,
        contact_id: Optional[str] = None,
        is_public: bool = False,
        min_len: int = 3,
        at: Optional[str] = None,
    ) -> int:
        """Record that ``entities`` appeared in ``conversation_key``. Returns the count kept."""
        if not conversation_key:
            return 0
        now = at or _now()
        kept = 0
        for raw in entities or ():
            n = normalize_entity(raw)
            if len(n) < min_len:
                continue
            self._conn.execute(
                "INSERT INTO context_entities "
                "(entity_norm, conversation_key, contact_id, is_public, first_seen, last_seen, count) "
                "VALUES (?,?,?,?,?,?,1) "
                "ON CONFLICT(entity_norm, conversation_key) DO UPDATE SET "
                "last_seen=excluded.last_seen, count=count+1, "
                "is_public=MAX(is_public, excluded.is_public)",
                (n, conversation_key, contact_id, 1 if is_public else 0, now, now),
            )
            kept += 1
        self._conn.commit()
        return kept

    def contexts_for(self, entity: str) -> List[dict]:
        """Contexts where this entity has appeared: [{conversation_key, is_public}]."""
        n = normalize_entity(entity)
        if not n:
            return []
        rows = self._conn.execute(
            "SELECT conversation_key, is_public FROM context_entities WHERE entity_norm=?",
            (n,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


class ProvenanceCrossContextGuard(CrossContextGuard):
    """Flags a reply that surfaces an entity known only from a different private context.

    ``extractor`` (optional ``ConversationExtractor``) pulls entities from the response text
    itself, so the host need only pass the text; any ``mentioned_entities`` passed in are
    merged on top. Entities introduced earlier in the *same* conversation (recorded under the
    same ``conversation_key`` on inbound) are never flagged.
    """

    def __init__(self, store: ContextProvenanceStore, extractor=None,
                 public_entities: Optional[Sequence[str]] = None) -> None:
        self._store = store
        self._extractor = extractor
        self._public = {normalize_entity(e) for e in (public_entities or ())}

    async def _entities_in(self, response_text: str, mentioned_entities: Sequence[str]) -> set:
        ents = {normalize_entity(e) for e in (mentioned_entities or ())}
        if self._extractor is not None and response_text:
            try:
                res = await self._extractor.extract(response_text, "response_guard")
                ents |= {normalize_entity(getattr(c, "text", None) or getattr(c, "name", ""))
                         for c in getattr(res, "entities", [])}
            except Exception:
                pass
        return {e for e in ents if e}

    async def check(self, *, response_text: str, conversation_key: Optional[str],
                    mentioned_entities: Sequence[str]) -> List[GuardFinding]:
        if not conversation_key:
            return []
        findings: List[GuardFinding] = []
        for ent in await self._entities_in(response_text, mentioned_entities):
            if ent in self._public:
                continue
            contexts = self._store.contexts_for(ent)
            if not contexts:
                continue                                   # never seen -> can't be a leak
            if any(c["is_public"] for c in contexts):
                continue                                   # public knowledge
            keys = {c["conversation_key"] for c in contexts}
            if conversation_key in keys:
                continue                                   # belongs to this conversation
            findings.append(GuardFinding(
                check="cross_context",
                severity="block",
                reason=f"entity {ent!r} is known only from another private conversation",
                excerpt=f"[{ent}]",
            ))
        return findings
