"""Cognition trigger — spawns an OpenClaw subagent for background thinking.

Colony does not run its own LLM. It sends cognition requests to OpenClaw
via the subagent spawn API, which handles model routing, concurrency,
and token budgeting.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from colony_sidecar.cognition.prompt import COGNITION_SYSTEM_PROMPT, build_cognition_prompt

logger = logging.getLogger(__name__)

# Module-level throttle state
_last_trigger_time: float = 0.0
_last_trigger_session: Optional[str] = None


def _cognition_enabled() -> bool:
    from colony_sidecar.util.autonomy_preset import resolve_bool
    return resolve_bool("COLONY_COGNITION_ENABLED", False)


def _cognition_model() -> Optional[str]:
    return os.environ.get("COLONY_COGNITION_MODEL") or None


def _throttle_seconds() -> int:
    try:
        return int(os.environ.get("COLONY_COGNITION_THROTTLE_SECONDS", "30"))
    except ValueError:
        return 30


def _update_throttle(now: float, session_id: str) -> None:
    global _last_trigger_time, _last_trigger_session
    _last_trigger_time = now
    _last_trigger_session = session_id


async def trigger_cognition(
    trigger_type: str,
    context: Dict[str, Any],
    priority: str = "normal",
) -> Dict[str, Any]:
    """Fire a cognition trigger by spawning an OpenClaw subagent.

    This is called from the API endpoint. It builds the cognition prompt,
    checks throttling, and delegates to OpenClaw's sessions_spawn.

    Args:
        trigger_type: turn_sync, signal_ingest, anomaly, or manual.
        context: Trigger-specific context data.
        priority: high, normal, or low.

    Returns:
        Dict with accepted status and optional throttle info.
    """
    if not _cognition_enabled():
        return {
            "accepted": False,
            "message": "Cognition substrate is disabled",
            "throttle_seconds": None,
        }

    model = _cognition_model()
    if not model:
        return {
            "accepted": False,
            "message": "COLONY_COGNITION_MODEL not configured",
            "throttle_seconds": None,
        }

    # Throttle check (skip for high priority)
    throttle_secs = _throttle_seconds()
    now = time.time()
    if priority != "high" and (now - _last_trigger_time) < throttle_secs:
        remaining = int(throttle_secs - (now - _last_trigger_time))
        return {
            "accepted": True,
            "message": f"Throttled — queued for {remaining}s",
            "throttle_seconds": remaining,
        }

    # Get existing commitments for context (avoid duplicates)
    existing = []
    person_id = context.get("person_id")
    if person_id:
        try:
            from colony_sidecar.api.routers.host import _commitment_store
            if _commitment_store is not None:
                existing = _commitment_store.get_pending_for_person(person_id)
        except Exception:
            logger.debug("Failed to fetch existing commitments for cognition", exc_info=True)

    # Build the prompt
    user_prompt = build_cognition_prompt(
        trigger_type=trigger_type,
        context=context,
        existing_commitments=existing,
    )

    # Update throttle timestamp
    session_id = context.get("session_id", "")
    _update_throttle(now, session_id)

    # Emit cognition.requested event for the plugin to pick up
    _emit_cognition_event(trigger_type, user_prompt, model, priority)

    return {
        "accepted": True,
        "message": "Cognition trigger fired",
        "throttle_seconds": None,
    }


def _emit_cognition_event(
    trigger_type: str,
    prompt: str,
    model: str,
    priority: str,
) -> None:
    """Emit a cognition.requested event carrying a full spawn spec.

    This is an OPTIONAL integration point: no shipped consumer spawns a
    dedicated cognition session from it today (the hermes plugin only caches
    events as context blurbs). The working per-turn judgment path is the
    inline introspection in cognition/introspection.py; a deployment that
    wants a real tool-restricted cognition session can subscribe to this
    event and honor system_prompt/model/tools_allow. See docs/KNOWN-GAPS.md.
    """
    try:
        from colony_sidecar.events.broadcaster import emit
        emit("cognition.requested", {
            "trigger_type": trigger_type,
            "system_prompt": COGNITION_SYSTEM_PROMPT,
            "user_prompt": prompt,
            "model": model,
            "priority": priority,
            # Names match the registered agent tools so a consumer can pass
            # this straight through as an allowlist.
            "tools_allow": [
                "colony_create_commitment",
                "colony_list_commitments",
                "colony_resolve_commitment",
            ],
        })
    except Exception:
        logger.debug("Failed to emit cognition.requested event", exc_info=True)
