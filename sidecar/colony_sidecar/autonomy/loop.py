"""AutonomyLoop — Colony's continuous operating cycle.

Wires existing subsystems into a coherent tick-based loop that runs
as a background asyncio task inside the sidecar. Each tick drains
events, checks goals, runs cognition, and executes initiatives.

Design principle: wire what exists. The loop is pure glue — it
orchestrates subsystems that are already wired in the sidecar.

State lives in Neo4j + SQLite. The loop is stateless and restartable.
Kill it at any point and it picks up cleanly on restart.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from colony_sidecar.autonomy.config import AutonomyConfig, AutonomyMode
from colony_sidecar.autonomy.registry import SubsystemRegistry
from colony_sidecar.events.bus import EventBus
from colony_sidecar.events.types import Event

# Lazy import to avoid circular dependency — broadcast_event is defined
# in the host router which imports from this module.
_broadcast = None


def _get_broadcast():
    global _broadcast
    if _broadcast is None:
        try:
            from colony_sidecar.api.routers.host import broadcast_event
            _broadcast = broadcast_event
        except ImportError:
            def _broadcast(e):
                return None
    return _broadcast

logger = logging.getLogger(__name__)


@dataclass
class LoopStats:
    """Lightweight counters updated each tick for observability."""

    ticks: int = 0
    events_processed: int = 0
    goals_checked: int = 0
    initiatives_generated: int = 0
    actions_executed: int = 0
    errors: int = 0
    actions_this_hour: int = 0
    hour_bucket: int = field(default_factory=lambda: datetime.now(timezone.utc).hour)
    skills_loaded: int = 0
    skills_evicted: int = 0
    signals_collected: int = 0
    scoring_runs: int = 0
    tier_changes: int = 0
    memories_promoted: int = 0
    task_follow_ups: int = 0
    scheduled_runs: int = 0

    def as_dict(self) -> dict:
        return {
            "ticks": self.ticks,
            "events_processed": self.events_processed,
            "goals_checked": self.goals_checked,
            "initiatives_generated": self.initiatives_generated,
            "actions_executed": self.actions_executed,
            "errors": self.errors,
            "actions_this_hour": self.actions_this_hour,
            "hour_bucket": self.hour_bucket,
            "skills_loaded": self.skills_loaded,
            "skills_evicted": self.skills_evicted,
            "signals_collected": self.signals_collected,
            "scoring_runs": self.scoring_runs,
            "tier_changes": self.tier_changes,
            "memories_promoted": self.memories_promoted,
            "task_follow_ups": self.task_follow_ups,
            "scheduled_runs": self.scheduled_runs,
        }


class AutonomyLoop:
    """Colony's continuous operating cycle.

    Takes a SubsystemRegistry and AutonomyConfig. The registry provides
    lazy access to all wired subsystems — if something isn't wired,
    the corresponding phase is a no-op.

    The loop does NOT auto-start. The host calls ``start()`` or uses
    the ``/v1/host/autonomy/start`` API endpoint.

    Each tick:
      1. Drain pending events
      2. Check goal engine for goals needing attention
      3. Check anomaly detections above severity threshold
      4. Run initiative engine — Colony decides whether to act
      5. Execute approved actions
      6. Run cognition pipeline tick
      7. Memory consolidation (hourly)
      8. Memory decay (daily)
      9. Memory pruning (weekly)
     10. Task completion follow-ups
     11. Frustration back-off update
     12. Bootstrap self-check (daily)
     13. Self-reflection (weekly)
     14. Relationship scoring
     15. Synthesis (connection discovery)
     16. Skill trigger evaluation + eviction
     17. Sleep until next tick or event wakes early
    """

    def __init__(
        self,
        registry: SubsystemRegistry,
        config: Optional[AutonomyConfig] = None,
        event_bus: Optional[EventBus] = None,
        scheduler: Any = None,
    ) -> None:
        self._registry = registry
        self.config = config or AutonomyConfig()
        self.events = event_bus or EventBus()
        self.stats = LoopStats()
        self._scheduler = scheduler

        self._running = False
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._wake_sub: Any = None
        self._pending_initiatives: List[Any] = []
        # Per-domain timestamps of the last observation-sync request, so
        # a slow agent isn't spammed with duplicate sync jobs every tick.
        self._last_sync_request: dict = {}
        self._last_consolidation_hour: int = -1
        self._last_bootstrap_check: Optional[datetime] = None
        self._last_self_reflection: Optional[datetime] = None
        self._last_decay_date: Optional[str] = None
        self._last_prune_date: Optional[str] = None
        self._last_reconcile_date: Optional[str] = None
        self._last_archive_week: Optional[str] = None
        self._last_distillation_week: str = ""
        self._last_task_completion_check: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the autonomy loop. Runs until stop() is called."""
        self._running = True
        self._stop_event.clear()

        # Fail loudly at startup if the owner identity is missing or
        # unresolvable (v0.16.0). Relationship generation fails closed at
        # tick time either way; this surfaces the misconfiguration once,
        # at CRITICAL, instead of letting it hide in per-tick noise.
        try:
            from colony_sidecar.identity.resolver import (
                OwnerIdentityError,
                get_identity_resolver,
            )
            await get_identity_resolver().owner_identities()
        except OwnerIdentityError as exc:
            logger.critical(
                "OWNER IDENTITY NOT RESOLVED — relationship initiative "
                "generation will be disabled until fixed: %s", exc,
            )
        except Exception as exc:
            logger.warning("Owner identity startup check failed: %s", exc)

        # Reactive mode: just mark as running, no timer
        if self.config.mode == AutonomyMode.REACTIVE:
            logger.info(
                "Autonomy loop started in REACTIVE mode (on-demand only, tz=%s)",
                self.config.timezone,
            )
            return

        # Proactive mode: start timer loop
        logger.info(
            "Autonomy loop starting in PROACTIVE mode (tick=%.0fs, quiet=%s-%s %s)",
            self.config.tick_interval_secs,
            self.config.quiet_hours_start,
            self.config.quiet_hours_end,
            self.config.timezone,
        )

        self._wake_sub = self.events.subscribe(
            handler=self._on_wake_signal,
            event_types=[Event],
        )

        try:
            while not self._stop_event.is_set():
                await self._tick()
                await self._sleep_until_next_tick()
        finally:
            if self._wake_sub is not None:
                self.events.unsubscribe(self._wake_sub)
            self._running = False
            logger.info("Autonomy loop stopped. Stats: %s", self.stats.as_dict())

    async def stop(self) -> None:
        """Signal the loop to stop after the current tick completes."""
        logger.info("Autonomy loop stop requested")
        self._stop_event.set()
        self._wake_event.set()

    def wake(self) -> None:
        """Wake the loop early from its sleep. Thread-safe."""
        self._wake_event.set()

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        self.stats.ticks += 1
        self._reset_hour_bucket()
        tick_start = datetime.now(timezone.utc)

        # Telemetry touch
        try:
            from colony_sidecar.api.routers.host import _telemetry
            if _telemetry is not None:
                await _telemetry.touch("last_tick_at")
        except Exception:
            logger.warning("Telemetry touch failed (non-critical)")

        logger.debug("Tick #%d starting", self.stats.ticks)

        # Phase 0: evaluate skill triggers
        event_text = self._gather_event_text()
        await self._phase_skill_triggers(event_text)

        # Phase 1: drain pending events
        await self._phase_events()

        # Phase 2: check goals needing attention
        await self._phase_goals()

        # Phase 3: check anomalies
        await self._phase_anomalies()

        # Phase 4: scheduled periodic tasks (memory consolidate, briefing, etc.)
        await self._phase_scheduled()

        # Phase 5: run initiative engine
        await self._phase_initiative()

        # Phase 5b: self-directed thinking (v0.17.0) — novel work the
        # data-reactive generators can't see. Appends to the same
        # pending-initiative batch Phase 6 consumes.
        await self._phase_thinking()

        # Phase 6: execute approved actions
        await self._phase_execute()

        # Phase 6b: request fresh observations for stale domains (v0.16.0)
        await self._phase_observation_sync()

        # Phase 6c: feed completed agent work back into memory (v0.17.0)
        await self._phase_job_writeback()

        # Phase 7: cognition pipeline tick
        await self._phase_cognition()

        # Phase 8: memory consolidation (hourly)
        await self._phase_memory_consolidation()

        # Phase 9: memory decay (daily)
        await self._phase_memory_decay()

        # Phase 10: memory reconciliation (daily)
        await self._phase_memory_reconciliation()

        # Phase 11: memory pruning (weekly)
        await self._phase_memory_pruning()

        # Phase 12: memory archive (weekly)
        await self._phase_memory_archive()

        # Phase 13: memory distillation (weekly)
        await self._phase_memory_distillation()

        # Phase 12: task completion follow-ups
        await self._phase_task_completion()

        # Phase 13: frustration back-off
        await self._phase_frustration_update()

        # Phase 14: relationship scoring
        await self._phase_relationships()

        # Phase 15: synthesis
        await self._phase_synthesis()

        # Phase 16: bootstrap self-check (daily)
        await self._phase_bootstrap_check()

        # Phase 17: self-reflection (weekly)
        await self._phase_self_reflection()

        # Phase 18: skill eviction
        await self._phase_skill_evict()

        # === Multi-Agent Phases (v0.7.0) ===

        # Phase 19: startup re-push (first tick only)
        await self._phase_startup_repush()

        # Phase 20: agent heartbeat (every tick)
        await self._phase_agent_heartbeat()

        # Phase 21: initiative timeout (every tick)
        await self._phase_initiative_timeout()

        # Phase 21b: owner-approval timeout for blocked jobs (every tick)
        await self._phase_approval_timeout()

        # Phase 21: stale initiative cleanup (every 5 ticks)
        if self.stats.ticks % 5 == 0:
            await self._phase_stale_initiative_cleanup()

        # Phase 22: ghost agent cleanup (every 10 ticks)
        if self.stats.ticks % 10 == 0:
            await self._phase_ghost_cleanup()

        # Phase 23: database backup (every 100 ticks)
        if self.stats.ticks % 100 == 0:
            await self._phase_database_backup()

        elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
        logger.debug("Tick #%d complete in %.2fs", self.stats.ticks, elapsed)

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    async def _phase_events(self) -> None:
        """Drain recent events and feed relevant ones to initiative engine."""
        try:
            recent = self.events.get_history(limit=50)
            new_count = len(recent)
            if new_count:
                for event in recent:
                    event_type = getattr(event, "event_type", None)
                    if event_type in ("message_received", "message_sent", "gateway_signal"):
                        getattr(event, "person_id", None)
            self.stats.events_processed += new_count
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase events error: %s", exc, exc_info=True)

    async def _phase_goals(self) -> None:
        """Check goal engine for goals needing attention."""
        goals = self._registry.goals
        if goals is None:
            return
        try:
            blocked = goals.list_goals(status="blocked", limit=20) if hasattr(goals, "list_goals") else []
            accepted = goals.list_goals(status="accepted", limit=20) if hasattr(goals, "list_goals") else []
            active = goals.list_goals(status="active", limit=50) if hasattr(goals, "list_goals") else []

            for goal in accepted:
                try:
                    if hasattr(goals, "activate_goal"):
                        goals.activate_goal(goal.get("goal_id", goal.get("id")))
                        logger.info("Loop activated goal: %r", goal.get("title"))
                except Exception as exc:
                    logger.warning("Failed to activate goal: %s", exc)

            total = len(blocked) + len(accepted) + len(active)
            self.stats.goals_checked += total
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase goals error: %s", exc, exc_info=True)

    async def _phase_anomalies(self) -> None:
        """Check anomaly detector for signals above severity threshold."""
        try:
            detector = self._registry.anomalies
            if detector is None:
                return
            if hasattr(detector, "detect"):
                recent_anomalies = await detector.detect(
                    threshold=self.config.anomaly_severity_threshold,
                )
            elif hasattr(detector, "get_recent"):
                recent_anomalies = detector.get_recent(
                    min_severity=self.config.anomaly_severity_threshold,
                    limit=20,
                )
            else:
                return

            if recent_anomalies:
                logger.info("Phase anomalies: %d above threshold", len(recent_anomalies))
                _get_broadcast()({
                    "type": "anomaly",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "payload": {"count": len(recent_anomalies)},
                })
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase anomalies error: %s", exc, exc_info=True)

    async def _phase_scheduled(self) -> None:
        """Run scheduled periodic tasks that are due (cron-style)."""
        scheduler = self._scheduler or self._registry.scheduler
        if scheduler is None:
            return
        try:
            results = await scheduler.tick()
            if results:
                ok = sum(1 for r in results if r.get("status") == "ok")
                self.stats.scheduled_runs += ok
                errs = len(results) - ok
                if errs:
                    self.stats.errors += errs
                    for r in results:
                        if r.get("status") != "ok":
                            logger.warning(
                                "Scheduled task failed: %s — %s",
                                r.get("task"), r.get("error"),
                            )
                if ok:
                    logger.info("Phase scheduled: %d task(s) ran", ok)
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase scheduled error: %s", exc, exc_info=True)

    async def _phase_initiative(self) -> None:
        """Run initiative engine to generate autonomous action proposals."""
        engine = self._registry.initiative_engine
        if engine is None:
            return

        try:
            engine.clear_context()
            await self._feed_pending_tasks(engine)
            await self._feed_neglected_contacts(engine)
            await self._feed_commitment_reminders(engine)

            initiatives = await engine.generate(
                min_priority=self.config.initiative_confidence_threshold,
                cooldown_tasks=float(os.environ.get(
                    "COLONY_INITIATIVE_COOLDOWN_TASKS", "12",
                )),
                cooldown_contacts=float(os.environ.get(
                    "COLONY_INITIATIVE_COOLDOWN_CONTACTS", "72",
                )),
            )

            if self._in_quiet_hours():
                initiatives = [i for i in initiatives if getattr(i, "priority", 0) >= 0.9]

            # Deferred initiatives queued by later phases of the previous
            # tick (e.g. skill-capture reviews from Phase 6c).
            deferred = getattr(self, "_deferred_initiatives", None)
            if deferred:
                initiatives = list(initiatives) + deferred
                self._deferred_initiatives = []

            if initiatives:
                logger.info("Phase initiative: %d new proposals", len(initiatives))
            self._pending_initiatives = initiatives
            self.stats.initiatives_generated += len(initiatives)

            # Capture context for payload building in _phase_execute
            self._last_initiative_context = dict(getattr(engine, "_context", {}))
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase initiative error: %s", exc, exc_info=True)
            self._pending_initiatives = []

    async def _phase_thinking(self) -> None:
        """Phase 5b: self-directed thinking (v0.17.0).

        On a slow cadence (COLONY_THINKING_INTERVAL_SECS), hand the LLM a
        situation report and let it propose novel initiatives the
        data-reactive generators can't see. Results join the same
        pending batch Phase 6 stores/delivers, so they inherit identical
        dedup, quiet-hours, rate-limit, and approval treatment.
        Disabled unless COLONY_ENABLE_INTERNAL_THINKING=true.
        """
        if os.environ.get("COLONY_ENABLE_INTERNAL_THINKING",
                          "false").lower() != "true":
            return
        router = self._registry.llm_router
        if router is None:
            return
        thinker = getattr(self, "_thinker", None)
        if thinker is None:
            from colony_sidecar.intelligence.components.self_directed_thinker import (
                SelfDirectedThinker,
            )
            thinker = SelfDirectedThinker(router)
            self._thinker = thinker
        if not thinker.due():
            return
        thinker.mark_ran()
        try:
            situation = self._build_thinking_situation()
            initiatives = await thinker.think(situation)
            if initiatives:
                self._pending_initiatives = list(
                    self._pending_initiatives or []) + initiatives
                self.stats.initiatives_generated += len(initiatives)
                logger.info("Phase thinking: %d novel proposal(s)",
                            len(initiatives))
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase thinking error: %s", exc, exc_info=True)

    def _build_thinking_situation(self) -> dict:
        """Assemble the situation report for the thinking phase."""
        situation: dict = {}
        ctx = dict(getattr(self, "_last_initiative_context", {}) or {})
        for key in ("pending_tasks", "neglected_contacts",
                    "commitment_reminders"):
            if ctx.get(key):
                situation[key] = list(ctx[key])[:10]

        goals = self._registry.goals
        if goals is not None and hasattr(goals, "list_goals"):
            try:
                situation["active_goals"] = goals.list_goals(
                    status="active", limit=20)
                situation["blocked_goals"] = goals.list_goals(
                    status="blocked", limit=10)
            except Exception:
                pass

        pending = getattr(self, "_pending_initiatives", None) or []
        situation["current_initiatives"] = [
            getattr(i, "description", "") for i in pending][:20]

        # Agent capability awareness (v0.18.0): the plugin reports the
        # Hermes skill index into the "skills" observation domain, so the
        # thinker proposes work the agent can actually do — and spots
        # genuine skill gaps instead of guessing.
        try:
            from colony_sidecar.api.routers.observations import (
                get_observation_store,
            )
            obs_store = get_observation_store()
            if obs_store is not None:
                skills = obs_store.list("skills", limit=40)
                if skills:
                    situation["agent_skills"] = [
                        {"name": o.entity_id,
                         "description": str(
                             (o.payload or {}).get("description", ""))[:120]}
                        for o in skills]
        except Exception:
            pass
        return situation

    def _build_initiative_context(self, initiative: Any, type_value: str) -> dict:
        """Build a focused, per-initiative context dict.

        Instead of dumping the entire engine state (which leaks internal
        context like all pending tasks, all neglected contacts, etc.), we
        look up only the item relevant to this specific initiative.
        """
        raw_ctx = getattr(self, "_last_initiative_context", {})
        entity_id = getattr(initiative, "entity_id", None)
        desc = getattr(initiative, "description", "")

        if type_value == "follow_up":
            for item in raw_ctx.get("pending_tasks", []):
                if item.get("entity_id") == entity_id:
                    return {
                        "blocked_goal": {
                            "goal_id": entity_id,
                            "title": item.get("description", desc),
                            "days_pending": item.get("days_pending", 0),
                        }
                    }
            return {}

        if type_value == "relationship":
            for contact in raw_ctx.get("neglected_contacts", []):
                if contact.get("entity_id") == entity_id:
                    return {
                        "neglected_contact": {
                            "contact_id": entity_id,
                            "contact_name": contact.get("name"),
                            "days_since_contact": contact.get("days_since_contact", 0),
                            "relationship_score": contact.get("relationship_score"),
                        }
                    }
            return {}

        if type_value == "commitment":
            for c in raw_ctx.get("upcoming_commitments", []):
                if c.get("commitment_id") == entity_id:
                    return {
                        "commitment": {
                            "commitment_id": entity_id,
                            "commitment_text": c.get("description"),
                            "deadline": c.get("due_at"),
                            "status": c.get("status"),
                            "person_id": c.get("person_id"),
                            "hours_until_due": c.get("hours_until_due"),
                        }
                    }
            return {}

        if type_value == "scheduling":
            for slot in raw_ctx.get("scheduling_opportunities", []):
                if slot.get("description") == desc:
                    return {
                        "upcoming_commitment": {
                            "description": slot.get("description", ""),
                            "hours_until_due": 0,  # not stored in opportunity dict
                        }
                    }
            return {}

        if type_value == "health":
            for alert in raw_ctx.get("health_alerts", []):
                if alert.get("metric") == entity_id:
                    return {
                        "health_alert": {
                            "metric": entity_id,
                            "value": alert.get("value"),
                            "target": alert.get("target"),
                        }
                    }
            return {}

        if type_value == "capability_gap":
            for gap in raw_ctx.get("capability_gaps", []):
                if gap.get("id") == entity_id:
                    return {"capability_gap": gap}
            return {}

        if type_value == "knowledge_acquisition":
            for gap in raw_ctx.get("knowledge_gaps", []):
                if gap.get("id") == entity_id:
                    return {"knowledge_gap": gap}
            return {}

        if type_value == "behavioral_correction":
            for pattern in raw_ctx.get("behavioral_patterns", []):
                if pattern.get("id") == entity_id:
                    return {"behavioral_pattern": pattern}
            return {}

        return {}

    async def _phase_execute(self) -> None:
        """Execute self-initiatives in the sidecar, then push remaining to delivery."""
        engine = self._registry.initiative_engine
        delivery = self._registry.delivery

        for initiative in list(self._pending_initiatives):
            if self.stats.actions_this_hour >= self.config.max_actions_per_hour:
                logger.warning("Hourly action limit reached")
                break

            initiative_type = getattr(initiative, "type", "unknown")
            type_value = initiative_type.value if hasattr(initiative_type, "value") else str(initiative_type)

            is_self_initiative = type_value in {
                "subsystem_health", "data_quality", "operational",
                "capability_gap", "knowledge_acquisition", "behavioral_correction",
            }

            # Try auto-execute for self-initiatives
            if is_self_initiative and engine is not None:
                try:
                    exec_result = await engine.execute_initiative(initiative.id)
                    result_status = exec_result.get("status")
                    skill_result = exec_result.get("result")

                    if result_status == "executed" and skill_result == "auto_fixed":
                        self.stats.actions_executed += 1
                        self.stats.actions_this_hour += 1
                        logger.info("Auto-fixed initiative: %s", initiative.id)
                        continue  # Don't push to delivery

                    if result_status == "executed" and skill_result == "proposal_created":
                        # Still push to delivery, but mark as proposed
                        pass

                    if result_status in ("no_skill", "not_self_initiative"):
                        # No skill matched — push to delivery for human decision
                        pass
                except Exception as exc:
                    logger.error("Auto-execution failed for %s: %s", initiative.id, exc)

            # Build and push payload
            try:
                # Situational context snapshot (v0.16.0): persisted with the
                # initiative so the agent gets it over the REST API, not just
                # in push payloads. Carries the rationale and a capture
                # timestamp (volatile types check it against their TTL).
                initiative_context = self._build_initiative_context(initiative, type_value)
                trigger_data = getattr(initiative, "trigger_data", None)
                if trigger_data and not initiative_context:
                    initiative_context = dict(trigger_data)
                rationale = getattr(initiative, "rationale", "")
                if rationale:
                    initiative_context.setdefault("rationale", rationale)
                initiative_context.setdefault(
                    "context_captured_at",
                    datetime.now(timezone.utc).isoformat(),
                )

                # Persist initiative before dispatch so it survives restarts
                store = getattr(self._registry, "initiative_store", None)
                if store:
                    try:
                        loop = asyncio.get_event_loop()
                        create_call = functools.partial(
                            store.create,
                            type=type_value,
                            description=getattr(initiative, "description", ""),
                            priority=getattr(initiative, "priority", 0.5),
                            rationale=rationale,
                            action_hint=getattr(initiative, "action_hint", None),
                            entity_id=getattr(initiative, "entity_id", None),
                            dedup_key=getattr(initiative, "dedup_key", None),
                            context=initiative_context or None,
                            expires_at=getattr(initiative, "expires_at", None),
                            source_type=type_value,
                            created_by="autonomy_loop",
                        )
                        stored = await loop.run_in_executor(None, create_call)
                        # If dedup hit and initiative is still active, skip dispatch
                        original_id = getattr(initiative, "id", None)
                        if stored and stored.is_active and (original_id is None or stored.id != original_id):
                            logger.info("Initiative dedup hit, skipping dispatch: %s", stored.id)
                            continue
                        # Use the persisted id for the payload
                        initiative_id = stored.id if stored else getattr(initiative, "id", str(uuid.uuid4()))
                    except Exception as exc:
                        logger.error("Failed to persist initiative, skipping dispatch: %s", exc)
                        continue
                else:
                    initiative_id = getattr(initiative, "id", str(uuid.uuid4()))

                # --- v0.13.0: Route AGENT_ACTION initiatives to task queue ---
                action_hint = getattr(initiative, "action_hint", None) or ""
                is_agent_action = (
                    type_value == "agent_action"
                    or action_hint.startswith("agent_")
                )

                if is_agent_action:
                    await self._post_agent_action_to_queue(
                        initiative, initiative_id, type_value, action_hint
                    )
                    continue  # Do NOT push to delivery bridge

                payload = {
                    "id": initiative_id,
                    "type": type_value,
                    "priority": getattr(initiative, "priority", 0.5),
                    "title": getattr(initiative, "description", "").split(".")[0][:80] if getattr(initiative, "description", "") else "(no title)",
                    "description": getattr(initiative, "description", ""),
                    "rationale": getattr(initiative, "rationale", ""),
                    "suggested_action": action_hint or "review_and_decide",
                    "entity_id": getattr(initiative, "entity_id", None),
                    "entity_type": type_value,
                    "channel_hint": "home" if is_self_initiative else "dm",
                    "context": initiative_context,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

                if delivery:
                    # Rate-limit check before push (v0.13.0)
                    from colony_sidecar.identity.resolver import get_owner_contact_id
                    # Rate-limiter bucket key only — "owner" here is a
                    # bucket label, not an identity claim.
                    person_id = payload.get("entity_id") or get_owner_contact_id() or "owner"
                    urgency = float(payload.get("priority", 0.5))
                    if hasattr(delivery, "_rate_limiter") and delivery._rate_limiter is not None:
                        allowed, reason = delivery._rate_limiter.can_deliver(person_id, urgency=urgency)
                        if not allowed:
                            logger.debug(
                                "Initiative push rate-limited for %s: %s (urgency=%.2f)",
                                person_id, reason, urgency,
                            )
                            continue  # Keep in store for retry next tick
                    if getattr(self.config, "proactive_delivery_enabled", False):
                        ok = await delivery.push_initiative(payload)
                        if ok:
                            self.stats.actions_executed += 1
                            self.stats.actions_this_hour += 1
                            logger.info("Pushed initiative: %s", payload["id"])
                            try:
                                from colony_sidecar.api.routers.host import _telemetry
                                if _telemetry is not None:
                                    await _telemetry.touch("last_initiative_at")
                            except Exception:
                                logger.warning("Telemetry touch failed (non-critical)")
                    else:
                        logger.debug("Proactive delivery disabled — initiative stored for agent polling")

                # WebSocket broadcast
                try:
                    broadcast = _get_broadcast()
                    broadcast({
                        "type": "initiative",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "payload": payload,
                    })
                except Exception:
                    logger.warning("WebSocket broadcast failed (non-critical)")
            except Exception as exc:
                logger.error("Failed to push initiative: %s", exc)

        self._pending_initiatives = []

    async def _phase_observation_sync(self) -> None:
        """Request fresh observations for stale domains (v0.16.0).

        The agent is Colony's sensor array: when a domain's newest
        observation outlives its sync interval, post a read-only
        ``agent_sync_<domain>`` job to the task queue. The agent claims
        it, observes through its own Hermes connections, and POSTs
        snapshots back to /v1/host/observations. Colony never calls
        external APIs itself.
        """
        task_queue = getattr(self._registry, "task_queue", None)
        if task_queue is None:
            return
        try:
            from colony_sidecar.api.routers.observations import get_observation_store
            obs_store = get_observation_store()
        except Exception:
            obs_store = None
        if obs_store is None:
            return

        from colony_sidecar.initiatives.action_registry import OBSERVATION_SYNC_ACTIONS
        from colony_sidecar.observations.store import OBSERVATION_SYNC_INTERVALS

        enabled = os.environ.get(
            "COLONY_SYNC_DOMAINS",
            "coding,task,calendar,research,project,system",
        )
        now = datetime.now(timezone.utc)

        for domain in (d.strip() for d in enabled.split(",")):
            action = OBSERVATION_SYNC_ACTIONS.get(domain)
            if action is None:
                continue
            interval = OBSERVATION_SYNC_INTERVALS.get(domain, 3600)
            try:
                age = obs_store.domain_age_seconds(domain)
            except Exception:
                continue
            if age is not None and age < interval:
                continue
            last_request = self._last_sync_request.get(domain)
            if last_request and (now - last_request).total_seconds() < interval:
                continue  # already asked; the agent may just be slow
            try:
                bucket = int(now.timestamp() // max(interval, 300))
                await task_queue.submit(
                    task_type="agent_action",
                    priority="normal",
                    params={
                        "action_hint": action,
                        "domain": domain,
                        "risk": "read_only",
                        "description": (
                            f"Observe the {domain} domain through your own "
                            f"connections and report snapshots to Colony"
                        ),
                        "report_to": "/v1/host/observations",
                        "report_example": {
                            "domain": domain,
                            "reported_by": "<your agent id>",
                            "observations": [
                                {"entity_id": "<stable id>", "payload": {}}
                            ],
                        },
                    },
                    idempotency_key=f"agent_sync:{domain}:{bucket}",
                )
                self._last_sync_request[domain] = now
                logger.info(
                    "Requested %s observation sync (domain age: %s)",
                    domain,
                    f"{age:.0f}s" if age is not None else "never observed",
                )
            except Exception as exc:
                logger.warning("Observation sync request failed for %s: %s", domain, exc)

    async def _post_agent_action_to_queue(
        self,
        initiative: Any,
        initiative_id: str,
        type_value: str,
        action_hint: str,
    ) -> None:
        """Post an AGENT_ACTION initiative to the task queue (v0.13.0).

        Gated actions are posted as BLOCKED awaiting owner approval; the
        rest are posted as QUEUED for immediate claiming. What counts as
        gated depends on COLONY_APPROVAL_POLICY (v0.18.0): strict gates
        everything non-read-only (v0.17 behavior); graduated only gates
        destructive actions and outbound actions whose recipient is not
        an authorized contact. Auto-passed mutating/outbound jobs carry
        audit tags and emit ``action_auto_approved``.
        """
        task_queue = getattr(self._registry, "task_queue", None)
        if task_queue is None:
            logger.warning("No task_queue available, skipping agent_action: %s", initiative_id)
            return

        # v0.16.0: action_hint must be a named capability in the action
        # registry. Initiatives are built from graph data that can include
        # untrusted content — an unregistered hint NEVER reaches the queue.
        # The initiative stays stored (visible to the agent as information)
        # but nothing executes it.
        from colony_sidecar.initiatives.action_registry import (
            RiskTier,
            classify_agent_action,
            get_action,
            get_approval_policy,
        )

        policy = get_approval_policy()
        verdict = classify_agent_action(action_hint, policy=policy)
        if not verdict["executable"]:
            logger.warning(
                "action_hint %r is not in the action registry — initiative "
                "%s stored but NOT queued for execution",
                action_hint, initiative_id,
            )
            return

        auto_approve = os.environ.get("COLONY_AGENT_AUTO_APPROVE", "false").lower() == "true"

        job_payload = {
            "initiative_id": initiative_id,
            "action_hint": action_hint,
            "description": getattr(initiative, "description", ""),
            "entity_id": getattr(initiative, "entity_id", None),
            "risk": verdict["risk"],
            "auto_approve": auto_approve,
            "context": self._build_initiative_context(initiative, type_value),
        }

        # v0.18.0 graduated policy: an OUTBOUND action auto-passes only
        # when its recipient resolves to an authorized contact
        # (interaction_allowed=True). Fails closed — no contact store, no
        # target, unknown or unauthorized contact all keep the gate.
        target_verdict = ""
        if (
            policy == "graduated"
            and verdict["risk"] == RiskTier.OUTBOUND.value
            and verdict["requires_approval"]
        ):
            from colony_sidecar.initiatives.approval_policy import is_authorized_target

            contacts_store = getattr(self._registry, "contacts", None)
            authorized, target_verdict = await is_authorized_target(
                job_payload, get_action(action_hint), contacts_store,
            )
            if authorized:
                verdict = classify_agent_action(
                    action_hint,
                    params=job_payload,
                    policy=policy,
                    target_authorized=True,
                )

        # Gated actions require HUMAN OWNER approval — the agent cannot
        # approve its own mutations. COLONY_AGENT_AUTO_APPROVE collapses
        # the gate for trusted deployments (default false).
        is_gated = bool(verdict["requires_approval"])
        job_payload["destructive"] = is_gated  # legacy field name, kept for workers

        # v0.17.0: gated jobs are created directly in BLOCKED so no worker
        # can claim them in the window before a post-hoc transition lands.
        gate_pending = is_gated and not auto_approve

        # v0.18.0: non-read-only jobs that the POLICY (not the legacy env
        # bypass) waved through get a visible audit trail.
        policy_auto_pass = (
            not is_gated and verdict["risk"] != RiskTier.READ_ONLY.value
        )
        if gate_pending:
            job_tags = {"blocked_reason": "awaiting_owner_approval"}
        elif policy_auto_pass:
            approved_via = (
                "standing_approval" if verdict["reason"] == "standing_approval"
                else policy
            )
            job_tags = {
                "auto_approved_by_policy": approved_via,
                "risk": str(verdict["risk"]),
            }
            if verdict["reason"] == "outbound_authorized_contact" and target_verdict:
                job_tags["outbound_target"] = target_verdict
        else:
            job_tags = None

        try:
            from colony_sidecar.task_queue.models import JobStatus

            job_result = await task_queue.submit(
                task_type="agent_action",
                priority="high" if getattr(initiative, "priority", 0.5) > 0.7 else "normal",
                params=job_payload,
                idempotency_key=f"agent_action:{action_hint}:{getattr(initiative, 'entity_id', 'global')}",
                initial_status=JobStatus.BLOCKED if gate_pending else None,
                tags=job_tags,
            )
            job_id = job_result.get("id")
            logger.info("Posted agent_action job %s for initiative %s", job_id, initiative_id)

            if policy_auto_pass and job_id:
                logger.info(
                    "Auto-approved %s job %s (%s: %s)",
                    verdict["risk"], job_id, policy, verdict["reason"],
                )
                try:
                    from colony_sidecar.events.broadcaster import emit as broadcast
                    broadcast("action_auto_approved", {
                        "job_id": job_id,
                        "initiative_id": initiative_id,
                        "action_hint": action_hint,
                        "risk": verdict["risk"],
                        "policy": policy,
                        "reason": verdict["reason"],
                    })
                except Exception:
                    pass

            # Update initiative with job_id
            store = getattr(self._registry, "initiative_store", None)
            if store and job_id:
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        lambda sid=initiative_id, jid=job_id: store.update(sid, job_id=jid, status="assigned"),
                    )
                except Exception as exc:
                    logger.warning("Failed to link initiative %s to job %s: %s", initiative_id, job_id, exc)

            # mutating/outbound and not auto-approved → blocked awaiting owner
            if gate_pending and job_id:
                logger.info(
                    "Blocked %s job %s awaiting owner approval",
                    verdict["risk"], job_id,
                )
                # Push approval request to delivery
                delivery = self._registry.delivery
                if delivery and hasattr(delivery, "push_initiative"):
                    if getattr(self.config, "proactive_delivery_enabled", False):
                        await delivery.push_initiative({
                            "id": initiative_id,
                            "type": "agent_action",
                            "priority": getattr(initiative, "priority", 0.5),
                            "title": f"Approval required: {getattr(initiative, 'description', '')[:60]}",
                            "description": getattr(initiative, "description", ""),
                            "rationale": getattr(initiative, "rationale", ""),
                            "suggested_action": "colony_approve_initiative",
                            "entity_id": getattr(initiative, "entity_id", None),
                            "channel_hint": "dm",
                            "context": {"job_id": job_id, "action_hint": action_hint},
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        })
                    else:
                        logger.debug("Proactive delivery disabled — approval request stored for agent polling")

            # Only count as executed if the job was not blocked awaiting approval
            if not gate_pending:
                self.stats.actions_executed += 1
                self.stats.actions_this_hour += 1
        except Exception as exc:
            logger.error("Failed to post agent_action to queue: %s", exc)

    async def _feed_pending_tasks(self, engine: Any) -> None:
        """Feed active goals as pending tasks, respecting cooldown.

        Filters out abandoned and completed goals so the autonomy loop
        does not generate follow-up initiatives for dead tasks forever.
        """
        goals = self._registry.goals
        if goals is None:
            return

        try:
            # Use get_active_tasks which respects cooldown and snooze (v0.7.10)
            cooldown_tasks = float(os.environ.get(
                "COLONY_INITIATIVE_COOLDOWN_TASKS", "12",
            ))

            if hasattr(goals, "get_active_tasks"):
                active = goals.get_active_tasks(cooldown_hours=cooldown_tasks)
                pending_tasks = []
                for goal in active:
                    # Skip abandoned / completed / cancelled goals
                    g_status = getattr(goal, "status", None)
                    if g_status in ("abandoned", "completed", "cancelled"):
                        continue
                    days_pending = 0
                    if goal.created_at:
                        days_pending = (datetime.now(timezone.utc) - goal.created_at).total_seconds() / 86400
                    pending_tasks.append({
                        "description": goal.title or "pending task",
                        "days_pending": days_pending,
                        "entity_id": goal.goal_id,
                    })
            else:
                # Fallback for stores without get_active_tasks
                blocked = goals.list_goals(status="blocked", limit=20) if hasattr(goals, "list_goals") else []
                pending_tasks = []
                for goal in blocked:
                    # Skip abandoned / completed / cancelled goals
                    if isinstance(goal, dict):
                        g_status = goal.get("status", "")
                    else:
                        g_status = getattr(goal, "status", "")
                    if g_status in ("abandoned", "completed", "cancelled"):
                        continue
                    created = goal.created_at
                    days_pending = 0
                    if created:
                        days_pending = (datetime.now(timezone.utc) - created).total_seconds() / 86400
                    # Handle both dict and object representations
                    if isinstance(goal, dict):
                        entity_id = goal.get("context", {}).get("contact_id") if goal.get("context") else goal.get("goal_id")
                    else:
                        ctx = getattr(goal, "context", None)
                        entity_id = ctx.get("contact_id") if ctx else getattr(goal, "goal_id", None)
                    pending_tasks.append({
                        "description": goal.title or "blocked goal",
                        "days_pending": days_pending,
                        "entity_id": entity_id,
                    })

            # Always set pending_tasks so graph loader doesn't fall back to stale
            # graph data when the SQL store has no active goals (Bug 41).
            engine.add_context("pending_tasks", pending_tasks)
        except Exception as e:
            logger.warning("Failed to feed pending tasks: %s", e)

    async def _feed_neglected_contacts(self, engine: Any) -> None:
        """Feed contacts with declining affect AND genuine neglect.

        Combines affect-store signals with graph-based days-since-contact.
        Only feeds contacts that have both declining affect AND no recent
        interaction (≥7 days).  Skips the host's own contact.
        """
        affect = self._registry.affect_store
        if affect is None:
            return

        # Owner exclusion is a relationship-domain policy (the agent must
        # not "check in with" its own operator) — fail closed when the
        # owner identity can't be established. Other domains (commitment,
        # calendar, agent_action) legitimately target the owner and must
        # NOT inherit this filter.
        from colony_sidecar.identity.resolver import (
            OwnerIdentityError,
            get_identity_resolver,
        )
        resolver = get_identity_resolver()
        try:
            await resolver.owner_identities()
        except OwnerIdentityError as exc:
            logger.critical(
                "Owner identity unresolved — neglected-contact feed "
                "disabled (fail closed): %s", exc,
            )
            return

        try:
            states = affect.get_all_states() if hasattr(affect, "get_all_states") else []
            neglected = []

            for state in states[:20]:
                contact_id = state.get("contact_id")
                if not contact_id or await resolver.is_owner(contact_id):
                    continue

                # Only sustained decline, not a single bad event
                if hasattr(affect, "detect_sustained_decline"):
                    if not affect.detect_sustained_decline(contact_id, min_events=3):
                        continue
                else:
                    # Fallback: declining trend + negative valence
                    if state.get("trend") != "declining":
                        continue
                    if state.get("current_valence", 0) >= -0.3:
                        continue

                neglected.append({
                    "name": contact_id,
                    "entity_id": contact_id,
                    "days_since_contact": 7,  # minimum threshold; engine loads exact days from graph
                })

            if neglected:
                engine.add_context("neglected_contacts", neglected)
        except Exception as e:
            logger.warning("Failed to feed neglected contacts: %s", e)

    async def _feed_commitment_reminders(self, engine: Any) -> None:
        """Feed upcoming/overdue commitments for COMMITMENT initiatives.

        v0.16.0: commitments are first-class COMMITMENT initiatives
        (durable context, dedup ``commitment:{id}``) instead of being
        flattened into anonymous scheduling opportunities. The owner is a
        legitimate subject here.
        """
        commitments = self._registry.commitment_store
        if commitments is None:
            return

        try:
            # CommitmentStore.list() returns {"commitments": [...], "total": N}
            result = commitments.list(status=["pending"], limit=20) if hasattr(commitments, "list") else {"commitments": []}
            active = result.get("commitments", [])

            now = datetime.now(timezone.utc)
            upcoming = []

            for c in active:
                due = c.get("due_at")
                if not due:
                    continue

                if isinstance(due, str):
                    due = datetime.fromisoformat(due.replace("Z", "+00:00"))

                hours_until = (due - now).total_seconds() / 3600

                # Surface anything due in the next 48h, plus overdue
                # commitments up to a week old (they need follow-up most).
                if -168 < hours_until < 48:
                    upcoming.append({
                        "commitment_id": c.get("id"),
                        "description": c.get("description", "untitled"),
                        "due_at": due.isoformat(),
                        "hours_until_due": hours_until,
                        "overdue": hours_until <= 0,
                        "status": c.get("status", "pending"),
                        "person_id": c.get("person_id"),
                    })

            if upcoming:
                engine.add_context("upcoming_commitments", upcoming)
        except Exception as e:
            logger.warning("Failed to feed commitment reminders: %s", e)

    async def _phase_job_writeback(self) -> None:
        """Phase 6c (v0.17.0): close the act → learn loop.

        Completed/failed agent jobs become episodic memories, advance
        their goals, complete their linked initiatives, and broadcast
        events. Before this phase, agent work landed in the queue DB and
        was invisible to memory — Colony could act but never learn from
        acting. Idempotent via the ``memory_synced`` job tag; a poison
        job is retried up to 3 ticks then tagged off.
        """
        task_queue = getattr(self._registry, "task_queue", None)
        if task_queue is None:
            return
        qm = getattr(task_queue, "queue", None) or task_queue
        if not hasattr(qm, "get_jobs_by_status"):
            return
        from colony_sidecar.task_queue.models import JobStatus

        try:
            done = list(await qm.get_jobs_by_status(JobStatus.COMPLETED))
            done += list(await qm.get_jobs_by_status(JobStatus.FAILED))
        except Exception as exc:
            logger.debug("Job writeback: queue scan failed: %s", exc)
            return

        synced = 0
        for job in done:
            tags = job.tags or {}
            if tags.get("memory_synced") == "true":
                continue
            if job.job_type != "agent_action":
                continue
            action_hint = (job.payload or {}).get("action_hint", "")
            if str(action_hint).startswith("agent_sync_"):
                # Observation syncs already land in the observation store;
                # recording them as memories would be routine-plumbing noise.
                await self._tag_job_synced(qm, job)
                continue
            try:
                await self._writeback_one_job(job)
                await self._tag_job_synced(qm, job)
                synced += 1
            except Exception as exc:
                attempts = int(tags.get("memory_sync_attempts", "0")) + 1
                logger.warning("Job writeback failed for %s (attempt %d): %s",
                               job.job_id, attempts, exc)
                new_tags = {"memory_sync_attempts": str(attempts)}
                if attempts >= 3:
                    new_tags["memory_synced"] = "true"  # give up, stop retrying
                try:
                    await qm.update_job_status(job.job_id, job.status,
                                               tags=new_tags)
                except Exception:
                    pass
        if synced:
            logger.info("Phase job-writeback: %d agent job(s) fed back to memory",
                        synced)

    @staticmethod
    async def _tag_job_synced(qm: Any, job: Any) -> None:
        await qm.update_job_status(job.job_id, job.status,
                                   tags={"memory_synced": "true"})

    async def _writeback_one_job(self, job: Any) -> None:
        """Propagate one finished agent job to goals, memory, initiatives."""
        result = job.result
        payload = job.payload or {}
        succeeded = bool(result is not None and result.succeeded)
        action = payload.get("action_hint") or job.job_type
        description = payload.get("description", "")

        # 1. Goal progress — the engine method existed since v0.13 but
        # nothing ever called it.
        goals = self._registry.goals
        if (goals is not None and result is not None
                and hasattr(goals, "on_job_completed")):
            output = result.output or {}
            if output.get("goal_id") and output.get("subtask_id"):
                try:
                    goals.on_job_completed(result)
                except Exception as exc:
                    logger.warning("Goal writeback failed for %s: %s",
                                   job.job_id, exc)

        # 2. Episodic memory of what the agent did.
        graph = self._registry.graph
        if graph is not None and hasattr(graph, "store_memory"):
            outcome = "completed" if succeeded else (
                f"FAILED ({(result.error if result else None) or 'unknown error'})")
            summary = ""
            if result is not None and isinstance(result.output, dict):
                raw = result.output.get("summary") or result.output.get("result")
                if raw:
                    summary = f" Result: {str(raw)[:300]}"
            content = (f"Agent {outcome} action '{action}'"
                       + (f" — {description}" if description else "")
                       + f".{summary}")
            await graph.store_memory(
                content=content,
                memory_type="episodic",
                entities=[],
                metadata={"job_id": job.job_id, "action_hint": str(action),
                          "succeeded": succeeded},
                importance=0.6 if succeeded else 0.7,
                source_type="tool_output",
                source_uri=f"colony://jobs/{job.job_id}",
            )

        # 3. Linked initiative closure.
        initiative_id = payload.get("initiative_id")
        store = getattr(self._registry, "initiative_store", None)
        if initiative_id and store is not None:
            try:
                if succeeded and hasattr(store, "complete"):
                    store.complete(initiative_id,
                                   agent_id=job.claimed_by or "agent",
                                   result=f"job {job.job_id} completed")
                elif not succeeded and hasattr(store, "update"):
                    store.update(initiative_id, status="failed",
                                 failed_reason=f"job {job.job_id} failed")
            except Exception as exc:
                logger.warning("Initiative closure failed for %s: %s",
                               initiative_id, exc)

        # 4. Skill capture (v0.17.0, COLONY_ENABLE_SKILL_SYNTHESIS) — feed
        # successful novel work into the existing learning pipeline
        # (novelty gate → pattern extraction → DRAFT skill package).
        # Captured skills are DRAFT and deny-by-default; the v0.13
        # approval workflow gates activation, so nothing synthesized can
        # execute without the owner.
        if succeeded:
            await self._maybe_capture_skill(job, action, description)

        # 5. Broadcast for anything listening (WS clients, audit log).
        try:
            from colony_sidecar.events.broadcaster import emit as broadcast
            broadcast("job_completed" if succeeded else "job_failed",
                      {"job_id": job.job_id, "action_hint": str(action),
                       "initiative_id": initiative_id})
        except Exception:
            pass

    def _get_skill_learning(self) -> Any:
        """Lazily build the SkillLearningService (or None if disabled)."""
        if os.environ.get("COLONY_ENABLE_SKILL_SYNTHESIS",
                          "false").lower() != "true":
            return None
        service = getattr(self, "_skill_learning", None)
        if service is not None:
            return service
        skills_registry = self._registry.skills
        if skills_registry is None:
            return None
        try:
            import pathlib

            from colony_sidecar.skills.learning import (
                NoveltyDetector,
                PatternExtractor,
                SkillLearningService,
            )
            from colony_sidecar.skills.packager import SkillPackager

            library = pathlib.Path(
                os.environ.get("COLONY_SKILL_LIBRARY")
                or os.path.join(os.environ.get("COLONY_STATE_DIR", "."),
                                "skill_library"))
            packager = SkillPackager(
                registry=skills_registry,
                colony_id=os.environ.get("COLONY_NODE_ID", "colony"),
                library_root=library,
            )
            service = SkillLearningService(
                detector=NoveltyDetector(skills_registry),
                extractor=PatternExtractor(),
                packager=packager,
            )
            self._skill_learning = service
            logger.info("Skill synthesis enabled (library=%s)", library)
            return service
        except Exception as exc:
            logger.warning("Skill synthesis unavailable: %s", exc)
            self._skill_learning = None
            return None

    async def _maybe_capture_skill(self, job: Any, action: str,
                                   description: str) -> None:
        service = self._get_skill_learning()
        if service is None:
            return
        try:
            from datetime import datetime, timezone

            from colony_sidecar.skills.learning.triggers import (
                LearningTriggerEvent,
                TriggerSource,
            )
            from colony_sidecar.skills.models import TaskSolution

            result = job.result
            output = (result.output or {}) if result is not None else {}
            solution = TaskSolution(
                task_id=job.job_id,
                task_description=description or str(action),
                inputs=dict(job.payload or {}),
                output=output,
                trace=list(output.get("trace", [])),
                dependencies=[],
                embedding=None,
                step_fingerprint=None,
                duration_secs=float(
                    getattr(result, "duration_seconds", None) or 0.0),
                completed_at=getattr(result, "completed_at", None)
                or datetime.now(timezone.utc),
            )
            skill_id = await service.handle(LearningTriggerEvent(
                source=TriggerSource.POST_TASK_HOOK, solution=solution))
            if skill_id:
                self._queue_deferred_initiative(skill_id, description or action)
        except Exception as exc:
            logger.warning("Skill capture failed for %s: %s", job.job_id, exc)

    def _queue_deferred_initiative(self, skill_id: str, task_desc: str) -> None:
        """Surface a captured DRAFT skill to the owner next tick."""
        from colony_sidecar.intelligence.components.initiative_engine import (
            Initiative,
            InitiativeType,
        )
        deferred = getattr(self, "_deferred_initiatives", None)
        if deferred is None:
            deferred = []
            self._deferred_initiatives = deferred
        deferred.append(Initiative(
            id=f"init-skill-{skill_id[:24]}",
            type=InitiativeType.CAPABILITY_GAP,
            description=f"Review new draft skill '{skill_id}' captured from: "
                        f"{task_desc[:120]}",
            priority=0.7,
            rationale="[skill synthesis] novel successful work was captured "
                      "as a DRAFT skill; it cannot run until you approve it.",
            action_hint=None,
            dedup_key=f"skill_review:{skill_id}",
        ))

    async def _phase_cognition(self) -> None:
        """Run cognition pipeline tick."""
        cognition = self._registry.cognition
        if cognition is None:
            return
        try:
            if hasattr(cognition, "run_cycle"):
                await cognition.run_cycle()
                logger.debug("Phase cognition: cycle complete")
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase cognition error: %s", exc, exc_info=True)

    async def _phase_memory_consolidation(self) -> None:
        """Memory consolidation — runs once per hour."""
        graph = self._registry.graph
        if graph is None:
            return
        current_hour = datetime.now(timezone.utc).hour
        if current_hour == self._last_consolidation_hour:
            return
        try:
            if hasattr(graph, "consolidate_memories"):
                promoted = await graph.consolidate_memories()
                self.stats.memories_promoted += len(promoted) if promoted else 0
            self._last_consolidation_hour = current_hour
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase memory_consolidation error: %s", exc, exc_info=True)

    async def _phase_memory_decay(self) -> None:
        """Memory decay — runs once per day."""
        graph = self._registry.graph
        if graph is None:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == self._last_decay_date:
            return
        try:
            if hasattr(graph, "decay_memories"):
                await graph.decay_memories()
            self._last_decay_date = today
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase memory_decay error: %s", exc, exc_info=True)

    async def _phase_memory_pruning(self) -> None:
        """Memory pruning — runs once per week."""
        graph = self._registry.graph
        if graph is None:
            return
        week = datetime.now(timezone.utc).strftime("%Y-W%W")
        if week == self._last_prune_date:
            return
        try:
            if hasattr(graph, "prune_memories"):
                await graph.prune_memories()
            self._last_prune_date = week
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase memory_pruning error: %s", exc, exc_info=True)

    async def _phase_memory_reconciliation(self) -> None:
        """Memory reconciliation — runs once per day."""
        graph = self._registry.graph
        if graph is None:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today == self._last_reconcile_date:
            return
        try:
            from colony_sidecar.intelligence.graph.reconciler import FileReconciler
            reconciler = FileReconciler(graph)
            result = await reconciler.reconcile(dry_run=False)
            logger.info(
                "Phase memory_reconciliation: checked=%d verified=%d staled=%d superseded=%d errors=%d",
                result["files_checked"],
                result["memories_verified"],
                result["memories_staled"],
                result["memories_superseded"],
                len(result["errors"]),
            )
            self._last_reconcile_date = today
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase memory_reconciliation error: %s", exc, exc_info=True)

    async def _phase_memory_archive(self) -> None:
        """Memory archive — runs once per week."""
        graph = self._registry.graph
        if graph is None:
            return
        week = datetime.now(timezone.utc).strftime("%Y-W%W")
        if week == self._last_archive_week:
            return
        try:
            if hasattr(graph, "archive_memories"):
                archived = await graph.archive_memories(max_age_days=30)
                logger.info("Phase memory_archive: archived=%d", archived)
            self._last_archive_week = week
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase memory_archive error: %s", exc, exc_info=True)

    async def _phase_memory_distillation(self) -> None:
        """Memory distillation — runs once per week."""
        graph = self._registry.graph
        if graph is None:
            return
        week = datetime.now(timezone.utc).strftime("%Y-W%W")
        if week == self._last_distillation_week:
            return
        try:
            if hasattr(graph, "distill_memories"):
                await graph.distill_memories()
            self._last_distillation_week = week
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase memory_distillation error: %s", exc, exc_info=True)

    async def _phase_task_completion(self) -> None:
        """Emit follow-up events for goals that completed since the last check.

        Runs at most hourly. Emits one ``task_completed_followup`` event per
        newly-completed goal and asks the connection discoverer for
        reflection insights when a backlog accumulates.
        """
        now = datetime.now(timezone.utc)
        if self._last_task_completion_check is not None:
            elapsed = (now - self._last_task_completion_check).total_seconds()
            if elapsed < 3600:  # Check hourly
                return

        goals = self._registry.goals
        if goals is None:
            self._last_task_completion_check = now
            return

        try:
            from colony_sidecar.goals.models import GoalStatus
            completed = goals.list_goals(status=GoalStatus.COMPLETED, limit=50)

            window_start = self._last_task_completion_check
            new_completions = []
            for g in completed:
                cat = getattr(g, "completed_at", None)
                if cat is None:
                    continue
                if window_start is None or cat >= window_start:
                    new_completions.append(g)

            for g in new_completions:
                try:
                    self.events.emit(Event(
                        id=f"task-followup-{getattr(g, 'goal_id', uuid.uuid4())}",
                        source="autonomy.task_completion",
                    ))
                    broadcast = _get_broadcast()
                    if broadcast is not None:
                        try:
                            broadcast({
                                "type": "task_followup",
                                "goal_id": getattr(g, "goal_id", ""),
                                "title": getattr(g, "title", ""),
                            })
                        except Exception:
                            logger.debug("broadcast task_followup failed", exc_info=True)
                except Exception:
                    logger.debug("emit task_followup failed", exc_info=True)

            # If several goals finished in the window, ask synthesis for
            # reflection connections to surface patterns.
            if len(new_completions) >= 3:
                discoverer = self._registry.connection_discoverer
                if discoverer is not None and hasattr(discoverer, "discover_connections"):
                    try:
                        await discoverer.discover_connections(min_novelty=0.3)
                    except Exception:
                        logger.debug("reflection discovery failed", exc_info=True)

            self.stats.task_follow_ups += len(new_completions)
            self._last_task_completion_check = now
            if new_completions:
                logger.info(
                    "Phase task_completion: %d follow-up(s)", len(new_completions)
                )
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase task_completion error: %s", exc, exc_info=True)

    async def _phase_frustration_update(self) -> None:
        """Update delivery rate limiter based on engagement feedback."""
        delivery = self._registry.delivery
        if delivery is None:
            return
        try:
            learner = self._registry.learner
            if learner is not None and hasattr(delivery, "update_rate_limiter"):
                await delivery.update_rate_limiter(learner)
        except Exception as exc:
            logger.debug("Phase frustration_update error (non-fatal): %s", exc)

    async def _phase_relationships(self) -> None:
        """Update relationship scores and trust tiers."""
        graph = self._registry.graph
        if graph is None:
            return
        try:
            from colony_sidecar.intelligence.relationships.scorer import RelationshipScorer
            scorer = RelationshipScorer(graph)
            if hasattr(scorer, "refresh_all_scores"):
                changes = await scorer.refresh_all_scores()
                if changes:
                    self.stats.scoring_runs += 1
                    logger.info("Phase relationships: %d score updates", len(changes))
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase relationships error: %s", exc, exc_info=True)

    async def _phase_synthesis(self) -> None:
        """Discover cross-domain connections."""
        discoverer = self._registry.connection_discoverer
        if discoverer is None:
            return
        try:
            connections = await discoverer.discover_connections()
            if connections:
                logger.info("Phase synthesis: %d new connections", len(connections))
                _get_broadcast()({
                    "type": "insight",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "payload": {"new_connections": len(connections)},
                })
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase synthesis error: %s", exc, exc_info=True)

    async def _phase_bootstrap_check(self) -> None:
        """Run identity bootstrap self-check daily."""
        chain = self._registry.chain
        if chain is None:
            return
        now = datetime.now(timezone.utc)
        interval_hours = self.config.bootstrap_check_interval_hours
        if self._last_bootstrap_check is not None:
            elapsed = (now - self._last_bootstrap_check).total_seconds() / 3600
            if elapsed < interval_hours:
                return
        try:
            if hasattr(chain, "health_check"):
                healthy = await chain.health_check()
                self._last_bootstrap_check = now
                if not healthy:
                    logger.warning("Phase bootstrap_check: chain health degraded")
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase bootstrap_check error: %s", exc, exc_info=True)

    async def _phase_self_reflection(self) -> None:
        """Run self-reflection component weekly."""
        now = datetime.now(timezone.utc)
        interval_days = self.config.self_reflection_interval_days
        if self._last_self_reflection is not None:
            elapsed = (now - self._last_self_reflection).total_seconds() / 86400
            if elapsed < interval_days:
                return
        try:
            cognition = self._registry.cognition
            if cognition is not None and hasattr(cognition, "self_reflect"):
                await cognition.self_reflect()
                self._last_self_reflection = now
                logger.info("Phase self_reflection: complete")
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase self_reflection error: %s", exc, exc_info=True)

    async def _phase_skill_triggers(self, event_text: str) -> None:
        """Evaluate skill triggers from recent events."""
        skills = self._registry.skills
        if skills is None:
            return
        try:
            if hasattr(skills, "evaluate_triggers"):
                loaded = await skills.evaluate_triggers(event_text)
                self.stats.skills_loaded = len(loaded)
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase skill_triggers error: %s", exc, exc_info=True)

    async def _phase_skill_evict(self) -> None:
        """Evict cold skills after execution."""
        skills = self._registry.skills
        if skills is None:
            return
        try:
            if hasattr(skills, "evict_cold"):
                evicted = await skills.evict_cold()
                self.stats.skills_evicted += evicted
        except Exception as exc:
            logger.debug("Phase skill_evict error (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Multi-Agent Phases (v0.7.0)
    # ------------------------------------------------------------------

    async def _phase_agent_heartbeat(self) -> None:
        """Check agent status and mark offline if heartbeat timeout."""
        agent_store = self._registry.agent_store
        if agent_store is None:
            return
        try:
            # Get agents with old last_seen_at
            from datetime import timedelta
            threshold = datetime.now(timezone.utc) - timedelta(minutes=5)

            agents = agent_store.list(status=["online", "busy"])
            for agent in agents:
                if agent.last_seen_at and agent.last_seen_at < threshold:
                    logger.info("Agent %s marked offline (no heartbeat)", agent.name)
                    await agent_store.set_offline(agent.agent_id)

                    # Reassign pending initiatives
                    initiative_store = self._registry.initiative_store
                    if initiative_store:
                        reassigned = initiative_store.reassign_from_agent(
                            agent.agent_id,
                            only_pending=True,
                        )
                        if reassigned:
                            logger.info("Reassigned %d initiatives from offline agent %s", reassigned, agent.name)
        except Exception as exc:
            logger.debug("Phase agent_heartbeat error (non-fatal): %s", exc)

    async def _phase_startup_repush(self) -> None:
        """On first tick: prune orphaned initiatives and re-push pending to delivery."""
        if self.stats.ticks != 1:
            return

        initiative_store = self._registry.initiative_store
        delivery = self._registry.delivery
        graph = self._registry.graph

        if initiative_store is None:
            return

        try:
            # 1. Cancel initiatives whose entity no longer exists in graph
            if graph is not None and hasattr(graph, "driver"):
                pending = initiative_store.list(status=["pending"], limit=1000)
                pruned = 0
                for initiative in pending:
                    entity_id = initiative.entity_id
                    if not entity_id:
                        continue
                    try:
                        async with graph.driver.session(database=graph.database) as session:
                            result = await session.run(
                                "MATCH (n {id: $id}) RETURN count(n) as c",
                                {"id": entity_id},
                            )
                            record = await result.single()
                            if record is None or record["c"] == 0:
                                initiative_store.cancel(
                                    initiative.id,
                                    cancelled_by="autonomy_loop",
                                    reason="entity_no_longer_exists",
                                )
                                pruned += 1
                                logger.info(
                                    "Pruned orphaned initiative %s (entity %s not in graph)",
                                    initiative.id,
                                    entity_id,
                                )
                    except Exception as exc:
                        logger.debug("Graph check failed for %s: %s", entity_id, exc)

                if pruned:
                    logger.info("Pruned %d orphaned initiatives on startup", pruned)

            # 2. Re-push remaining pending initiatives to delivery bridge
            if delivery is not None:
                pending = initiative_store.list(status=["pending"], limit=100)
                repushed = 0
                for initiative in pending:
                    payload = {
                        "id": initiative.id,
                        "type": initiative.type,
                        "priority": initiative.priority,
                        "title": initiative.description.split(".")[0][:80] if initiative.description else "(no title)",
                        "description": initiative.description,
                        "rationale": initiative.rationale or "",
                        "suggested_action": initiative.action_hint or "review_and_decide",
                        "entity_id": initiative.entity_id,
                        "entity_type": initiative.type,
                        "context": {},
                        "generated_at": initiative.created_at.isoformat() if initiative.created_at else datetime.now(timezone.utc).isoformat(),
                    }
                    try:
                        if getattr(self.config, "proactive_delivery_enabled", False):
                            ok = await delivery.push_initiative(payload)
                            if ok:
                                repushed += 1
                        else:
                            logger.debug("Proactive delivery disabled — skipping startup re-push")
                    except Exception as exc:
                        logger.debug("Failed to re-push initiative %s: %s", initiative.id, exc)

                if repushed:
                    logger.info("Re-pushed %d pending initiatives to delivery bridge", repushed)

        except Exception as exc:
            self.stats.errors += 1
            logger.error("Startup re-push phase error: %s", exc, exc_info=True)

    async def _phase_initiative_timeout(self) -> None:
        """Check for timed-out initiatives."""
        initiative_store = self._registry.initiative_store
        if initiative_store is None:
            return
        try:
            now = datetime.now(timezone.utc)
            timed_out = initiative_store.find_timed_out(now)

            for initiative in timed_out:
                logger.warning(
                    "Initiative %s timed out after %ds",
                    initiative.id,
                    initiative.timeout_seconds,
                )

                initiative_store.update(
                    initiative.id,
                    status="failed",
                    failed_at=now.isoformat(),
                    failed_reason="timeout_exceeded",
                )

                initiative_store.log_history(
                    initiative.id,
                    action="timed_out",
                    agent_id=initiative.assigned_agent_id,
                    details={"timeout_seconds": initiative.timeout_seconds},
                )
        except Exception as exc:
            logger.debug("Phase initiative_timeout error (non-fatal): %s", exc)

    async def _phase_approval_timeout(self) -> None:
        """Fail BLOCKED jobs whose owner-approval window expired (v0.17.0).

        Jobs blocked with ``awaiting_owner_approval`` older than
        COLONY_APPROVAL_TIMEOUT_HOURS (default 72) are failed with reason
        ``owner_approval_timeout`` so they never execute silently later.
        """
        task_queue = getattr(self._registry, "task_queue", None)
        if task_queue is None:
            return
        try:
            timeout_hours = float(os.environ.get("COLONY_APPROVAL_TIMEOUT_HOURS", "72"))
            expired = await task_queue.queue.expire_blocked_approvals(
                datetime.now(timezone.utc), timeout_hours,
            )
            if expired:
                logger.info(
                    "Failed %d blocked job(s) after %.0fh without owner approval",
                    expired, timeout_hours,
                )
        except Exception as exc:
            logger.debug("Phase approval_timeout error (non-fatal): %s", exc)

    async def _phase_stale_initiative_cleanup(self) -> None:
        """Clean up initiatives stuck in acknowledged state."""
        initiative_store = self._registry.initiative_store
        agent_store = self._registry.agent_store
        if initiative_store is None or agent_store is None:
            return
        try:
            from datetime import timedelta
            threshold = datetime.now(timezone.utc) - timedelta(hours=1)

            stale = initiative_store.find_stale_acknowledged(threshold)

            for initiative in stale:
                agent = agent_store.get(initiative.assigned_agent_id)

                if agent is None or agent.status != "online":
                    logger.warning(
                        "Initiative %s stuck in acknowledged, reassigning",
                        initiative.id,
                    )
                    initiative_store.update(
                        initiative.id,
                        status="pending",
                        assigned_agent_id=None,
                        stale_reason="agent_offline_with_acknowledged",
                    )
        except Exception as exc:
            logger.debug("Phase stale_initiative_cleanup error (non-fatal): %s", exc)

    async def _phase_ghost_cleanup(self) -> None:
        """Remove agents that registered but never connected."""
        agent_store = self._registry.agent_store
        initiative_store = self._registry.initiative_store
        if agent_store is None:
            return
        try:
            from datetime import timedelta
            threshold = datetime.now(timezone.utc) - timedelta(minutes=10)

            ghosts = agent_store.list_ghosts(registered_before=threshold)

            for ghost in ghosts:
                # Reassign initiatives first
                if initiative_store:
                    initiatives = initiative_store.list(assigned_agent_id=ghost.agent_id)
                    for init in initiatives:
                        initiative_store.update(
                            init.id,
                            status="pending",
                            assigned_agent_id=None,
                            recovery_reason="agent_ghost",
                        )

                # Remove ghost
                agent_store.delete(ghost.agent_id)
                logger.info("Removed ghost agent %s", ghost.agent_id)

            # Also expire stale in_session deliveries (v0.13.0)
            delivery = getattr(self._registry, "delivery", None)
            if delivery and hasattr(delivery, "expire_in_session_deliveries"):
                expired = delivery.expire_in_session_deliveries(max_age_hours=24)
                if expired:
                    logger.info("Expired %d stale in_session deliveries", expired)
        except Exception as exc:
            logger.debug("Phase ghost_cleanup error (non-fatal): %s", exc)

    async def _phase_database_backup(self) -> None:
        """Periodic database backup for crash recovery."""
        try:
            agent_store = self._registry.agent_store
            initiative_store = self._registry.initiative_store

            if agent_store and hasattr(agent_store, "backup"):
                agent_store.backup()

            if initiative_store and hasattr(initiative_store, "backup"):
                initiative_store.backup()

            logger.debug("Database backup complete")
        except Exception as exc:
            logger.warning("Phase database_backup error: %s", exc)

    # ------------------------------------------------------------------
    # Sleep / wake
    # ------------------------------------------------------------------

    async def _sleep_until_next_tick(self) -> None:
        self._wake_event.clear()
        try:
            await asyncio.wait_for(
                asyncio.shield(self._wake_event.wait()),
                timeout=self.config.tick_interval_secs,
            )
        except asyncio.TimeoutError:
            pass

    def _on_wake_signal(self, event: Event) -> None:
        self._wake_event.set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _in_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours (in configured timezone)."""
        try:
            # Use configured timezone, fallback to UTC
            tz = ZoneInfo(self.config.timezone)
            now = datetime.now(tz)
        except Exception:
            now = datetime.now(timezone.utc)

        try:
            start_h, start_m = map(int, self.config.quiet_hours_start.split(":"))
            end_h, end_m = map(int, self.config.quiet_hours_end.split(":"))
        except (ValueError, AttributeError):
            return False

        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        current_minutes = now.hour * 60 + now.minute

        # Disabled if both are 00:00
        if start_minutes == 0 and end_minutes == 0:
            return False

        # Handle overnight quiet hours (e.g., 22:00 - 07:00)
        if start_minutes > end_minutes:
            return current_minutes >= start_minutes or current_minutes < end_minutes
        return start_minutes <= current_minutes < end_minutes

    def _reset_hour_bucket(self) -> None:
        current_hour = datetime.now(timezone.utc).hour
        if current_hour != self.stats.hour_bucket:
            self.stats.actions_this_hour = 0
            self.stats.hour_bucket = current_hour

    def _gather_event_text(self) -> str:
        try:
            recent = self.events.get_history(limit=10)
            parts = []
            for event in recent:
                event_type = getattr(event, "event_type", "")
                if event_type:
                    parts.append(event_type)
            return " ".join(parts)
        except Exception:
            return ""

    def status(self) -> dict:
        return {
            "running": self._running,
            "mode": self.config.mode.value,
            "timezone": self.config.timezone,
            "in_quiet_hours": self._in_quiet_hours(),
            "config": {
                "mode": self.config.mode.value,
                "timezone": self.config.timezone,
                "tick_interval_secs": self.config.tick_interval_secs,
                "initiative_confidence_threshold": self.config.initiative_confidence_threshold,
                "max_actions_per_hour": self.config.max_actions_per_hour,
                "quiet_hours_start": self.config.quiet_hours_start,
                "quiet_hours_end": self.config.quiet_hours_end,
            },
            "stats": self.stats.as_dict(),
        }
