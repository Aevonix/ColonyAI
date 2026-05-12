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
from pathlib import Path
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
        if rate_limiter is None:
            # Persist rate-limit state so a crashloop can't reset the daily
            # caps. Lives alongside other sidecar state under COLONY_STATE_DIR.
            state_dir = os.environ.get("COLONY_STATE_DIR", ".")
            db_path = Path(state_dir) / "colony-delivery-rate-limit.db"
            rate_limiter = DeliveryRateLimiter(db_path=db_path)
        self._rate_limiter = rate_limiter
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
        """Resolve the first configured home channel.
        
        Returns:
            Dict with platform, chat_id, account_id or None if not configured.
            Platform is normalized to lowercase OpenClaw channel name.
        """
        for platform, info in self._home_channels.items():
            return {
                "platform": platform.lower(),  # whatsapp, telegram, discord, slack, signal
                "chat_id": info["chat_id"],
                "account_id": "default",  # Could be made configurable later
            }
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

    async def push_initiative(self, initiative: Dict[str, Any]) -> bool:
        """Push a structured initiative to OpenClaw for LLM decision-making.
        
        Returns True if gateway accepted, False otherwise.
        """
        try:
            import aiohttp
        except ImportError:
            logger.warning("aiohttp not available — cannot push initiative")
            return False

        url = f"{self._gateway_url.rstrip('/')}/internal/initiative"
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._gateway_api_key:
            headers["Authorization"] = f"Bearer {self._gateway_api_key}"

        # Resolve home channel for delivery context
        home = self.resolve_home_channel()
        delivery_context = None
        if home:
            delivery_context = {
                "channel": home.get("platform"),
                "to": home.get("chat_id"),
                "accountId": home.get("account_id"),
            }

        payload = {
            "initiative": initiative,
            "source": "autonomy_loop",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "deliveryContext": delivery_context,
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
                            "Initiative pushed to gateway: %s (type=%s, priority=%.2f)",
                            initiative.get("id"),
                            initiative.get("type"),
                            initiative.get("priority", 0),
                        )
                        return True
                    body = await resp.text()
                    logger.warning(
                        "Gateway /internal/initiative returned %d: %s",
                        resp.status, body[:200]
                    )
                    # Fallback: broadcast via WebSocket so Hermes subscribers still receive it
                    self._broadcast_fallback(initiative)
                    return False
        except Exception as exc:
            logger.warning("push_initiative failed: %s", exc)
            # Fallback: broadcast via WebSocket so Hermes subscribers still receive it
            self._broadcast_fallback(initiative)
            return False

    def _broadcast_fallback(self, initiative: Dict[str, Any]) -> None:
        """Broadcast an initiative via WebSocket when HTTP push fails.

        This ensures Hermes and other WebSocket subscribers still receive
        the initiative even if the gateway's /internal/initiative endpoint
        is unavailable or unregistered.
        """
        try:
            from colony_sidecar.events.broadcaster import emit
            emit("initiative", initiative)
            logger.info("Initiative broadcast via WebSocket fallback: %s", initiative.get("id"))
        except Exception:
            logger.debug("WebSocket fallback broadcast failed for initiative %s", initiative.get("id"), exc_info=True)

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

    # ------------------------------------------------------------------
    # DIGEST channel
    # ------------------------------------------------------------------

    def get_pending_digest(self, person_id: str) -> List[PendingDelivery]:
        """Return all unsent DIGEST-channel deliveries for ``person_id``."""
        return [
            d for d in self._pending
            if d.person_id == person_id and d.channel == "digest" and not d.sent
        ]

    def pending_digest_recipients(self) -> List[str]:
        """List distinct ``person_id`` values with pending DIGEST items."""
        seen = set()
        ordered: List[str] = []
        for d in self._pending:
            if d.channel != "digest" or d.sent:
                continue
            if d.person_id in seen:
                continue
            seen.add(d.person_id)
            ordered.append(d.person_id)
        return ordered

    def build_digest_bundle(
        self,
        person_id: str,
        *,
        header: str = "Daily digest",
    ) -> Optional[str]:
        """Format this person's pending DIGEST items into a single bundled text
        block. Does not mark anything consumed — pair with ``consume_digest``.
        Items are sorted by urgency descending, then by queue time."""
        items = self.get_pending_digest(person_id)
        if not items:
            return None
        items = sorted(items, key=lambda d: (-d.urgency, d.queued_at))
        lines = [f"[{header}]"]
        for d in items:
            prefix = "\u203c" if d.urgency >= 0.8 else "\u2022"
            lines.append(f"{prefix} {d.content}")
        return "\n".join(lines)

    def consume_digest(self, person_id: str) -> int:
        """Mark all pending DIGEST deliveries for ``person_id`` as sent.

        Returns the count consumed. Each consumed item is also recorded
        against the rate limiter so the digest flush respects per-person
        caps consistently with the other channels.
        """
        consumed = 0
        for d in self._pending:
            if d.person_id == person_id and d.channel == "digest" and not d.sent:
                d.sent = True
                self._rate_limiter.record_delivery(d.person_id)
                self._sent.append(d)
                consumed += 1
        if consumed and len(self._sent) > self._sent_max:
            self._sent = self._sent[-self._sent_max:]
        return consumed

    async def flush_digests_to_gateway(
        self,
        *,
        platform: Optional[str] = None,
        chat_id: Optional[str] = None,
        header: str = "Daily digest",
    ) -> Dict[str, Any]:
        """Bundle each recipient's pending DIGEST items and push the bundle
        to the gateway.

        When ``platform``/``chat_id`` are omitted, the bridge falls back to
        the configured home channel (see ``resolve_home_channel``). If no
        home channel is configured, the flush is a no-op that returns
        ``{"sent": 0, "reason": "no_home_channel"}`` so a scheduler can
        still drain the item count at call time.

        Returns a summary dict: ``{"sent": N, "skipped": M, "recipients":
        [...], "reason": ...}``.
        """
        recipients = self.pending_digest_recipients()
        if not recipients:
            return {"sent": 0, "skipped": 0, "recipients": []}

        if not platform or not chat_id:
            home = self.resolve_home_channel()
            if home is None:
                return {
                    "sent": 0,
                    "skipped": len(recipients),
                    "recipients": recipients,
                    "reason": "no_home_channel",
                }
            platform = platform or home["platform"]
            chat_id = chat_id or home["chat_id"]

        sent = 0
        skipped = 0
        for person_id in recipients:
            bundle = self.build_digest_bundle(person_id, header=header)
            if not bundle:
                continue
            ok = await self.push_to_gateway(
                platform=platform,
                chat_id=chat_id,
                message=bundle,
                source="digest",
            )
            if ok:
                self.consume_digest(person_id)
                sent += 1
            else:
                skipped += 1
        return {
            "sent": sent,
            "skipped": skipped,
            "recipients": recipients,
        }

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
