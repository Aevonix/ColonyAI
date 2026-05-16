"""Owner check-in scheduler task for Colony autonomy.

When the initiative pipeline produces nothing for a sustained period,
the scheduler task detects the silence and emits a proactive_message
event to reach out to the owner.

State is persisted to disk so check-ins survive sidecar restarts.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from colony_sidecar.autonomy.config import AutonomyConfig
from colony_sidecar.events.types import Event

logger = logging.getLogger(__name__)

# State file for check-in persistence
_STATE_FILE = Path(os.environ.get("COLONY_STATE_DIR", str(Path.home() / ".colony" / "data"))) / "autonomy_checkin.json"


class CheckInState:
    """File-backed state for owner check-in timing."""

    def __init__(self, path: Path = _STATE_FILE):
        self._path = path
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        """Load persisted check-in state."""
        try:
            if self._path.exists():
                with open(self._path, "r") as f:
                    return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load check-in state: %s", exc)
        return {}

    def save(self, data: Dict[str, Any]) -> None:
        """Persist check-in state to disk."""
        try:
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            logger.warning("Failed to save check-in state: %s", exc)

    def get_last_check_in(self) -> Optional[datetime]:
        """Return the timestamp of the last check-in, if any."""
        raw = self.load().get("last_check_in_at")
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                pass
        return None

    def set_last_check_in(self, when: Optional[datetime] = None) -> None:
        """Record a check-in timestamp."""
        data = self.load()
        data["last_check_in_at"] = (when or datetime.now(timezone.utc)).isoformat()
        self.save(data)


class OwnerCheckInTask:
    """Scheduler task that detects initiative silence and checks in with the owner."""

    def __init__(
        self,
        registry: Any,
        config: AutonomyConfig,
        event_bus: Any,
        telemetry: Any,
    ) -> None:
        self._registry = registry
        self._config = config
        self._event_bus = event_bus
        self._telemetry = telemetry
        self._state = CheckInState()

    async def run(self) -> Dict[str, Any]:
        """Execute the check-in check. Called by the scheduler."""
        if not self._config.owner_check_in_enabled:
            return {"status": "skipped", "reason": "disabled"}

        # 1. Check initiative silence via telemetry
        if self._telemetry is None:
            return {"status": "skipped", "reason": "telemetry_not_available"}

        try:
            silence = await self._telemetry.silence_hours("initiative")
        except Exception as exc:
            logger.warning("Check-in: telemetry query failed: %s", exc)
            return {"status": "error", "reason": f"telemetry_error: {exc}"}

        if silence is None:
            # No initiatives have ever been recorded — consider as silent
            silence = float("inf")

        if silence < self._config.owner_check_in_silent_hours:
            return {
                "status": "ok",
                "silence_hours": round(silence, 2),
                "threshold": self._config.owner_check_in_silent_hours,
                "check_in": False,
            }

        # 2. Check cooldown
        last_check_in = self._state.get_last_check_in()
        if last_check_in is not None:
            cooldown_delta = timedelta(hours=self._config.owner_check_in_cooldown_hours)
            if datetime.now(timezone.utc) - last_check_in < cooldown_delta:
                return {
                    "status": "ok",
                    "silence_hours": round(silence, 2),
                    "check_in": False,
                    "reason": "cooldown",
                }

        # 3. Check quiet hours
        if self._in_quiet_hours():
            return {
                "status": "ok",
                "silence_hours": round(silence, 2),
                "check_in": False,
                "reason": "quiet_hours",
            }

        # 4. Resolve owner
        owner_id = await self._resolve_owner()
        if not owner_id:
            return {
                "status": "ok",
                "silence_hours": round(silence, 2),
                "check_in": False,
                "reason": "no_owner_resolved",
            }

        # 5. Emit proactive message
        ok = await self._emit_check_in(owner_id)
        if ok:
            self._state.set_last_check_in()
            return {
                "status": "ok",
                "silence_hours": round(silence, 2),
                "check_in": True,
                "owner_id": owner_id,
            }

        return {
            "status": "error",
            "silence_hours": round(silence, 2),
            "check_in": False,
            "reason": "emit_failed",
        }

    def _in_quiet_hours(self) -> bool:
        """Determine if current time falls within quiet hours."""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self._config.timezone)
            now = datetime.now(tz)
            current = now.hour * 60 + now.minute

            start_parts = self._config.quiet_hours_start.split(":")
            end_parts = self._config.quiet_hours_end.split(":")
            start = int(start_parts[0]) * 60 + int(start_parts[1])
            end = int(end_parts[0]) * 60 + int(end_parts[1])

            if start == end:
                return False
            if start < end:
                return start <= current < end
            return current >= start or current < end
        except Exception:
            return False

    async def _resolve_owner(self) -> Optional[str]:
        """Resolve the owner contact ID from identity or fallback."""
        # Explicit override from config
        if self._config.owner_contact_id:
            return self._config.owner_contact_id

        # Try identity manager
        identity = getattr(self._registry, "identity", None)
        if identity is not None:
            self_id = getattr(identity, "self_contact_id", None)
            if self_id:
                return self_id

        # Fallback: highest-scored contact with interaction history
        contacts = getattr(self._registry, "contacts", None)
        if contacts is not None and hasattr(contacts, "list_contacts"):
            try:
                all_contacts = contacts.list_contacts(limit=50)
                # Filter to contacts with at least one interaction and non-stranger tier
                scored = []
                for c in all_contacts:
                    tier = getattr(c, "trust_tier", "stranger")
                    if tier == "stranger":
                        continue
                    score = getattr(c, "relationship_score", 0.0)
                    last_at = getattr(c, "last_interaction_at", None)
                    if last_at is not None:
                        scored.append((c, score))
                if scored:
                    scored.sort(key=lambda x: x[1], reverse=True)
                    return getattr(scored[0][0], "contact_id", None)
            except Exception as exc:
                logger.warning("Check-in: contact resolution failed: %s", exc)

        return None

    async def _emit_check_in(self, owner_id: str) -> bool:
        """Push a check-in initiative directly to the delivery bridge."""
        delivery = getattr(self._registry, "delivery", None)
        if delivery is None:
            logger.warning("Check-in: delivery bridge not available")
            return False

        try:
            payload = {
                "id": f"checkin-{datetime.now(timezone.utc).isoformat()}",
                "type": "proactive_message",
                "channel_hint": "dm",
                "priority": 0.5,
                "title": "Owner check-in",
                "description": (
                    "Nothing urgent has surfaced recently. "
                    "Do you need anything, or should I keep monitoring?"
                ),
                "rationale": "Autonomy loop silence detected",
                "suggested_action": "notify_user",
                "entity_id": owner_id,
                "entity_type": "owner",
                "context": {},
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

            ok = await delivery.push_initiative(payload)
            if ok:
                logger.info("Check-in: pushed initiative to %s", owner_id)
                # Also emit event for audit trail
                if self._event_bus is not None:
                    event = Event(
                        id=payload["id"],
                        event_type="proactive_message",
                        person_id=owner_id,
                        payload={
                            "content": payload["description"],
                            "source": "autonomy_check_in",
                            "delivered": True,
                        },
                    )
                    if hasattr(self._event_bus, "emit_async"):
                        await self._event_bus.emit_async(event)
                    else:
                        self._event_bus.emit(event)
            return ok
        except Exception as exc:
            logger.error("Check-in: failed to push initiative: %s", exc)
            return False
