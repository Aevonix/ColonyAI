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
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from colony_sidecar.autonomy.config import AutonomyConfig
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
            _broadcast = lambda e: None
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
        self._last_consolidation_hour: int = -1
        self._last_bootstrap_check: Optional[datetime] = None
        self._last_self_reflection: Optional[datetime] = None
        self._last_decay_date: Optional[str] = None
        self._last_prune_date: Optional[str] = None
        self._last_distillation_week: str = ""
        self._last_task_completion_check: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the autonomy loop. Runs until stop() is called."""
        logger.info(
            "Autonomy loop starting (tick_interval=%.0fs)",
            self.config.tick_interval_secs,
        )
        self._running = True
        self._stop_event.clear()

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

        # Phase 6: execute approved actions
        await self._phase_execute()

        # Phase 7: cognition pipeline tick
        await self._phase_cognition()

        # Phase 8: memory consolidation (hourly)
        await self._phase_memory_consolidation()

        # Phase 9: memory decay (daily)
        await self._phase_memory_decay()

        # Phase 10: memory pruning (weekly)
        await self._phase_memory_pruning()

        # Phase 11: memory distillation (weekly)
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
                        person_id = getattr(event, "person_id", None)
                        description = (
                            f"Message from {person_id}"
                            if person_id and event_type == "message_received"
                            else f"Signal: {event_type}"
                        )
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
            )

            if self._in_quiet_hours():
                initiatives = [i for i in initiatives if getattr(i, "priority", 0) >= 0.9]

            if initiatives:
                logger.info("Phase initiative: %d new proposals", len(initiatives))
            self._pending_initiatives = initiatives
            self.stats.initiatives_generated += len(initiatives)
        except Exception as exc:
            self.stats.errors += 1
            logger.error("Phase initiative error: %s", exc, exc_info=True)
            self._pending_initiatives = []

    async def _phase_execute(self) -> None:
        """Execute approved actions via direct delivery."""
        for initiative in list(self._pending_initiatives):
            if self.stats.actions_this_hour >= self.config.max_actions_per_hour:
                logger.warning("Hourly action limit reached")
                break

            delivery = self._registry.delivery
            if delivery is None:
                continue

            home = delivery.resolve_home_channel()
            if home is None:
                logger.warning("No home channel configured — cannot deliver")
                continue

            try:
                ok = await delivery.push_to_gateway(
                    platform=home["platform"],
                    chat_id=home["chat_id"],
                    message=getattr(initiative, "description", ""),
                    source="initiative",
                )
                if ok:
                    self.stats.actions_executed += 1
                    self.stats.actions_this_hour += 1
                    logger.info("Delivered initiative: %s", getattr(initiative, "id", "?"))
            except Exception as exc:
                logger.error("push_to_gateway failed: %s", exc)

        self._pending_initiatives = []

    async def _feed_pending_tasks(self, engine: Any) -> None:
        """Feed blocked goals as pending tasks."""
        goals = self._registry.goals
        if goals is None:
            return

        try:
            blocked = goals.list_goals(status="blocked", limit=20) if hasattr(goals, "list_goals") else []
            pending_tasks = []

            for goal in blocked:
                # Goal is a dataclass with attribute access
                created = goal.created_at
                days_pending = 0
                if created:
                    days_pending = (datetime.now(timezone.utc) - created).total_seconds() / 86400

                pending_tasks.append({
                    "description": goal.title or "blocked goal",
                    "days_pending": days_pending,
                    "entity_id": goal.context.get("contact_id") if goal.context else None,
                })

            if pending_tasks:
                engine.add_context("pending_tasks", pending_tasks)
        except Exception as e:
            logger.warning("Failed to feed pending tasks: %s", e)

    async def _feed_neglected_contacts(self, engine: Any) -> None:
        """Feed contacts with declining affect."""
        affect = self._registry.affect_store
        if affect is None:
            return

        try:
            # AffectStore.get_all_states() returns list of contact state dicts
            states = affect.get_all_states() if hasattr(affect, "get_all_states") else []
            neglected = []

            for state in states[:20]:
                contact_id = state.get("contact_id")
                if not contact_id:
                    continue

                # Note: field is "current_valence", not "valence"
                valence = state.get("current_valence", 0)
                if valence < -0.3:
                    neglected.append({
                        "name": contact_id,
                        "entity_id": contact_id,
                        "days_since_contact": 0,
                    })

            if neglected:
                engine.add_context("neglected_contacts", neglected)
        except Exception as e:
            logger.warning("Failed to feed neglected contacts: %s", e)

    async def _feed_commitment_reminders(self, engine: Any) -> None:
        """Feed upcoming commitments as scheduling opportunities."""
        commitments = self._registry.commitment_store
        if commitments is None:
            return

        try:
            # CommitmentStore.list() returns {"commitments": [...], "total": N}
            result = commitments.list(status=["pending"], limit=20) if hasattr(commitments, "list") else {"commitments": []}
            active = result.get("commitments", [])

            now = datetime.now(timezone.utc)
            opportunities = []

            for c in active:
                due = c.get("due_at")
                if not due:
                    continue

                if isinstance(due, str):
                    due = datetime.fromisoformat(due.replace("Z", "+00:00"))

                hours_until = (due - now).total_seconds() / 3600

                if 0 < hours_until < 48:
                    opportunities.append({
                        "description": f"Commitment due: {c.get('description', 'untitled')}",
                        "priority": 0.9 if hours_until < 4 else 0.6,
                        "rationale": f"Due in {int(hours_until)}h",
                        "action_hint": "remind_user",
                    })

            if opportunities:
                engine.add_context("scheduling_opportunities", opportunities)
        except Exception as e:
            logger.warning("Failed to feed commitment reminders: %s", e)

    async def _phase_cognition(self) -> None:
        """Run cognition pipeline tick."""
        cognition = self._registry.cognition
        if cognition is None:
            return
        try:
            if hasattr(cognition, "run_cycle"):
                result = await cognition.run_cycle()
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
                result = await cognition.self_reflect()
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
        now = datetime.now(timezone.utc)
        try:
            start_h, start_m = map(int, self.config.quiet_hours_start.split(":"))
            end_h, end_m = map(int, self.config.quiet_hours_end.split(":"))
        except (ValueError, AttributeError):
            return False

        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        current_minutes = now.hour * 60 + now.minute

        if start_minutes == 0 and end_minutes == 0:
            return False

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
            "in_quiet_hours": self._in_quiet_hours(),
            "config": {
                "tick_interval_secs": self.config.tick_interval_secs,
                "initiative_confidence_threshold": self.config.initiative_confidence_threshold,
                "max_actions_per_hour": self.config.max_actions_per_hour,
                "quiet_hours_start": self.config.quiet_hours_start,
                "quiet_hours_end": self.config.quiet_hours_end,
            },
            "stats": self.stats.as_dict(),
        }
