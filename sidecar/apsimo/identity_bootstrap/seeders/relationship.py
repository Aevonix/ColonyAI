"""Relationship / contacts seeder — creates self-contact in the contacts store."""

from __future__ import annotations

import json
import logging
import time
import secrets
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hdl_id() -> str:
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    return f"hdl-{ts}-{rand}"


class RelationshipSeeder:
    name = "contacts"

    async def seed(self, corpus: Any) -> None:
        """Seed the self-contact into the contacts store."""
        colony_id = corpus.colony_id
        contact_id = f"self:{colony_id}"
        now = _now_iso()

        try:
            import colony.api.routers.contacts as contacts_mod
        except ImportError:
            logger.debug("contacts: router not importable — skipping")
            return

        store = getattr(contacts_mod, "_contact_store", None)

        if store is not None:
            await self._seed_sqlite(store, contact_id, corpus, now)
        else:
            self._seed_memory(contacts_mod, contact_id, corpus, now)

    async def _seed_sqlite(self, store: Any, contact_id: str, corpus: Any, now: str) -> None:
        """Upsert self-contact via the store's aiosqlite connection."""
        db = getattr(store, "_db", None)
        if db is None:
            logger.debug("contacts: store._db is None — falling back to memory")
            return

        colony_id = corpus.colony_id
        tags_json = json.dumps(["self", "system", "colony"])

        try:
            # Use aiosqlite's async execute
            await db.execute(
                """
                INSERT INTO contacts
                    (contact_id, display_name, given_name, family_name, organization,
                     relationship_score, trust_tier, interaction_allowed,
                     tags_json, privacy_level, person_node_id, notes, import_source,
                     first_seen_at, last_interaction_at, interaction_count,
                     enrichment_source, enrichment_last_at, deleted_at,
                     created_at, updated_at)
                VALUES
                    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(contact_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    trust_tier = excluded.trust_tier
                """,
                (
                    contact_id,
                    corpus.colony_name,
                    "Colony",
                    "Self",
                    "Colony AI",
                    1.0,
                    "inner_circle",
                    1,
                    tags_json,
                    "private",
                    f"colony-self-{colony_id}",
                    "Auto-seeded by identity bootstrap",
                    "identity_bootstrap",
                    now,
                    now,
                    0,
                    json.dumps(["identity_bootstrap"]),
                    now,
                    None,
                    now,
                    now,
                ),
            )
            await db.commit()
        except Exception as exc:
            logger.warning("contacts: sqlite upsert failed: %s", exc)
            return

        # Add internal handle
        hdl_id = f"hdl-self-{colony_id[:8]}"
        try:
            await db.execute(
                """
                INSERT INTO contact_handles
                    (handle_id, contact_id, gateway, address,
                     is_primary, verified, confidence, source, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(handle_id) DO NOTHING
                """,
                (hdl_id, contact_id, "custom", f"colony://{colony_id}", 1, 1, 1.0, "identity_bootstrap", now),
            )
            await db.commit()
        except Exception as exc:
            logger.debug("contacts: handle upsert failed: %s", exc)

        logger.info("contacts: self-contact seeded (contact_id=%s)", contact_id)

    def _seed_memory(self, contacts_mod: Any, contact_id: str, corpus: Any, now: str) -> None:
        """Fallback: write to the in-memory _store dict."""
        in_mem = getattr(contacts_mod, "_store", None)
        if in_mem is None:
            logger.debug("contacts: no _store fallback available")
            return

        in_mem[contact_id] = {
            "contact_id": contact_id,
            "display_name": corpus.colony_name,
            "given_name": "Colony",
            "family_name": "Self",
            "organization": "Colony AI",
            "relationship_score": 1.0,
            "trust_tier": "inner_circle",
            "interaction_allowed": True,
            "tags": ["self", "system", "colony"],
            "privacy_level": "private",
            "person_node_id": f"colony-self-{corpus.colony_id}",
            "notes": "Auto-seeded by identity bootstrap",
            "import_source": "identity_bootstrap",
            "first_seen_at": now,
            "last_interaction_at": now,
            "interaction_count": 0,
            "enrichment_source": ["identity_bootstrap"],
            "enrichment_last_at": now,
            "deleted_at": None,
            "created_at": now,
            "updated_at": now,
        }
        logger.info("contacts: self-contact seeded (in-memory, contact_id=%s)", contact_id)
