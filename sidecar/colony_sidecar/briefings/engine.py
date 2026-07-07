"""BriefingEngine — top-level coordinator for the Colony Briefing System."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, List, Optional

from colony_sidecar.briefings.composer import BriefingComposer
from colony_sidecar.briefings.config import BriefingConfig
from colony_sidecar.briefings.delivery import BriefingDeliveryEngine
from colony_sidecar.briefings.engagement import SectionEngagementTracker
from colony_sidecar.briefings.enhancer import BriefingLMEnhancer
from colony_sidecar.briefings.models import (
    Briefing,
    BriefingCompletionSignal,
    BriefingStatus,
    BriefingType,
    SectionEngagementRecord,
)
from colony_sidecar.briefings.store import BriefingStore


class BriefingEngine:
    """Top-level coordinator for generating, delivering, and tracking briefings."""

    def __init__(
        self,
        config: Optional[BriefingConfig] = None,
        store: Optional[BriefingStore] = None,
        delivery: Optional[BriefingDeliveryEngine] = None,
        enhancer: Optional[BriefingLMEnhancer] = None,
        delivery_bridge: Optional[Any] = None,  # ProactiveDeliveryBridge
        relationship_aggregator: Optional[Any] = None,
        calendar_aggregator: Optional[Any] = None,
        goal_aggregator: Optional[Any] = None,
        anomaly_aggregator: Optional[Any] = None,
        mind_model_aggregator: Optional[Any] = None,
        synthesis_aggregator: Optional[Any] = None,
    ) -> None:
        self._config = config or BriefingConfig()
        self._store = store or BriefingStore(db_path=":memory:")
        self._delivery = delivery or BriefingDeliveryEngine(gateways=[], default_gateway="api")
        self._enhancer = enhancer or BriefingLMEnhancer(enabled=False)
        self._delivery_bridge = delivery_bridge
        # Aggregators default to stubs inside the composer; the real ones must
        # be passed through or every briefing data section is permanently empty.
        self._composer = BriefingComposer(
            relationship_aggregator=relationship_aggregator,
            calendar_aggregator=calendar_aggregator,
            goal_aggregator=goal_aggregator,
            anomaly_aggregator=anomaly_aggregator,
            mind_model_aggregator=mind_model_aggregator,
            synthesis_aggregator=synthesis_aggregator,
            suppressed_sections=self._config.suppressed_sections,
        )
        self._engagement = SectionEngagementTracker(store=self._store)
        self._scheduler = None

        # Register the WhatsApp gateway if a delivery bridge is available.
        if delivery_bridge is not None:
            from colony_sidecar.briefings.delivery import WhatsAppBriefingGateway
            self._delivery.register(WhatsAppBriefingGateway(delivery_bridge=delivery_bridge))

    # ------------------------------------------------------------------
    # Generation & Delivery
    # ------------------------------------------------------------------

    def generate_and_deliver(self, briefing_type: BriefingType) -> Briefing:
        """Generate a briefing of the given type, persist it, and deliver it.

        Raises ValueError for BriefingType.TACTICAL (use fire_tactical() instead).
        Engagement history is used to re-order sections by prior engagement score.
        If LM enhancement is enabled, the briefing is polished before delivery.

        Returns:
            The saved Briefing object after delivery attempt. The returned
            Briefing.status reflects the delivery outcome (DELIVERED or PENDING).
        """
        if briefing_type == BriefingType.TACTICAL:
            raise ValueError("Use fire_tactical() for tactical briefings")

        now = datetime.now(timezone.utc)
        scores = self._engagement.get_section_scores()

        if briefing_type == BriefingType.DAILY:
            cfg = self._config.daily
            briefing = self._composer.build_daily_briefing(
                date=now.strftime("%Y-%m-%d"),
                tz=cfg.timezone,
                max_sections=cfg.max_sections,
                section_order=cfg.section_order,
                engagement_history=scores,
            )
        else:  # WEEKLY
            cfg = self._config.weekly
            from datetime import timedelta
            week_start = now - timedelta(days=now.weekday())
            briefing = self._composer.build_weekly_briefing(
                period_start=week_start,
                period_end=now,
                engagement_history=scores,
            )

        if self._config.lm_enhancement_enabled:
            briefing = self._enhancer.enhance_briefing(briefing)

        # Resolve home channel gateway before persisting (Fix 8 point 1).
        if not briefing.gateway:
            briefing.gateway = self._resolve_home_channel()

        # draft → saved
        self._store.save(briefing)
        # draft → queued
        self._store.mark_queued(briefing.briefing_id)
        # queued → delivering
        self._store.mark_delivering(briefing.briefing_id)
        result = self._delivery.deliver(briefing)
        if result.success:
            # delivering → delivered
            self._store.mark_delivered(briefing.briefing_id, result.gateway)
            briefing = self._store.get(briefing.briefing_id)
        return briefing

    def fire_tactical(
        self,
        trigger: str,
        severity: str,
        summary: str,
        details: str,
        suggested_actions: Optional[List[str]] = None,
    ) -> Optional[Briefing]:
        """Fire a tactical (event-driven) briefing if the tactical channel is enabled.

        Returns None when tactical briefings are disabled in config, so callers
        must handle the None case. When enabled, saves and delivers immediately
        without engagement scoring.

        Returns:
            The delivered Briefing, or None if tactical is disabled.
        """
        if not self._config.tactical.enabled:
            return None

        briefing = self._composer.build_tactical_briefing(
            trigger=trigger,
            severity=severity,
            summary=summary,
            details=details,
            suggested_actions=suggested_actions,
        )
        if not briefing.gateway:
            briefing.gateway = self._resolve_home_channel()
        self._store.save(briefing)
        self._store.mark_queued(briefing.briefing_id)
        self._store.mark_delivering(briefing.briefing_id)
        result = self._delivery.deliver(briefing)
        if result.success:
            self._store.mark_delivered(briefing.briefing_id, result.gateway)
            briefing = self._store.get(briefing.briefing_id)
        return briefing

    # ------------------------------------------------------------------
    # Pending delivery (Fix 8 point 3 — pick up stalled drafts)
    # ------------------------------------------------------------------

    def deliver_pending(self) -> int:
        """Attempt delivery of any DRAFT or QUEUED briefings.

        Transitions each briefing through the state machine:
            draft/queued → delivering → delivered (or back to queued on failure).

        Returns the number of briefings successfully delivered.
        """
        count = 0
        for status in (BriefingStatus.DRAFT, BriefingStatus.QUEUED):
            for briefing in self._store.list_by_status(status):
                if not briefing.gateway:
                    briefing.gateway = self._resolve_home_channel()
                    # Persist the resolved gateway so the delivery engine uses it.
                    self._store.save(briefing)
                self._store.mark_delivering(briefing.briefing_id)
                result = self._delivery.deliver(briefing)
                if result.success:
                    self._store.mark_delivered(briefing.briefing_id, result.gateway)
                    count += 1
                    import logging as _logging
                    _logging.getLogger(__name__).info(
                        "deliver_pending: delivered briefing %s via %s",
                        briefing.briefing_id,
                        result.gateway,
                    )
                else:
                    # Revert to queued so the scheduler retries next cycle.
                    self._store.mark_queued(briefing.briefing_id)
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "deliver_pending: failed to deliver briefing %s: %s",
                        briefing.briefing_id,
                        result.error,
                    )
        return count

    def _resolve_home_channel(self) -> str:
        """Return the home channel platform from delivery_bridge or env vars."""
        if self._delivery_bridge is not None:
            home = self._delivery_bridge.resolve_home_channel()
            if home:
                return home["platform"]
        for platform in ("whatsapp", "telegram", "discord", "slack", "signal"):
            if os.environ.get(f"{platform.upper()}_HOME_CHANNEL"):
                return platform
        return "api"

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_pending(self) -> List[Briefing]:
        return self._store.list_by_status(BriefingStatus.DRAFT)

    def get_recent(self, limit: int = 10) -> List[Briefing]:
        return self._store.list_recent(limit=limit)

    # ------------------------------------------------------------------
    # Engagement
    # ------------------------------------------------------------------

    def record_engagement(self, briefing_id: str, section_name: str, signal: str) -> None:
        record = SectionEngagementRecord(
            section_name=section_name,
            briefing_id=briefing_id,
            signal=signal,
            recorded_at=datetime.now(timezone.utc),
        )
        self._engagement.record(record)
        if signal == "read":
            self._store.mark_read(briefing_id)

    def get_suppression_candidates(self, **kwargs) -> List[str]:
        return self._engagement.get_suppression_candidates(**kwargs)

    def suppress_section(self, section: str, reason: str = "") -> None:
        if section not in self._config.suppressed_sections:
            self._config.suppressed_sections.append(section)

    def emit_completion_signal(self, briefing: Briefing) -> BriefingCompletionSignal:
        scores = self._engagement.get_section_scores()
        active = briefing.active_sections()
        engaged = [s.name for s in active if scores.get(s.name, 0.5) > 0.5]
        return BriefingCompletionSignal(
            briefing_id=briefing.briefing_id,
            briefing_type=briefing.briefing_type,
            sections=[s.name for s in active],
            engaged_sections=engaged,
            delivered_at=datetime.now(timezone.utc),
            total_anomalies=0,
            active_goals=0,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attach_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def start(self) -> None:
        if self._scheduler is not None:
            self._scheduler.start()

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop()
