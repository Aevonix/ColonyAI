"""MonitoringHandler — health-check probe jobs."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from colony_sidecar.task_queue.handlers.base import JobHandler, Job

logger = logging.getLogger(__name__)


class MonitoringHandler(JobHandler):
    """Run a health-check probe.

    Job payload:
        endpoint (str): HTTP URL to GET.
        expected_status (int, default 200): Expected HTTP status.
        timeout_secs (float, default 10): Per-request timeout.

    Returns:
        {"metrics": {"status": int, "latency_ms": float}, "alerts": list[str]}
    """

    async def execute(self, job: Job) -> Dict[str, Any]:
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError(
                "MonitoringHandler requires 'aiohttp'. Install with: pip install aiohttp"
            ) from exc

        url = job.payload["endpoint"]
        expected = job.payload.get("expected_status", 200)
        timeout = job.payload.get("timeout_secs", 10.0)

        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                latency_ms = (time.monotonic() - t0) * 1000
                alerts = []
                if resp.status != expected:
                    alerts.append(
                        f"Expected status {expected}, got {resp.status}"
                    )
                return {
                    "metrics": {
                        "status": resp.status,
                        "latency_ms": round(latency_ms, 2),
                    },
                    "alerts": alerts,
                }
