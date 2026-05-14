"""Example Colony initiative polling hook for Hermes Gateway.

Place this at ~/.hermes/hooks/colony-initiatives/handler.py
It polls Colony /v1/host/initiatives every 60s and fires unseen pending
initiatives to the Hermes webhook.

Key design:
- Payload is wrapped as {"type": "initiative", "payload": {...}} so the
  webhook template can use {__raw__} without fragility.
- Colony URL is read from environment (COLONY_URL) instead of hardcoded.
"""

import asyncio
import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Configuration — read from environment for flexibility across deployments
COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
COLONY_API_KEY = os.environ.get("COLONY_API_KEY", "dev-mode-no-key")
WEBHOOK_URL = "http://127.0.0.1:8644/webhooks/colony-initiatives"
POLL_INTERVAL = 60.0

_seen_ids: set[str] = set()
_task: asyncio.Task | None = None


async def _poll_loop() -> None:
    """Background loop: poll Colony initiatives and fire to webhook."""
    logger.info("[colony-hook] Initiative polling started")
    while True:
        try:
            await _check_and_fire()
        except Exception as exc:
            logger.warning("[colony-hook] Poll cycle error: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)


async def _check_and_fire() -> None:
    """Fetch pending initiatives and POST unseen ones to the webhook."""
    headers = {"Authorization": f"Bearer {COLONY_API_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{COLONY_URL}/v1/host/initiatives", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    initiatives = data.get("initiatives", []) if isinstance(data, dict) else data
    pending = [i for i in initiatives if i.get("status") == "pending"]

    fired = 0
    for initiative in pending:
        iid = initiative.get("id")
        if not iid or iid in _seen_ids:
            continue
        _seen_ids.add(iid)

        # Wrap payload so webhook template can use {__raw__} without fragility
        payload = {
            "type": "initiative",
            "payload": {
                "initiative_type": initiative.get("initiative_type", ""),
                "title": initiative.get("title", ""),
                "description": initiative.get("description", ""),
                "priority": initiative.get("priority", 0),
                "status": initiative.get("status", ""),
                "id": iid,
                "dedup_key": initiative.get("dedup_key", ""),
                "context": initiative.get("context", {}),
                "created_at": initiative.get("created_at", ""),
                "expires_at": initiative.get("expires_at", ""),
            },
            "occurred_at": initiative.get("created_at", ""),
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                wh_resp = await client.post(
                    WEBHOOK_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                wh_resp.raise_for_status()
                fired += 1
                logger.info(
                    "[colony-hook] Fired initiative %s (%s) to webhook",
                    iid,
                    initiative.get("title", "")[:40],
                )
        except Exception as exc:
            logger.warning("[colony-hook] Failed to fire initiative %s: %s", iid, exc)

    if fired:
        logger.info("[colony-hook] Fired %d/%d pending initiatives", fired, len(pending))


async def handle(event_type: str, context: dict[str, Any]) -> None:
    """Gateway hook entrypoint."""
    global _task
    if event_type == "gateway:startup":
        if _task is None or _task.done():
            _task = asyncio.create_task(_poll_loop())
            logger.info("[colony-hook] Polling task created")
