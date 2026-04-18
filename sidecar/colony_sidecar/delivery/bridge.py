"""ProactiveDeliveryBridge — queues and manages proactive message delivery.

The autonomy loop calls deliver() with an initiative or insight. The bridge:
1. Rate-limits per person
2. Queues the message in pending deliveries
3. The gateway polls GET /v1/delivery/pending and sends via platform adapters

Delivery channels:
  PUSH       → deliver immediately (queued for gateway polling)
  IN_SESSION → store for injection into next conversation's system prompt
  DIGEST     → accumulate for bundled morning briefing (not yet implemented)
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from colony_sidecar.delivery.rate_limiter import DeliveryRateLimiter

logger = logging.getLogger(__name__)

# Default internal port for the gateway's /internal/deliver endpoint.
_DEFAULT_GATEWAY_INTERNAL_PORT = 7779


@dataclass
class PendingDelivery:
    """A proactive message waiting to be sent to a user."""
    delivery_id: str
    person_id: str
    content: str
    channel: str          # "push" | "in_session" | "digest"
    urgency: float
    source: str           # "initiative" | "insight" | "anomaly"
    initiative_id: Optional[str]
    queued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sent: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class ProactiveDeliveryBridge:
    """Routes autonomy loop outputs (initiatives, insights) to users via the gateway.

    Two delivery paths are supported:

    1. **Poll path** (default): The gateway polls /v1/delivery/pending every few
       seconds and POSTs each pending delivery to the appropriate platform adapter.

    2. **Push path** (when gateway_url is set): ``push_to_gateway()`` POSTs
       directly to the gateway's internal ``POST /internal/deliver`` endpoint so
       messages are delivered immediately without polling latency.
    """

    def __init__(
        self,
        rate_limiter: Optional[DeliveryRateLimiter] = None,
        gateway_url: Optional[str] = None,
        gateway_api_key: Optional[str] = None,
    ) -> None:
        self._rate_limiter = rate_limiter or DeliveryRateLimiter()
        self._pending: List[PendingDelivery] = []
        self._sent: List[PendingDelivery] = []  # short history for observability
        self._sent_max: int = 500  # cap to prevent unbounded growth

        # Gateway push path — optional direct delivery via /internal/deliver
        _port = int(os.environ.get("COLONY_GATEWAY_INTERNAL_PORT", _DEFAULT_GATEWAY_INTERNAL_PORT))
        self._gateway_url: str = (
            gateway_url
            or os.environ.get("COLONY_GATEWAY_INTERNAL_URL", "")
            or f"http://localhost:{_port}"
        )
        self._gateway_api_key: str = (
            gateway_api_key
            or os.environ.get("COLONY_API_KEY", "")
        )

        # Home channel config read from env vars — used to resolve
        # platform/chat_id when only person_id is available.
        self._home_channels: Dict[str, Dict[str, str]] = self._load_home_channels()

    # ------------------------------------------------------------------
    # Home channel resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _load_home_channels() -> Dict[str, Dict[str, str]]:
        """Build a {platform: {chat_id, name}} mapping from env vars."""
        channels: Dict[str, Dict[str, str]] = {}
        env_map = {
            "telegram": ("TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_NAME"),
            "whatsapp": ("WHATSAPP_HOME_CHANNEL", "WHATSAPP_HOME_CHANNEL_NAME"),
            "discord": ("DISCORD_HOME_CHANNEL", "DISCORD_HOME_CHANNEL_NAME"),
            "slack": ("SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_NAME"),
            "signal": ("SIGNAL_HOME_CHANNEL", "SIGNAL_HOME_CHANNEL_NAME"),
        }
        for platform, (chat_env, name_env) in env_map.items():
            chat_id = os.environ.get(chat_env, "")
            if chat_id:
                channels[platform] = {
                    "chat_id": chat_id,
                    "name": os.environ.get(name_env, platform.title()),
                }
        return channels

    def resolve_home_channel(self) -> Optional[Dict[str, str]]:
        """Return the first configured home channel as {platform, chat_id}."""
        for platform, info in self._home_channels.items():
            return {"platform": platform, "chat_id": info["chat_id"]}
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deliver(
        self,
        person_id: str,
        content: str,
        channel: str = "push",
        urgency: float = 0.5,
        source: str = "initiative",
        initiative_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Queue a proactive message for delivery.

        Returns the delivery_id if queued, None if rate-limited.
        """
        allowed, reason = self._rate_limiter.can_deliver(person_id, urgency=urgency)
        if not allowed:
            logger.debug(
                "Proactive delivery blocked for %s: %s (urgency=%.2f)",
                person_id,
                reason,
                urgency,
            )
            return None

        delivery = PendingDelivery(
            delivery_id=str(uuid.uuid4()),
            person_id=person_id,
            content=content,
            channel=channel,
            urgency=urgency,
            source=source,
            initiative_id=initiative_id,
            metadata=metadata or {},
        )
        self._pending.append(delivery)
        logger.info(
            "Proactive delivery queued: %s → %s (channel=%s, urgency=%.2f)",
            delivery.delivery_id,
            person_id,
            channel,
            urgency,
        )
        return delivery.delivery_id

    async def push_to_gateway(
        self,
        platform: str,
        chat_id: str,
        message: str,
        source: str = "initiative",
    ) -> bool:
        """Push a proactive message directly to the gateway's /internal/deliver endpoint.

        Returns True if the gateway accepted the message, False otherwise.
        The caller is responsible for prior rate-limit checks if needed.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not available — cannot push to gateway")
            return False

        url = f"{self._gateway_url.rstrip('/')}/internal/deliver"
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._gateway_api_key:
            headers["Authorization"] = f"Bearer {self._gateway_api_key}"

        payload = {
            "platform": platform,
            "chat_id": chat_id,
            "message": message,
            "source": source,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as resp:
                    if resp.status == 200:
                        logger.info(
                            "Proactive message pushed to gateway (platform=%s, chat_id=%s, source=%s)",
                            platform,
                            chat_id,
                            source,
                        )
                        return True
                    body = await resp.text()
                    logger.warning(
                        "Gateway /internal/deliver returned %d: %s",
                        resp.status,
                        body[:200],
                    )
                    return False
        except Exception as exc:
            logger.warning("push_to_gateway failed: %s", exc)
            return False

    def get_pending(self, gateway_id: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        """Return pending PUSH deliveries for the gateway to send.

        Only returns unsent PUSH channel deliveries. IN_SESSION deliveries are
        fetched separately via get_in_session_context().
        """
        results = []
        for d in self._pending:
            if d.sent:
                continue
            if d.channel != "push":
                continue
            results.append(self._to_dict(d))
            if len(results) >= limit:
                break
        return results

    def mark_sent(self, delivery_id: str) -> bool:
        """Mark a delivery as sent (called by gateway after successful send)."""
        for d in self._pending:
            if d.delivery_id == delivery_id:
                d.sent = True
                self._rate_limiter.record_delivery(d.person_id)
                self._sent.append(d)
                if len(self._sent) > self._sent_max:
                    self._sent = self._sent[-self._sent_max:]
                logger.info("Delivery %s marked sent (person=%s)", delivery_id, d.person_id)
                return True
        logger.debug("mark_sent: delivery %s not found", delivery_id)
        return False

    def get_in_session_context(self, person_id: str) -> Optional[str]:
        """Return pending IN_SESSION deliveries formatted for prompt injection.

        Marks them as consumed after returning.
        """
        in_session = [
            d for d in self._pending
            if d.person_id == person_id and d.channel == "in_session" and not d.sent
        ]
        if not in_session:
            return None

        lines = ["[Things to mention this session]"]
        for d in in_session:
            lines.append(f"• {d.content}")
            d.sent = True
            self._rate_limiter.record_delivery(d.person_id)

        return "\n".join(lines)

    def purge_sent(self) -> int:
        """Remove sent deliveries from the pending queue. Returns count purged."""
        before = len(self._pending)
        self._pending = [d for d in self._pending if not d.sent]
        return before - len(self._pending)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(d: PendingDelivery) -> Dict[str, Any]:
        return {
            "delivery_id": d.delivery_id,
            "person_id": d.person_id,
            "content": d.content,
            "channel": d.channel,
            "urgency": d.urgency,
            "source": d.source,
            "initiative_id": d.initiative_id,
            "queued_at": d.queued_at.isoformat(),
            "metadata": d.metadata,
        }
