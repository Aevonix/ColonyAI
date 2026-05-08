"""Initiative Engine — generate proactive suggestions.

Generates:
    - Follow-up reminders
    - Relationship maintenance suggestions
    - Health insights
    - Scheduling recommendations
"""

import asyncio
import os
import uuid as _uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import logging

logger = logging.getLogger(__name__)


@dataclass
class InitiativeConfig:
    """Configuration for initiative generation thresholds."""
    
    # Contact neglect threshold (days)
    contact_neglect_days: int = 7
    
    # Goal block threshold (days before generating initiative)
    goal_block_threshold_days: int = 1
    
    # Health score threshold (below this generates alert)
    health_score_threshold: float = 70.0
    
    # Calendar gap threshold (hours — gaps larger than this are opportunities)
    calendar_gap_threshold_hours: float = 2.0
    
    # Research task age threshold (days — tasks older than this generate initiatives)
    research_task_age_days: int = 1
    
    # Signal accumulation threshold (count — above this generates initiative)
    signal_accumulation_threshold: int = 10
    
    @classmethod
    def from_env(cls) -> "InitiativeConfig":
        """Load configuration from environment variables."""
        return cls(
            contact_neglect_days=int(
                os.getenv("COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS", "7")
            ),
            goal_block_threshold_days=int(
                os.getenv("COLONY_INITIATIVE_GOAL_BLOCK_DAYS", "1")
            ),
            health_score_threshold=float(
                os.getenv("COLONY_INITIATIVE_HEALTH_THRESHOLD", "70.0")
            ),
            calendar_gap_threshold_hours=float(
                os.getenv("COLONY_INITIATIVE_GAP_THRESHOLD", "2.0")
            ),
            research_task_age_days=int(
                os.getenv("COLONY_INITIATIVE_RESEARCH_AGE_DAYS", "1")
            ),
            signal_accumulation_threshold=int(
                os.getenv("COLONY_INITIATIVE_SIGNAL_THRESHOLD", "10")
            ),
        )


class InitiativeType(str, Enum):
    """Categories of proactive suggestions."""

    FOLLOW_UP = "follow_up"
    RELATIONSHIP = "relationship"
    HEALTH = "health"
    SCHEDULING = "scheduling"
    CODING = "coding"  # Code execution / refactoring tasks


@dataclass
class Initiative:
    """A proactive suggestion.

    Attributes:
        id: Unique initiative identifier
        type: Category of suggestion
        description: Human-readable description of what to do
        priority: How important this is (0-1)
        rationale: Why this suggestion was generated
        action_hint: Optional suggested concrete action
        entity_id: Optional related entity (person, task, etc.)
        dedup_key: Optional deduplication key (prevents duplicates)
        expires_at: When this initiative is no longer relevant
        created_at: When the initiative was generated
    """

    id: str
    type: InitiativeType
    description: str
    priority: float
    rationale: str
    action_hint: Optional[str] = None
    entity_id: Optional[str] = None
    dedup_key: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)


class InitiativeEngine:
    """Generate proactive suggestions.

    Analyzes context data provided via ``add_context()`` to surface
    actionable suggestions the user hasn't explicitly asked for.

    Context categories:
    - ``pending_tasks``: List of dicts with keys ``description``, ``days_pending``
    - ``neglected_contacts``: List of dicts with ``name``, ``entity_id``, ``days_since_contact``
    - ``health_alerts``: List of dicts with ``metric``, ``value``, ``target``
    - ``scheduling_opportunities``: List of dicts with ``description``, ``priority``, ``rationale``, ``action_hint``
    - ``completed_tasks``: List of dicts with ``description``, ``entity_id``, ``result`` (Gap C)

    Args:
        graph_client: Colony graph client for relationship/entity data
        event_bus: Colony event bus for subscribing to relevant events
        mind_model: Mind model for behavioral state awareness
        goal_store: Optional GoalStore for initiative dedup cooldown
    """

    def __init__(
        self,
        graph_client: Any,
        event_bus: Any,
        mind_model: Any,
        store: Optional[Any] = None,  # InitiativeStore for persistence
        goal_store: Optional[Any] = None,  # GoalStore for dedup cooldown (v0.7.10)
        config: Optional[InitiativeConfig] = None,
    ) -> None:
        self.graph = graph_client
        self.events = event_bus
        self.mind_model = mind_model
        self._store = store
        self._goal_store = goal_store
        self._config = config or InitiativeConfig.from_env()
        self._initiatives: List[Initiative] = []
        self._context: Dict[str, List[Dict[str, Any]]] = {}
        
        # Track last graph load to avoid redundant queries within same tick
        self._last_graph_load: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    def add_context(
        self,
        context_type: str,
        items: List[Dict[str, Any]],
    ) -> None:
        """Provide context data for initiative generation.

        Args:
            context_type: One of "pending_tasks", "neglected_contacts",
                "health_alerts", "scheduling_opportunities"
            items: List of context item dicts (schema depends on type)
        """
        if context_type not in self._context:
            self._context[context_type] = []
        self._context[context_type].extend(items)
        logger.debug("Added %d items to context '%s'", len(items), context_type)

    def clear_context(self, context_type: Optional[str] = None) -> None:
        """Clear context data, optionally for a specific type only.

        Args:
            context_type: If provided, only clear this type; else clear all.
        """
        if context_type:
            self._context.pop(context_type, None)
        else:
            self._context.clear()

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Graph context loading (NEW — fixes broken generation)
    # ------------------------------------------------------------------

    async def _load_graph_context(self) -> None:
        """Load live data from graph and mind model into self._context.
        
        This is the core fix — it populates the context dict that
        _generate_* methods read from. All queries are defensive:
        if a subsystem is unavailable, that category is skipped.
        """
        # Avoid redundant loads within same tick
        if self._last_graph_load and (
            datetime.now(timezone.utc) - self._last_graph_load
        ) < timedelta(seconds=10):
            return
        
        self._last_graph_load = datetime.now(timezone.utc)
        
        # Load all data sources in parallel where possible
        await asyncio.gather(
            self._load_blocked_goals(),
            self._load_neglected_contacts(),
            self._load_health_trends(),
            self._load_scheduling_opportunities(),
            self._load_pending_signals(),
            self._load_pending_research_tasks(),
            return_exceptions=True,  # Don't let one failure kill others
        )

    async def _load_blocked_goals(self) -> None:
        """Query graph for blocked goals."""
        if self.graph is None:
            return
        
        try:
            query = """
            MATCH (g:Goal {status: 'blocked'})
            WHERE g.blocked_at < datetime() - duration('P%dD')
            RETURN g.id as id, g.title as title, g.description as description,
                   g.blocked_at as blocked_at, g.priority as priority
            ORDER BY g.priority DESC, g.blocked_at ASC
            """ % self._config.goal_block_threshold_days
            
            results = await self.graph.run_query(query)
            
            tasks = []
            for record in results:
                blocked_at = record.get("blocked_at")
                days_pending = 0
                if blocked_at:
                    if isinstance(blocked_at, str):
                        blocked_at = datetime.fromisoformat(blocked_at.replace('Z', '+00:00'))
                    days_pending = (datetime.now(timezone.utc) - blocked_at).days
                
                tasks.append({
                    "entity_id": record["id"],
                    "description": record.get("title", "Unknown goal"),
                    "days_pending": days_pending,
                    "priority": record.get("priority", 0.5),
                })
            
            self._context["pending_tasks"] = tasks
            logger.debug("Loaded %d blocked goals", len(tasks))
        except Exception as e:
            logger.debug("Blocked goals query failed: %s", e)
            self._context.setdefault("pending_tasks", [])

    async def _load_neglected_contacts(self) -> None:
        """Query graph for contacts with no recent interaction."""
        if self.graph is None:
            return
        
        try:
            query = """
            MATCH (p:Person)
            WHERE p.last_interaction < datetime() - duration('P%dD')
              OR p.last_interaction IS NULL
            RETURN p.id as id, p.name as name, p.last_interaction as last_interaction
            ORDER BY p.last_interaction ASC
            """ % self._config.contact_neglect_days
            
            results = await self.graph.run_query(query)
            
            contacts = []
            for record in results:
                last_interaction = record.get("last_interaction")
                days_since = self._config.contact_neglect_days
                if last_interaction:
                    if isinstance(last_interaction, str):
                        last_interaction = datetime.fromisoformat(
                            last_interaction.replace('Z', '+00:00')
                        )
                    days_since = (datetime.now(timezone.utc) - last_interaction).days
                
                contacts.append({
                    "entity_id": record["id"],
                    "name": record.get("name", "Unknown"),
                    "days_since_contact": days_since,
                })
            
            self._context["neglected_contacts"] = contacts
            logger.debug("Loaded %d neglected contacts", len(contacts))
        except Exception as e:
            logger.debug("Neglected contacts query failed: %s", e)
            self._context.setdefault("neglected_contacts", [])

    async def _load_health_trends(self) -> None:
        """Query mind model for health anomalies."""
        if self.mind_model is None:
            return
        
        try:
            health_state = await self.mind_model.get_health_state()
            alerts = []
            
            # Check sleep score
            sleep_score = health_state.get("sleep_score")
            if sleep_score is not None and sleep_score < self._config.health_score_threshold:
                alerts.append({
                    "metric": "sleep_score",
                    "value": sleep_score,
                    "target": self._config.health_score_threshold,
                    "rationale": f"Sleep score ({sleep_score}) below threshold",
                })
            
            # Check recovery score
            recovery_score = health_state.get("recovery_score")
            if recovery_score is not None and recovery_score < self._config.health_score_threshold:
                alerts.append({
                    "metric": "recovery_score",
                    "value": recovery_score,
                    "target": self._config.health_score_threshold,
                    "rationale": f"Recovery score ({recovery_score}) below threshold",
                })
            
            # Check HRV trend
            hrv_trend = health_state.get("hrv_trend")
            if hrv_trend is not None and hrv_trend < -10:
                alerts.append({
                    "metric": "hrv_trend",
                    "value": hrv_trend,
                    "target": 0,
                    "rationale": f"HRV declining ({hrv_trend}%)",
                })
            
            self._context["health_alerts"] = alerts
            logger.debug("Loaded %d health alerts", len(alerts))
        except Exception as e:
            logger.debug("Health trends query failed: %s", e)
            self._context.setdefault("health_alerts", [])

    async def _load_scheduling_opportunities(self) -> None:
        """Query mind model for calendar gaps and overdue commitments."""
        if self.mind_model is None:
            return
        
        try:
            schedule_state = await self.mind_model.get_schedule_state()
            opportunities = []
            
            # Check for calendar gaps > threshold hours
            gaps = schedule_state.get("gaps", [])
            for gap in gaps:
                duration = gap.get("duration_hours", 0)
                if duration > self._config.calendar_gap_threshold_hours:
                    opportunities.append({
                        "description": f"Free block: {duration:.1f} hours ({gap['start']} to {gap['end']})",
                        "priority": 0.5,
                        "rationale": "Good time for deep work or catching up",
                        "action_hint": "schedule",
                    })
            
            # Check for overdue commitments
            overdue = schedule_state.get("overdue_commitments", [])
            for commitment in overdue:
                opportunities.append({
                    "description": f"Overdue: {commitment.get('title', 'Unknown')}",
                    "priority": 0.85,
                    "rationale": f"{commitment.get('days_overdue', 0)} days overdue",
                    "action_hint": "notify_user",
                })
            
            self._context["scheduling_opportunities"] = opportunities
            logger.debug("Loaded %d scheduling opportunities", len(opportunities))
        except Exception as e:
            logger.debug("Scheduling opportunities query failed: %s", e)
            self._context.setdefault("scheduling_opportunities", [])

    async def _load_pending_signals(self) -> None:
        """Get count of unprocessed signals."""
        if self.mind_model is None:
            return
        
        try:
            count = await self.mind_model.get_pending_signal_count()
            if count > self._config.signal_accumulation_threshold:
                # Add as a single "meta" opportunity
                self._context.setdefault("scheduling_opportunities", []).append({
                    "description": f"{count} unprocessed signals awaiting review",
                    "priority": min(0.9, 0.5 + count * 0.01),
                    "rationale": "Accumulated behavioral signals need processing",
                    "action_hint": "process_signals",
                })
            logger.debug("Pending signals: %d", count)
        except Exception as e:
            logger.debug("Pending signals query failed: %s", e)

    async def _load_pending_research_tasks(self) -> None:
        """Query graph for pending research tasks."""
        if self.graph is None:
            return
        
        try:
            query = """
            MATCH (t:Task {type: 'research', status: 'pending'})
            WHERE t.created_at < datetime() - duration('P%dD')
            RETURN t.id as id, t.title as title, t.description as description,
                   t.priority as priority, t.created_at as created_at
            ORDER BY t.priority DESC, t.created_at ASC
            """ % self._config.research_task_age_days
            
            results = await self.graph.run_query(query)
            
            # Add to pending_tasks context (research tasks are a type of pending task)
            existing_tasks = self._context.get("pending_tasks", [])
            for record in results:
                existing_tasks.append({
                    "entity_id": record["id"],
                    "description": f"Research: {record.get('title', 'Unknown')}",
                    "days_pending": self._config.research_task_age_days,
                    "priority": record.get("priority", 0.5),
                })
            
            self._context["pending_tasks"] = existing_tasks
            logger.debug("Loaded %d research tasks", len(results))
        except Exception as e:
            logger.debug("Research tasks query failed: %s", e)
            self._context.setdefault("pending_tasks", [])

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        types: Optional[List[InitiativeType]] = None,
        min_priority: float = 0.5,
        cooldown_tasks: float = 12.0,
        cooldown_contacts: float = 72.0,
    ) -> List[Initiative]:
        """Generate proactive suggestions with deduplication.

        Args:
            types: If provided, only generate these types. None = all types.
            min_priority: Minimum priority threshold (0-1)
            cooldown_tasks: Don't repeat task initiatives within N hours (default 12)
            cooldown_contacts: Don't repeat contact initiatives within N hours (default 72)
        """
        # NEW: Load live data from graph before generating
        await self._load_graph_context()
        
        initiatives: List[Initiative] = []

        if not types or InitiativeType.FOLLOW_UP in types:
            initiatives.extend(await self._generate_follow_ups())
            initiatives.extend(await self._generate_task_completion_follow_ups())

        if not types or InitiativeType.RELATIONSHIP in types:
            initiatives.extend(await self._generate_relationship_suggestions())

        if not types or InitiativeType.HEALTH in types:
            initiatives.extend(await self._generate_health_suggestions())

        if not types or InitiativeType.SCHEDULING in types:
            initiatives.extend(await self._generate_scheduling_suggestions())

        filtered = [i for i in initiatives if i.priority >= min_priority]

        # Dedup: filter by cooldown per entity (v0.7.10)
        now = datetime.now(timezone.utc)
        deduped = []
        for init in filtered:
            entity_id = getattr(init, "entity_id", None)
            init_type = getattr(init, "type", InitiativeType.FOLLOW_UP)
            type_val = init_type.value if hasattr(init_type, "value") else str(init_type)

            # Use goal_store cooldown for task-type initiatives
            if self._goal_store and entity_id and type_val == "follow_up":
                try:
                    goal = self._goal_store.get_goal(entity_id)
                    if goal and goal.last_initiative_at:
                        cooldown = timedelta(hours=cooldown_tasks)
                        if (now - goal.last_initiative_at) < cooldown:
                            logger.debug(
                                "Skipping initiative for %s: last initiative %s ago (< %dh cooldown)",
                                entity_id, now - goal.last_initiative_at, cooldown_tasks,
                            )
                            continue
                except Exception:
                    pass  # If goal not found, allow generation

            # In-memory cooldown for non-task types (contacts, etc.)
            # Uses dedup_key as cooldown tracker
            if init.dedup_key and type_val != "follow_up":
                cooldown = timedelta(hours=cooldown_contacts)
                # Check initiative store for recent initiatives with same dedup_key
                if self._store:
                    try:
                        existing = self._store.get_by_dedup_key(init.dedup_key)
                        if existing and existing.is_active:
                            existing_time = existing.created_at
                            if existing_time and (now - existing_time) < cooldown:
                                logger.debug(
                                    "Skipping initiative for %s: within %dh cooldown",
                                    init.dedup_key, cooldown_contacts,
                                )
                                continue
                    except Exception:
                        pass

            deduped.append(init)

        result = sorted(deduped, key=lambda i: i.priority, reverse=True)

        # Persist to store if available
        if self._store:
            for initiative in result:
                try:
                    # Generate dedup_key from entity_id if not set
                    if not initiative.dedup_key and initiative.entity_id:
                        initiative.dedup_key = f"{initiative.type.value}:{initiative.entity_id}"

                    self._store.create(
                        type=initiative.type.value,
                        description=initiative.description,
                        priority=initiative.priority,
                        rationale=initiative.rationale,
                        action_hint=initiative.action_hint,
                        entity_id=initiative.entity_id,
                        dedup_key=initiative.dedup_key,
                        expires_at=initiative.expires_at,
                        source_type="autonomy",
                        created_by="initiative_engine",
                    )
                except Exception as e:
                    logger.warning("Failed to persist initiative %s: %s", initiative.id, e)

            # Mark initiative as generated on the goal (v0.7.10)
            if self._goal_store and initiative.entity_id:
                try:
                    self._goal_store.mark_initiative_generated(initiative.entity_id)
                except Exception as e:
                    logger.debug("Failed to mark initiative generated for %s: %s", initiative.entity_id, e)

        logger.debug(
            "Generated %d initiatives (%d above threshold %.2f)",
            len(initiatives),
            len(result),
            min_priority,
        )
        return result

    async def complete(self, initiative_id: str, result: str = "") -> None:
        """Mark an initiative as completed.
        
        Args:
            initiative_id: ID of the initiative to complete
            result: Optional result/description of what was done
        """
        self._initiatives = [i for i in self._initiatives if i.id != initiative_id]
        
        if self._store:
            try:
                self._store.update_status(
                    initiative_id,
                    status="completed",
                    metadata={"result": result, "completed_at": datetime.now(timezone.utc).isoformat()},
                )
            except Exception as e:
                logger.warning("Failed to mark initiative %s complete: %s", initiative_id, e)
        
        if self._goal_store:
            try:
                self._goal_store.complete_task(initiative_id, result=result)
            except Exception as e:
                logger.debug("Failed to complete goal %s: %s", initiative_id, e)
        
        logger.info("Completed initiative %s: %s", initiative_id, result)

    async def acknowledge(self, initiative_id: str) -> None:
        """Acknowledge an initiative (mark as seen but not acted on).
        
        Args:
            initiative_id: ID of the initiative to acknowledge
        """
        if self._store:
            try:
                self._store.update_status(
                    initiative_id,
                    status="acknowledged",
                    metadata={"acknowledged_at": datetime.now(timezone.utc).isoformat()},
                )
            except Exception as e:
                logger.warning("Failed to acknowledge initiative %s: %s", initiative_id, e)
        
        logger.debug("Acknowledged initiative %s", initiative_id)

    async def dismiss(self, initiative_id: str) -> None:
        """Dismiss an initiative so it won't be surfaced from the active list.

        Args:
            initiative_id: ID of the initiative to dismiss
        """
        self._initiatives = [i for i in self._initiatives if i.id != initiative_id]

        # Update store if available
        if self._store:
            try:
                self._store.cancel(initiative_id, cancelled_by="initiative_engine", reason="dismissed")
            except Exception as e:
                logger.warning("Failed to dismiss initiative %s in store: %s", initiative_id, e)

        logger.debug("Dismissed initiative %s", initiative_id)

    async def get_active(self) -> List[Initiative]:
        """Get all non-expired active initiatives.

        Returns:
            Active initiatives sorted by priority (descending)
        """
        # Load from store if available
        if self._store:
            try:
                stored = self._store.list(
                    status=["pending", "assigned", "acknowledged"],
                    limit=100,
                )
                # Convert StoredInitiative to Initiative
                from datetime import datetime as dt
                result = []
                for s in stored:
                    # Check expiry
                    if s.expires_at and dt.now(timezone.utc) > s.expires_at:
                        continue
                    result.append(Initiative(
                        id=s.id,
                        type=InitiativeType(s.type),
                        description=s.description,
                        priority=s.priority,
                        rationale=s.rationale or "",
                        action_hint=s.action_hint,
                        entity_id=s.entity_id,
                        dedup_key=s.dedup_key,
                        expires_at=s.expires_at,
                        created_at=s.created_at,
                    ))
                return sorted(result, key=lambda i: i.priority, reverse=True)
            except Exception as e:
                logger.warning("Failed to load from store, using in-memory: %s", e)

        # Fallback to in-memory
        now = datetime.now(timezone.utc)
        active = [
            i for i in self._initiatives
            if i.expires_at is None or i.expires_at > now
        ]
        return sorted(active, key=lambda i: i.priority, reverse=True)

    # ------------------------------------------------------------------
    # Generators
    # ------------------------------------------------------------------

    async def _generate_follow_ups(self) -> List[Initiative]:
        """Generate follow-up suggestions from pending tasks in context."""
        initiatives: List[Initiative] = []
        for item in self._context.get("pending_tasks", []):
            desc = item.get("description", "pending task")
            days = float(item.get("days_pending", 0))
            entity_id = item.get("entity_id")
            # Priority grows with time pending, capped at 1.0
            priority = min(1.0, 0.4 + days * 0.1)
            initiatives.append(
                Initiative(
                    id=f"followup-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.FOLLOW_UP,
                    description=f"Follow up on: {desc}",
                    priority=priority,
                    rationale=f"Task has been pending for {days:.0f} day(s)",
                    action_hint=f"Review status of '{desc}'",
                    entity_id=entity_id,
                    dedup_key=f"follow_up:{entity_id}" if entity_id else None,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                )
            )
        return initiatives

    async def _generate_task_completion_follow_ups(self) -> List[Initiative]:
        """Generate follow-up initiatives for recently completed background tasks (Gap C)."""
        initiatives: List[Initiative] = []
        for task in self._context.get("completed_tasks", []):
            desc = task.get("description", "background task")
            entity_id = task.get("entity_id")
            initiatives.append(
                Initiative(
                    id=f"task-done-{_uuid_module.uuid4().hex[:8]}",
                    type=InitiativeType.FOLLOW_UP,
                    description=f"Task completed: {desc}",
                    priority=0.6,
                    rationale="Background task finished with result",
                    action_hint=None,  # Deliver to user, don't auto-execute
                    entity_id=entity_id,
                    dedup_key=f"task_done:{entity_id}" if entity_id else None,
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
                )
            )
        return initiatives

    async def _generate_relationship_suggestions(self) -> List[Initiative]:
        """Generate relationship maintenance suggestions from neglected contacts."""
        initiatives: List[Initiative] = []
        for contact in self._context.get("neglected_contacts", []):
            name = contact.get("name", "contact")
            days = float(contact.get("days_since_contact", 0))
            entity_id = contact.get("entity_id")
            priority = min(1.0, 0.3 + days * 0.05)
            initiatives.append(
                Initiative(
                    id=f"relationship-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.RELATIONSHIP,
                    description=f"Reach out to {name}",
                    priority=priority,
                    rationale=f"No contact with {name} for {days:.0f} day(s)",
                    action_hint=f"Send a quick message to {name}",
                    entity_id=entity_id,
                    dedup_key=f"relationship:{entity_id}" if entity_id else None,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                )
            )
        return initiatives

    async def _generate_health_suggestions(self) -> List[Initiative]:
        """Generate health-related suggestions from health alert context."""
        initiatives: List[Initiative] = []
        for alert in self._context.get("health_alerts", []):
            metric = alert.get("metric", "health metric")
            value = alert.get("value")
            target = alert.get("target")

            if value is not None and target is not None and target != 0:
                deviation = abs(float(value) - float(target)) / abs(float(target))
                priority = min(1.0, 0.4 + deviation * 0.6)
            else:
                priority = 0.5

            initiatives.append(
                Initiative(
                    id=f"health-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.HEALTH,
                    description=f"Review {metric}: current={value}, target={target}",
                    priority=priority,
                    rationale=f"{metric} is outside target range",
                    action_hint=f"Check and adjust {metric}",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                )
            )
        return initiatives

    async def _generate_scheduling_suggestions(self) -> List[Initiative]:
        """Generate scheduling recommendations from opportunity context."""
        initiatives: List[Initiative] = []
        for slot in self._context.get("scheduling_opportunities", []):
            desc = slot.get("description", "scheduling opportunity")
            priority = float(slot.get("priority", 0.5))
            initiatives.append(
                Initiative(
                    id=f"schedule-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.SCHEDULING,
                    description=desc,
                    priority=min(1.0, priority),
                    rationale=slot.get("rationale", "Based on observed patterns"),
                    action_hint=slot.get("action_hint"),
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
            )
        return initiatives
