#!/usr/bin/env python3
"""Colony digest — bundles completed/failed job summaries and messages owner.

Runs every 6 hours via cron. Queries the task queue digest endpoint and
pushes the result to the delivery bridge (or logs if no bridge).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("colony_digest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

COLONY_URL = os.environ.get("COLONY_URL", "http://127.0.0.1:7777")
API_TOKEN = os.environ.get("COLONY_AGENT_API_TOKEN", "")
_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"} if API_TOKEN else {}


def main() -> int:
    try:
        resp = httpx.get(
            f"{COLONY_URL}/v1/host/queue/digest",
            headers=_HEADERS,
            params={"hours": 6},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch digest: %s", exc)
        return 1

    completed = data.get("completed", [])
    failed = data.get("failed", [])

    if not completed and not failed:
        logger.info("No activity in last 6 hours")
        return 0

    lines = ["[Colony Digest — 6h]"]
    if completed:
        lines.append(f"✅ Completed ({len(completed)})")
        for item in completed:
            lines.append(f"  • {item}")
    if failed:
        lines.append(f"⚠️ Needs attention ({len(failed)})")
        for item in failed:
            lines.append(f"  • {item}")

    message = "\n".join(lines)
    logger.info("Digest:\n%s", message)

    # Push to delivery bridge via existing initiative endpoint
    try:
        payload = {
            "initiative_type": "PROACTIVE_MESSAGE",
            "title": "Colony Digest",
            "description": message,
            "priority": 50,
            "dedup_key": f"digest:{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H')}",
        }
        push_resp = httpx.post(
            f"{COLONY_URL}/v1/host/initiatives",
            headers=_HEADERS,
            json=payload,
            timeout=10,
        )
        push_resp.raise_for_status()
        logger.info("Digest pushed to delivery")
    except Exception as exc:
        logger.warning("Failed to push digest to delivery: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
