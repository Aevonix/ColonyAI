"""MonitoringHandler — health-check probe jobs."""

from __future__ import annotations

import ipaddress
import logging
import os
import time
from typing import Any, Dict
from urllib.parse import urlsplit

from colony_sidecar.task_queue.handlers.base import JobHandler, Job

logger = logging.getLogger(__name__)


def _host_allowlisted(host: str) -> bool:
    entries = {
        item.strip().lower()
        for item in os.environ.get(
            "COLONY_MONITORING_HOST_ALLOWLIST", ""
        ).split(",")
        if item.strip()
    }
    host = host.lower().rstrip(".")
    return host in entries or any(
        entry.startswith("*.") and host.endswith(entry[1:])
        for entry in entries
    )


def _unsafe_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return not address.is_global


async def _validate_endpoint(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("monitoring endpoint must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("monitoring endpoint cannot contain credentials")
    host = parsed.hostname.rstrip(".")
    try:
        address = str(ipaddress.ip_address(host))
    except ValueError:
        # DNS resolution followed by an ordinary HTTP client resolution is a
        # rebinding TOCTOU. Hostnames therefore require an exact/operator
        # wildcard allowlist entry; literal public IPs need no DNS at all.
        if not _host_allowlisted(host):
            raise ValueError(
                "monitoring hostname must be explicitly configured in "
                "COLONY_MONITORING_HOST_ALLOWLIST"
            )
        return url
    if _unsafe_address(address) and not _host_allowlisted(host):
        raise ValueError(
            "monitoring endpoint resolves to a non-public address; configure "
            "COLONY_MONITORING_HOST_ALLOWLIST for an intentional internal probe"
        )
    return url


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

        url = await _validate_endpoint(job.payload["endpoint"])
        expected = job.payload.get("expected_status", 200)
        timeout = job.payload.get("timeout_secs", 10.0)

        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False,
            ) as resp:
                latency_ms = (time.monotonic() - t0) * 1000
                alerts = []
                if resp.status != expected:
                    alerts.append(
                        f"Expected status {expected}, got {resp.status}"
                    )
                succeeded = not alerts
                return {
                    "status": "completed" if succeeded else "failed",
                    "summary": (
                        f"probe returned expected HTTP {resp.status}"
                        if succeeded else "; ".join(alerts)
                    ),
                    "action_plane": {
                        "state": "completed" if succeeded else "failed",
                    },
                    "metrics": {
                        "status": resp.status,
                        "latency_ms": round(latency_ms, 2),
                    },
                    "alerts": alerts,
                }
