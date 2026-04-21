"""ConditionWorker — poll external conditions on behalf of blocked goals.

Registered as a task queue worker handler. When a goal blocks on an
external condition (email reply, deployment health, etc.), the goal engine
schedules a check_condition job. This module handles it.

When the condition is met, the worker calls goal_engine.unblock_goal().
When not yet met, it returns condition_met=False and the caller may
reschedule.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default polling intervals (seconds) by condition type
DEFAULT_INTERVALS: Dict[str, int] = {
    "email_reply": 5 * 60,       # 5 minutes
    "deployment_health": 30,      # 30 seconds
    "delivery_status": 60 * 60,  # 1 hour
    "api_response": 60,          # 1 minute
    "custom": 5 * 60,            # 5 minutes (fallback)
}


async def handle_check_condition(
    payload: Dict[str, Any],
    goal_engine: Any,
) -> Dict[str, Any]:
    """Execute a single condition check and report to the goal engine.

    Returns a result dict with:
      - condition_met: bool
      - goal_id: str
      - details: dict (condition-specific)

    If condition is not yet met, returns condition_met=False.
    The loop's _phase_goals() picks up any newly unblocked goals on the
    next tick.
    """
    goal_id = payload["goal_id"]
    condition_type = payload["condition_type"]
    condition_params = payload.get("condition_params", {})

    condition_checkers = {
        "email_reply": _check_email_reply,
        "deployment_health": _check_deployment_health,
        "delivery_status": _check_delivery_status,
        "api_response": _check_api_response,
    }

    checker = condition_checkers.get(condition_type)
    if not checker:
        logger.warning("Unknown condition type %r for goal %s", condition_type, goal_id)
        return {"condition_met": False, "goal_id": goal_id, "details": {}}

    try:
        result = await checker(condition_params)
    except NotImplementedError:
        logger.debug("Condition checker %r not yet implemented", condition_type)
        return {"condition_met": False, "goal_id": goal_id, "details": {}}
    except Exception as exc:
        logger.error(
            "Condition check %r failed for goal %s: %s",
            condition_type,
            goal_id,
            exc,
        )
        return {"condition_met": False, "goal_id": goal_id, "details": {"error": str(exc)}}

    if result.get("condition_met"):
        try:
            goal_engine.unblock_goal(goal_id)
            logger.info("Condition met for goal %s — unblocked", goal_id)
        except Exception as exc:
            logger.warning("Could not unblock goal %s: %s", goal_id, exc)

    return {
        "condition_met": result.get("condition_met", False),
        "goal_id": goal_id,
        "details": result,
    }


def get_check_interval(condition_type: str, deadline: Optional[datetime] = None) -> int:
    """Return appropriate polling interval in seconds.

    If deadline is within 1 hour, returns 60s regardless of type.
    """
    if deadline is not None:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining < 3600:
            return 60

    return DEFAULT_INTERVALS.get(condition_type, DEFAULT_INTERVALS["custom"])


# ---------------------------------------------------------------------------
# Condition checker implementations
# ---------------------------------------------------------------------------

async def _check_deployment_health(params: dict) -> dict:
    """Check if a deployment endpoint returns the expected HTTP status.

    params:
        endpoint (str): HTTP URL to GET.
        expected_status (int, default 200): Expected HTTP status code.
        timeout_secs (float, default 10): Per-request timeout.

    Returns:
        {"condition_met": bool, "status_code": int, "latency_ms": float}
    """
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError(
            "_check_deployment_health requires 'aiohttp'. "
            "Install with: pip install aiohttp"
        ) from exc

    import time

    endpoint = params["endpoint"]
    expected = params.get("expected_status", 200)
    timeout = params.get("timeout_secs", 10.0)

    t0 = time.monotonic()
    async with aiohttp.ClientSession() as session:
        async with session.get(
            endpoint, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            latency_ms = (time.monotonic() - t0) * 1000
            return {
                "condition_met": resp.status == expected,
                "status_code": resp.status,
                "latency_ms": round(latency_ms, 2),
            }


async def _check_api_response(params: dict) -> dict:
    """Check if an API endpoint returns an expected field value.

    params:
        url (str): URL to request.
        method (str, default "GET"): HTTP method.
        headers (dict, optional): Request headers.
        body (dict, optional): JSON body (for POST/PUT).
        expected_field (str, optional): Dot-separated JSON path, e.g. "data.status".
        expected_value (Any, optional): Value to compare against.
        timeout_secs (float, default 30): Per-request timeout.

    Returns:
        {"condition_met": bool, "field_value": Any}
    """
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError(
            "_check_api_response requires 'aiohttp'. "
            "Install with: pip install aiohttp"
        ) from exc

    method = params.get("method", "GET").upper()
    url = params["url"]
    headers = params.get("headers", {})
    body = params.get("body")
    expected_field = params.get("expected_field")
    expected_value = params.get("expected_value")
    timeout = params.get("timeout_secs", 30.0)

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method,
            url,
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json()

    # Traverse dot-separated field path
    field_value = data
    if expected_field:
        for key in expected_field.split("."):
            if isinstance(field_value, dict):
                field_value = field_value.get(key)
            else:
                field_value = None
                break

    return {
        "condition_met": field_value == expected_value,
        "field_value": field_value,
    }


async def _check_email_reply(params: dict) -> dict:
    """Check if a reply has arrived to a specific email thread via IMAP.

    params:
        thread_id (str, optional): Message-ID to match in In-Reply-To/References.
        expected_sender (str, optional): Match on from address (substring).
        subject_pattern (str, optional): Regex to match subject line.
        since_iso (str, optional): ISO timestamp — only consider messages after this.

    Credentials are loaded from SecretsManager:
        COLONY_IMAP_HOST, COLONY_IMAP_PORT, COLONY_IMAP_USERNAME, COLONY_IMAP_PASSWORD

    Returns:
        {"condition_met": bool, "message_id": str|None, "from": str|None}
    """
    import re
    from datetime import datetime

    try:
        from colony_sidecar.secrets.manager import SecretsManager
        from colony_sidecar.email.providers import IMAPProvider
    except ImportError:
        logger.debug("email_reply check skipped — IMAP provider not installed")
        return {
            "condition_met": False,
            "message_id": None,
            "from": None,
            "details": {"unavailable": "imap_provider_not_installed"},
        }

    sm = SecretsManager()
    host = sm.get_required("COLONY_IMAP_HOST")
    port = int(sm.get("COLONY_IMAP_PORT") or "993")
    username = sm.get_required("COLONY_IMAP_USERNAME")
    password = sm.get_required("COLONY_IMAP_PASSWORD")

    since_str = params.get("since_iso")
    since = datetime.fromisoformat(since_str) if since_str else None

    provider = IMAPProvider(
        host=host, port=port, username=username, password=password
    )
    entries = provider.fetch_recent(limit=50, since=since)

    thread_id = params.get("thread_id", "")
    expected_sender = params.get("expected_sender", "")
    subject_pattern = params.get("subject_pattern")

    for entry in entries:
        if expected_sender and expected_sender.lower() not in entry.from_address.lower():
            continue
        if subject_pattern and not re.search(
            subject_pattern, entry.subject or "", re.IGNORECASE
        ):
            continue
        # thread_id match: check external_id (Message-ID) as a best-effort proxy
        if thread_id and thread_id not in (entry.external_id or ""):
            continue
        return {
            "condition_met": True,
            "message_id": entry.external_id,
            "from": entry.from_address,
        }

    return {"condition_met": False, "message_id": None, "from": None}


async def _check_delivery_status(params: dict) -> dict:
    """Check carrier delivery status for a tracking number.

    params:
        tracking_number (str): Carrier tracking number.
        carrier (str): "ups" | "fedex"

    Carrier API keys are loaded from SecretsManager:
        COLONY_UPS_API_KEY, COLONY_FEDEX_API_KEY

    Returns:
        {"condition_met": bool, "status": str, "last_update": str|None}
    """
    try:
        from colony_sidecar.secrets.manager import SecretsManager
    except ImportError as exc:
        raise RuntimeError(
            "_check_delivery_status requires colony.secrets"
        ) from exc

    sm = SecretsManager()
    carrier = params.get("carrier", "").lower()
    tracking_number = params["tracking_number"]

    CARRIER_CONFIGS: dict = {
        "ups": {
            "url": f"https://onlinetools.ups.com/track/v1/details/{tracking_number}",
            "key_secret": "COLONY_UPS_API_KEY",
            "delivered_statuses": {"D"},
            "status_path": "trackResponse.shipment.0.activity.0.status.type",
        },
        "fedex": {
            "url": "https://apis.fedex.com/track/v1/trackingnumbers",
            "key_secret": "COLONY_FEDEX_API_KEY",
            "delivered_statuses": {"DL"},
            "status_path": (
                "output.completeTrackResults.0.trackResults"
                ".0.latestStatusDetail.code"
            ),
        },
    }

    config = CARRIER_CONFIGS.get(carrier)
    if not config:
        raise ValueError(
            f"Unsupported carrier: {carrier!r}. "
            f"Supported: {sorted(CARRIER_CONFIGS)}"
        )

    api_key = sm.get(config["key_secret"])
    if not api_key:
        raise KeyError(
            f"Missing carrier API key secret: {config['key_secret']}"
        )

    result = await _check_api_response({
        "url": config["url"],
        "method": "GET",
        "headers": {"Authorization": f"Bearer {api_key}"},
        "expected_field": config["status_path"],
        "expected_value": None,
        "timeout_secs": 15.0,
    })

    status_code = result.get("field_value")
    delivered = status_code in config["delivered_statuses"]
    return {
        "condition_met": delivered,
        "status": str(status_code),
        "last_update": None,
    }
