"""Colony Briefing System — delivery gateways and delivery engine.

Implements the gateway abstraction and concrete iMessage, Telegram,
and API gateways with retry logic and failure handling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Briefing, BriefingPriority, BriefingStatus

logger = logging.getLogger(__name__)

_IMESSAGE_MAX_CHARS = 3000
_TELEGRAM_MAX_CHARS = 4096

_PRIORITY_EMOJI = {
    BriefingPriority.LOW: "🔵",
    BriefingPriority.NORMAL: "⚪",
    BriefingPriority.HIGH: "🟡",
    BriefingPriority.URGENT: "🔴",
}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class DeliveryResult:
    success: bool
    gateway: str
    briefing_id: str
    error: Optional[str] = None
    delivered_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Abstract gateway
# ---------------------------------------------------------------------------


class BriefingGateway(ABC):
    """Abstract delivery gateway for briefings."""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def send(self, briefing: Briefing, formatted: str) -> DeliveryResult: ...

    def format(self, briefing: Briefing) -> str:
        """Plain-text fallback formatter."""
        lines = [
            f"[{briefing.briefing_type.value.upper()} BRIEFING] {briefing.briefing_id[:8]}",
            f"Priority: {briefing.priority.value}",
            "",
        ]
        for section in briefing.active_sections():
            lines.append(f"--- {section.name.upper()} ---")
            if section.narrative:
                lines.append(section.narrative)
            else:
                lines.append(str(section.content))
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# iMessage gateway
# ---------------------------------------------------------------------------


class IMessageBriefingGateway(BriefingGateway):
    """Deliver briefings via iMessage."""

    def __init__(
        self,
        imessage_client: Optional[Any] = None,
        recipient_handle: str = "",
        rich_formatting: bool = True,
        quiet_hours: Optional[tuple[int, int]] = None,  # (start_hour, end_hour)
    ) -> None:
        self._client = imessage_client
        self._recipient = recipient_handle
        self._rich = rich_formatting
        self._quiet_hours = quiet_hours

    def name(self) -> str:
        return "imessage"

    def is_available(self) -> bool:
        return self._client is not None and bool(self._recipient)

    def _in_quiet_hours(self) -> bool:
        if not self._quiet_hours:
            return False
        now_hour = datetime.now().hour
        start, end = self._quiet_hours
        if start <= end:
            return start <= now_hour < end
        return now_hour >= start or now_hour < end

    def format(self, briefing: Briefing) -> str:
        emoji = _PRIORITY_EMOJI.get(briefing.priority, "⚪")
        lines = [f"{emoji} *Colony {briefing.briefing_type.value.title()} Briefing*", ""]
        for section in briefing.active_sections():
            header = f"**{section.name.replace('_', ' ').title()}**" if self._rich else section.name.upper()
            lines.append(header)
            if section.narrative:
                lines.append(section.narrative)
            else:
                lines.append(_dict_to_text(section.content))
            lines.append("")
        text = "\n".join(lines)
        if len(text) > _IMESSAGE_MAX_CHARS:
            trailer = "\n... [see Colony for full briefing]"
            text = text[: _IMESSAGE_MAX_CHARS - len(trailer)] + trailer
        return text

    def send(self, briefing: Briefing, formatted: str) -> DeliveryResult:
        if self._in_quiet_hours():
            return DeliveryResult(
                success=False,
                gateway=self.name(),
                briefing_id=briefing.briefing_id,
                error="quiet hours",
            )
        try:
            self._client.send(self._recipient, formatted)
            return DeliveryResult(
                success=True,
                gateway=self.name(),
                briefing_id=briefing.briefing_id,
                delivered_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            return DeliveryResult(
                success=False,
                gateway=self.name(),
                briefing_id=briefing.briefing_id,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Telegram gateway
# ---------------------------------------------------------------------------


class TelegramBriefingGateway(BriefingGateway):
    """Deliver briefings via Telegram bot."""

    def __init__(
        self,
        telegram_client: Optional[Any] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self._client = telegram_client
        self._chat_id = chat_id

    def name(self) -> str:
        return "telegram"

    def is_available(self) -> bool:
        return self._client is not None and bool(self._chat_id)

    def format(self, briefing: Briefing) -> str:
        emoji = _PRIORITY_EMOJI.get(briefing.priority, "⚪")
        lines = [f"{emoji} *Colony {briefing.briefing_type.value.title()} Briefing*", ""]
        for section in briefing.active_sections():
            lines.append(f"*{section.name.replace('_', ' ').title()}*")
            if section.narrative:
                lines.append(section.narrative)
            else:
                lines.append(_dict_to_text(section.content))
            lines.append("")
        return "\n".join(lines)

    def send(self, briefing: Briefing, formatted: str) -> DeliveryResult:
        # Split long messages
        chunks = _split_message(formatted, _TELEGRAM_MAX_CHARS)
        try:
            for chunk in chunks:
                self._client.send_message(self._chat_id, chunk, parse_mode="Markdown")
            return DeliveryResult(
                success=True,
                gateway=self.name(),
                briefing_id=briefing.briefing_id,
                delivered_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            return DeliveryResult(
                success=False,
                gateway=self.name(),
                briefing_id=briefing.briefing_id,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# API gateway
# ---------------------------------------------------------------------------


class APIBriefingGateway(BriefingGateway):
    """Store briefings for API retrieval — no push delivery."""

    def __init__(self, store: Optional[Any] = None) -> None:
        self._store = store

    def name(self) -> str:
        return "api"

    def is_available(self) -> bool:
        return True  # Always available

    def send(self, briefing: Briefing, formatted: str) -> DeliveryResult:
        # Mark briefing as delivered; API clients poll to retrieve it
        if self._store:
            self._store.mark_delivered(briefing.briefing_id, self.name())
        return DeliveryResult(
            success=True,
            gateway=self.name(),
            briefing_id=briefing.briefing_id,
            delivered_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# WhatsApp gateway
# ---------------------------------------------------------------------------


class WhatsAppBriefingGateway(BriefingGateway):
    """Deliver briefings via WhatsApp using ProactiveDeliveryBridge.push_to_gateway()."""

    def __init__(
        self,
        delivery_bridge: Optional[Any] = None,  # ProactiveDeliveryBridge
        chat_id: Optional[str] = None,
    ) -> None:
        self._bridge = delivery_bridge
        self._chat_id = chat_id or os.environ.get("WHATSAPP_HOME_CHANNEL", "")

    def name(self) -> str:
        return "whatsapp"

    def is_available(self) -> bool:
        return self._bridge is not None and bool(self._chat_id)

    def format(self, briefing: Briefing) -> str:
        emoji = _PRIORITY_EMOJI.get(briefing.priority, "⚪")
        lines = [f"{emoji} *Colony {briefing.briefing_type.value.title()} Briefing*", ""]
        for section in briefing.active_sections():
            lines.append(f"*{section.name.replace('_', ' ').title()}*")
            if section.narrative:
                lines.append(section.narrative)
            else:
                lines.append(_dict_to_text(section.content))
            lines.append("")
        return "\n".join(lines)

    def send(self, briefing: Briefing, formatted: str) -> DeliveryResult:
        try:
            success = self._push(formatted)
            if success:
                return DeliveryResult(
                    success=True,
                    gateway=self.name(),
                    briefing_id=briefing.briefing_id,
                    delivered_at=datetime.now(timezone.utc),
                )
            return DeliveryResult(
                success=False,
                gateway=self.name(),
                briefing_id=briefing.briefing_id,
                error="gateway rejected delivery",
            )
        except Exception as exc:
            return DeliveryResult(
                success=False,
                gateway=self.name(),
                briefing_id=briefing.briefing_id,
                error=str(exc),
            )

    def _push(self, message: str) -> bool:
        """Synchronous wrapper around the async push_to_gateway call."""
        coro = self._bridge.push_to_gateway(
            platform="whatsapp",
            chat_id=self._chat_id,
            message=message,
            source="briefing",
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (the scheduler's worker thread — the normal
            # path) — safe to call asyncio.run() directly.
            return asyncio.run(coro)
        # We ARE on the event-loop thread. The old run_coroutine_threadsafe +
        # blocking result() here scheduled onto THIS loop and then blocked it:
        # guaranteed deadlock until the 10s timeout, then a failed delivery.
        # Run the coroutine on its own loop in a worker thread instead —
        # still a synchronous wait (this method's contract), but it completes.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(asyncio.run, coro).result(timeout=15.0)


# ---------------------------------------------------------------------------
# Delivery engine
# ---------------------------------------------------------------------------

_RETRY_DELAYS = [1.0, 4.0, 16.0]


class BriefingDeliveryEngine:
    """Route briefings to the appropriate gateway with retry logic."""

    def __init__(
        self,
        gateways: Optional[List[BriefingGateway]] = None,
        default_gateway: str = "api",
    ) -> None:
        self._gateways: Dict[str, BriefingGateway] = {}
        for gw in gateways or []:
            self._gateways[gw.name()] = gw
        self._default = default_gateway

    def register(self, gateway: BriefingGateway) -> None:
        self._gateways[gateway.name()] = gateway

    def deliver(
        self,
        briefing: Briefing,
        gateway_name: Optional[str] = None,
    ) -> DeliveryResult:
        """Deliver a briefing, retrying up to 3 times with exponential backoff.

        For URGENT briefings that fail on the primary gateway, attempts all
        remaining available gateways.
        """
        name = gateway_name or briefing.gateway or self._default
        gw = self._gateways.get(name)

        if gw is None or not gw.is_available():
            # Try fallbacks
            gw = self._find_available_gateway(exclude=name)

        if gw is None:
            return DeliveryResult(
                success=False,
                gateway=name,
                briefing_id=briefing.briefing_id,
                error="no available gateway",
            )

        formatted = gw.format(briefing)
        result = self._deliver_with_retry(gw, briefing, formatted)

        # For critical briefings: try all gateways on failure
        if not result.success and briefing.priority == BriefingPriority.URGENT:
            for fallback in self._gateways.values():
                if fallback.name() == gw.name() or not fallback.is_available():
                    continue
                fb_formatted = fallback.format(briefing)
                fb_result = self._deliver_with_retry(fallback, briefing, fb_formatted)
                if fb_result.success:
                    return fb_result

        return result

    def _deliver_with_retry(
        self,
        gw: BriefingGateway,
        briefing: Briefing,
        formatted: str,
    ) -> DeliveryResult:
        last_result: Optional[DeliveryResult] = None
        for attempt, delay in enumerate([0.0] + _RETRY_DELAYS):
            if delay > 0:
                time.sleep(delay)
            last_result = gw.send(briefing, formatted)
            if last_result.success:
                return last_result
            logger.warning(
                "Delivery attempt %d failed for briefing %s via %s: %s",
                attempt + 1,
                briefing.briefing_id,
                gw.name(),
                last_result.error,
            )
        return last_result  # type: ignore[return-value]

    def _find_available_gateway(self, exclude: str = "") -> Optional[BriefingGateway]:
        for gw in self._gateways.values():
            if gw.name() != exclude and gw.is_available():
                return gw
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dict_to_text(d: Any) -> str:
    if isinstance(d, dict):
        parts = []
        for k, v in d.items():
            parts.append(f"{k}: {v}")
        return " | ".join(parts)
    return str(d)


def _split_message(text: str, max_len: int) -> List[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
