"""Sessions seeder — records the bootstrap system session."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionsSeeder:
    name = "sessions"

    async def seed(self, corpus: Any) -> None:
        colony_id = corpus.colony_id
        contact_id = f"self:{colony_id}"
        session_id = f"sess-bootstrap-{colony_id[:8]}"
        now = _now_iso()
        # 1 year expiry for bootstrap session
        expires_at_ts = time.time() + 86400 * 365

        try:
            import colony.api.routers.sessions as sessions_mod
        except ImportError:
            logger.debug("sessions: router not importable — skipping")
            return

        db_backend = getattr(sessions_mod, "_db_backend", None)

        if db_backend is not None:
            sess_dict = {
                "session_id": session_id,
                "contact_id": contact_id,
                "gateway": "system",
                "trust_tier": "inner_circle",
                "status": "active",
                "created_at": now,
                "closed_at": None,
                "expires_at_ts": expires_at_ts,
            }
            try:
                db_backend.save(sess_dict)
                logger.info("sessions: bootstrap session saved (id=%s)", session_id)
                return
            except Exception as exc:
                logger.warning("sessions: db_backend.save failed: %s", exc)

        # Fallback: in-memory store
        in_mem = getattr(sessions_mod, "_store", None)
        if in_mem is not None:
            in_mem[session_id] = {
                "session_id": session_id,
                "contact_id": contact_id,
                "gateway": "system",
                "trust_tier": "inner_circle",
                "status": "active",
                "created_at": now,
                "closed_at": None,
                "expires_at_ts": expires_at_ts,
            }
            logger.info("sessions: bootstrap session saved in-memory (id=%s)", session_id)
