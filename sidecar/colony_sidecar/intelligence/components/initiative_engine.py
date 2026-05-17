"""Initiative Engine — generate proactive suggestions.

Generates:
    - Follow-up reminders
    - Relationship maintenance suggestions
    - Health insights
    - Scheduling recommendations
"""

import asyncio
import os
import re
import unicodedata
import uuid as _uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import logging

from colony_sidecar.skills.base import ExecutionResult, InitiativeExecutionContext
from colony_sidecar.skills.registry import SkillRegistry

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
        def _int(env_var: str, default: int) -> int:
            try:
                return int(os.getenv(env_var, str(default)))
            except ValueError:
                logger.warning("Invalid %s, using default %d", env_var, default)
                return default
        
        def _float(env_var: str, default: float) -> float:
            try:
                return float(os.getenv(env_var, str(default)))
            except ValueError:
                logger.warning("Invalid %s, using default %.1f", env_var, default)
                return default
        
        return cls(
            contact_neglect_days=_int("COLONY_INITIATIVE_CONTACT_NEGLECT_DAYS", 7),
            goal_block_threshold_days=_int("COLONY_INITIATIVE_GOAL_BLOCK_DAYS", 1),
            health_score_threshold=_float("COLONY_INITIATIVE_HEALTH_THRESHOLD", 70.0),
            calendar_gap_threshold_hours=_float("COLONY_INITIATIVE_GAP_THRESHOLD", 2.0),
            research_task_age_days=_int("COLONY_INITIATIVE_RESEARCH_AGE_DAYS", 1),
            signal_accumulation_threshold=_int("COLONY_INITIATIVE_SIGNAL_THRESHOLD", 10),
        )


class InitiativeType(str, Enum):
    """Categories of proactive suggestions."""

    FOLLOW_UP = "follow_up"
    RELATIONSHIP = "relationship"
    HEALTH = "health"
    SCHEDULING = "scheduling"
    CODING = "coding"  # Code execution / refactoring tasks

    # Self-initiative types (v0.11.0)
    SUBSYSTEM_HEALTH = "subsystem_health"
    DATA_QUALITY = "data_quality"
    OPERATIONAL = "operational"
    CAPABILITY_GAP = "capability_gap"
    KNOWLEDGE_ACQUISITION = "knowledge_acquisition"
    BEHAVIORAL_CORRECTION = "behavioral_correction"


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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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

    # Common suffixes that create false duplicates (e.g. "Craig Wiener" vs "Craig Wiener US")
    _NAME_SUFFIXES = {"us", "usa", "uk", "jr", "sr", "ii", "iii", "iv", "dr", "mr", "ms"}

    @staticmethod
    def _normalize_contact_name(name: str) -> str:
        """Normalize a contact name for deduplication.
        
        Steps:
        1. Strip leading/trailing whitespace
        2. Lowercase
        3. Remove accents (NFKD decomposition)
        4. Remove common location/professional suffixes
        5. Collapse multiple spaces
        """
        if not name or not isinstance(name, str):
            return ""
        name = name.strip().lower()
        # Decompose accents: "João" → "Joao", "López" → "Lopez"
        name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
        # Remove common suffixes like " US", " USA", " Jr." etc.
        parts = name.split()
        filtered = []
        for part in parts:
            clean = part.strip(".,")
            if clean not in InitiativeEngine._NAME_SUFFIXES:
                filtered.append(clean)
        name = " ".join(filtered)
        # Collapse multiple spaces
        name = re.sub(r"\s+", " ", name).strip()
        return name

    @staticmethod
    def _is_meaningful_contact(name: str) -> bool:
        """Filter out junk/system nodes that shouldn't generate initiatives."""
        if not name or not isinstance(name, str):
            return False
        name = name.strip()
        if len(name) < 3:
            return False
        # Single word names are suspicious (unless they're known mononyms)
        words = name.split()
        if len(words) < 2:
            return False
        # Block known system/junk names
        blocked = {
            "another", "best", "can", "conversation", "episodic",
            "gateway", "has", "high", "hydrahost", "infrastructure",
            "integration", "local-llama", "logged", "memories", "memory",
            "mind", "openclaw", "phase", "process", "session", "should",
            "tmux", "vllm", "what", "unknown", "default", "none",
        }
        if name.lower() in blocked:
            return False
        # Block phone numbers and UUID-like strings
        if re.match(r"^\+?\d", name) or re.match(r"^[a-f0-9-]{8,}$", name.replace("-", "")):
            return False
        return True

    def __init__(
        self,
        graph_client: Any,
        event_bus: Any,
        mind_model: Any,
        store: Optional[Any] = None,  # InitiativeStore for persistence
        goal_store: Optional[Any] = None,  # GoalStore for dedup cooldown (v0.7.10)
        config: Optional[InitiativeConfig] = None,
        skill_registry: Optional[SkillRegistry] = None,
    ) -> None:
        self.graph = graph_client
        self.events = event_bus
        self.mind_model = mind_model
        self._store = store
        self._goal_store = goal_store
        self._config = config or InitiativeConfig.from_env()
        self._initiatives: List[Initiative] = []
        self._context: Dict[str, List[Dict[str, Any]]] = {}
        
        # Skill registry for self-initiative execution (v0.11.0)
        self._skills = skill_registry or SkillRegistry(
            graph_client=graph_client,
            event_bus=event_bus,
        )
        
        # Track last graph load to avoid redundant queries within same tick
        self._last_graph_load: Optional[datetime] = None
        
        # Track last self-initiative execution per category for cooldown
        self._last_self_initiative_at: Dict[str, datetime] = {}

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
            # Partial clear doesn't reset graph load cache
        else:
            self._context.clear()
            # Reset graph load cache so next generate() reloads from graph (Bug 37)
            self._last_graph_load = None

    # ------------------------------------------------------------------
    # Neo4j datetime helper (Bug 50/51)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_neo4j_datetime(value: Any) -> Optional[datetime]:
        """Convert Neo4j datetime or string to Python datetime."""
        if value is None:
            return None
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        if hasattr(value, 'to_native'):
            # neo4j.time.DateTime
            return value.to_native()
        if isinstance(value, datetime):
            return value
        return None

    # ------------------------------------------------------------------
    # Graph context loading
    # ------------------------------------------------------------------

    async def _load_graph_context(self) -> None:
        """Load live data from graph and mind model into self._context.
        
        This is the core fix — it populates the context dict that
        _generate_* methods read from. All queries are defensive:
        if a subsystem is unavailable, that category is skipped.
        
        Respects manually-added context: if a category already has data
        (e.g. from AutonomyLoop._feed_* methods), graph loading for that
        category is skipped to avoid duplicates.
        """
        # Avoid redundant loads within same tick
        if self._last_graph_load and (
            datetime.now(timezone.utc) - self._last_graph_load
        ) < timedelta(seconds=10):
            return
        
        self._last_graph_load = datetime.now(timezone.utc)
        
        # Only load categories that don't already have manually-fed context
        loaders = []
        if "pending_tasks" not in self._context:
            loaders.append(self._load_blocked_goals())
            loaders.append(self._load_pending_research_tasks())
        if "neglected_contacts" not in self._context:
            loaders.append(self._load_neglected_contacts())
        if "health_alerts" not in self._context:
            loaders.append(self._load_health_trends())
        if "scheduling_opportunities" not in self._context:
            loaders.append(self._load_scheduling_opportunities())
        # Always check signals unless explicitly in context (Bug 40)
        if "pending_signals" not in self._context:
            loaders.append(self._load_pending_signals())
        # Self-initiative context loaders (v0.11.0)
        if "subsystem_health" not in self._context:
            loaders.append(self._load_subsystem_health())
        if "data_quality_issues" not in self._context:
            loaders.append(self._load_data_quality_issues())
        if "operational_tasks" not in self._context:
            loaders.append(self._load_operational_tasks())
        if "initiative_categories" not in self._context:
            loaders.append(self._load_initiative_categories())
        
        if loaders:
            await asyncio.gather(*loaders, return_exceptions=True)

    async def _load_blocked_goals(self) -> None:
        """Query graph for blocked/stuck goals.

        Schema-adaptive: tries multiple property names for compatibility
        with different graph schemas. Filters out terminal states
        (abandoned, completed, cancelled) so dead goals don't generate
        follow-up initiatives forever.
        """
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        # Try multiple query variants for schema compatibility.
        # All variants exclude terminal goal states.
        queries = [
            # Colony schema (status='blocked', blocked_at)
            {
                "query": """
                    MATCH (g:Goal)
                    WHERE g.status = 'blocked'
                      AND NOT coalesce(g.state, '') IN ['abandoned', 'completed', 'cancelled']
                      AND NOT coalesce(g.status, '') IN ['abandoned', 'completed', 'cancelled']
                      AND g.blocked_at < datetime() - duration({days: $days})
                    RETURN g.id as id, g.title as title, g.description as description,
                           g.blocked_at as blocked_at, g.priority as priority
                    ORDER BY g.priority DESC, g.blocked_at ASC
                """,
                "date_field": "blocked_at",
            },
            # Alternative: state='blocked', blocked_at
            {
                "query": """
                    MATCH (g:Goal)
                    WHERE g.state = 'blocked'
                      AND NOT coalesce(g.state, '') IN ['abandoned', 'completed', 'cancelled']
                      AND NOT coalesce(g.status, '') IN ['abandoned', 'completed', 'cancelled']
                      AND (g.blocked_at < datetime() - duration({days: $days})
                           OR g.blocked_at IS NULL)
                    RETURN g.id as id, g.title as title, g.description as description,
                           g.blocked_at as blocked_at, g.priority as priority
                    ORDER BY g.priority DESC, g.blocked_at ASC
                """,
                "date_field": "blocked_at",
            },
            # Fallback: status='blocked', updated_at
            {
                "query": """
                    MATCH (g:Goal)
                    WHERE (g.status = 'blocked' OR g.state = 'blocked')
                      AND NOT coalesce(g.state, '') IN ['abandoned', 'completed', 'cancelled']
                      AND NOT coalesce(g.status, '') IN ['abandoned', 'completed', 'cancelled']
                    RETURN g.id as id, g.title as title, g.description as description,
                           g.updated_at as blocked_at, g.priority as priority
                    ORDER BY g.priority DESC, g.updated_at ASC
                """,
                "date_field": "updated_at",
            },
        ]

        tasks = []
        date_field = "blocked_at"  # default
        for variant in queries:
            if tasks:  # Stop if we found data
                break
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    result = await session.run(
                        variant["query"],
                        days=self._config.goal_block_threshold_days,
                    )
                    async for record in result:
                        record = dict(record)
                        date_field = variant["date_field"]
                        blocked_at = self._parse_neo4j_datetime(record.get(date_field))
                        # Bug 13: max(0, ...) to prevent negative days
                        days_pending = max(0, (datetime.now(timezone.utc) - blocked_at).days) if blocked_at else 0

                        tasks.append({
                            "entity_id": record["id"],
                            "description": record.get("title", "Unknown goal"),
                            "days_pending": days_pending,
                            "priority": record.get("priority", 0.5),
                        })

                if tasks:
                    logger.debug("Loaded %d blocked goals using %s", len(tasks), date_field)
            except Exception as e:
                logger.debug("Blocked goals query variant failed: %s", e)
                continue

        self._context["pending_tasks"] = tasks
        if not tasks:
            logger.debug("No blocked goals found with any schema variant")
            self._context.setdefault("pending_tasks", [])

    async def _load_neglected_contacts(self) -> None:
        """Query graph for contacts with no recent interaction.

        Only returns contacts that have an explicit interaction date
        older than the threshold. Nodes with no dates at all are NOT
        treated as neglected — they are likely uninitialised or system
        nodes. Skips the host's own contact.
        """
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        import os
        host_id = os.environ.get("COLONY_HOST_CONTACT_ID", "Jane Doe")

        query = """
            MATCH (p:Person)
            WITH p,
                 CASE
                   WHEN p.lastCommunication IS NOT NULL
                        AND p.lastCommunication < datetime() - duration({days: $days})
                     THEN duration.inDays(p.lastCommunication, datetime()).days
                   WHEN p.lastInteraction IS NOT NULL
                        AND p.lastInteraction < datetime() - duration({days: $days})
                     THEN duration.inDays(p.lastInteraction, datetime()).days
                   WHEN p.lastSeen IS NOT NULL
                        AND p.lastSeen < datetime() - duration({days: $days})
                     THEN duration.inDays(p.lastSeen, datetime()).days
                   ELSE null
                 END AS days_since
            WHERE days_since IS NOT NULL
            RETURN p.id as id, p.name as name, days_since
            ORDER BY days_since DESC
            LIMIT $limit
        """

        contacts = []
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(
                    query,
                    days=self._config.contact_neglect_days,
                    limit=100,
                )
                async for record in result:
                    record = dict(record)
                    name = record.get("name", "Unknown")
                    entity_id = record.get("id")
                    days_since = record.get("days_since")

                    # Skip host and junk/system nodes
                    if entity_id == host_id or name == host_id:
                        continue
                    if not self._is_meaningful_contact(name):
                        logger.debug("Skipping junk contact: %s (%s)", name, entity_id)
                        continue

                    if entity_id is not None and days_since is not None:
                        contacts.append({
                            "entity_id": entity_id,
                            "name": name,
                            "days_since_contact": int(days_since),
                        })

            logger.debug("Loaded %d genuinely neglected contacts", len(contacts))
        except Exception as e:
            logger.warning("Neglected contacts query failed: %s", e)

        self._context["neglected_contacts"] = contacts
        if not contacts:
            self._context.setdefault("neglected_contacts", [])

    async def _load_health_trends(self) -> None:
        """Query graph for health signals and score trends."""
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        query = """
            MATCH (s:Signal)
            WHERE s.metric IN ['energy', 'sleep', 'focus', 'mood']
              AND s.confidence > 0.7
            RETURN s.metric as metric, avg(s.value) as avg_value,
                   max(s.timestamp) as last_seen
            ORDER BY last_seen DESC
        """

        alerts = []
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query)
                async for record in result:
                    record = dict(record)
                    metric = record.get("metric")
                    avg_value = record.get("avg_value", 0.0)
                    if avg_value is not None and avg_value < self._config.health_score_threshold:
                        alerts.append({
                            "metric": metric,
                            "value": float(avg_value),
                            "target": self._config.health_score_threshold,
                        })
            logger.debug("Loaded %d health alerts", len(alerts))
        except Exception as e:
            logger.debug("Health trends query failed: %s", e)

        self._context["health_alerts"] = alerts
        if not alerts:
            self._context.setdefault("health_alerts", [])

    async def _load_scheduling_opportunities(self) -> None:
        """Query graph for scheduling gaps and opportunities."""
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        query = """
            MATCH (o:Owner)
            OPTIONAL MATCH (o)-[:HAS_CALENDAR]->(c:Calendar)
            WITH o, c
            WHERE c IS NULL OR c.last_synced < datetime() - duration({hours: 24})
            RETURN 'calendar_sync' as opportunity,
                   CASE WHEN c IS NULL THEN 0.9 ELSE 0.7 END as priority
            LIMIT 5
        """

        opportunities = []
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query)
                async for record in result:
                    record = dict(record)
                    opp_type = record.get("opportunity")
                    if opp_type == "calendar_sync":
                        opportunities.append({
                            "description": "Calendar hasn't synced in 24+ hours",
                            "priority": float(record.get("priority", 0.7)),
                            "rationale": "Missing recent calendar data may cause scheduling gaps",
                            "action_hint": "Sync calendar to get latest availability",
                        })
            logger.debug("Loaded %d scheduling opportunities", len(opportunities))
        except Exception as e:
            logger.debug("Scheduling opportunities query failed: %s", e)

        self._context["scheduling_opportunities"] = opportunities
        if not opportunities:
            self._context.setdefault("scheduling_opportunities", [])

    async def _load_pending_signals(self) -> None:
        """Query graph for signals awaiting processing."""
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        query = """
            MATCH (s:Signal)
            WHERE s.processed = false OR s.processed IS NULL
            RETURN count(s) as pending_count
        """

        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query)
                record = await result.single()
                if record:
                    count = record.get("pending_count", 0)
                    if count > self._config.signal_accumulation_threshold:
                        self._context.setdefault("pending_signals", []).append({
                            "metric": "pending_signals",
                            "value": int(count),
                            "target": self._config.signal_accumulation_threshold,
                        })
                        logger.debug("Signal accumulation: %d pending", count)
        except Exception as e:
            logger.debug("Pending signals query failed: %s", e)

    async def _load_pending_research_tasks(self) -> None:
        """Query graph for open research tasks that haven't been acted on.
        
        Schema-adaptive: tries multiple property names for compatibility.
        Tasks without a due date or with a past due date are considered pending.
        """
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return
        
        queries = [
            # Schema variant: Task with status='open' and due_at
            {
                "query": """
                    MATCH (t:Task)
                    WHERE t.status = 'open'
                      AND t.type = 'research'
                      AND (t.due_at < datetime() - duration({days: $days})
                           OR t.due_at IS NULL)
                    RETURN t.id as id, t.title as title, t.description as description,
                           t.due_at as due_at, t.priority as priority
                    ORDER BY t.priority DESC, t.due_at ASC
                    LIMIT 20
                """,
                "date_field": "due_at",
            },
            # Schema variant: Task with state='open' and updated_at
            {
                "query": """
                    MATCH (t:Task)
                    WHERE t.state = 'open'
                      AND (t.type = 'research' OR t.tags CONTAINS 'research')
                      AND (t.updated_at < datetime() - duration({days: $days})
                           OR t.updated_at IS NULL)
                    RETURN t.id as id, t.title as title, t.description as description,
                           t.updated_at as due_at, t.priority as priority
                    ORDER BY t.priority DESC, t.updated_at ASC
                    LIMIT 20
                """,
                "date_field": "updated_at",
            },
            # Fallback: any open task with 'research' in the description
            {
                "query": """
                    MATCH (t:Task)
                    WHERE (t.status = 'open' OR t.state = 'open')
                      AND (t.type = 'research' OR t.description CONTAINS 'research'
                           OR coalesce(t.tags, '') CONTAINS 'research')
                    RETURN t.id as id, t.title as title, t.description as description,
                           coalesce(t.due_at, t.updated_at, datetime()) as due_at,
                           t.priority as priority
                    ORDER BY t.priority DESC, due_at ASC
                    LIMIT 20
                """,
                "date_field": "due_at",
            },
        ]
        
        existing_tasks = self._context.get("pending_tasks", [])
        task_count = 0
        
        for variant in queries:
            if task_count > 0:
                break
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    result = await session.run(
                        variant["query"],
                        days=self._config.research_task_age_days,
                    )
                    async for record in result:
                        record = dict(record)
                        date_field = variant["date_field"]
                        due_at = self._parse_neo4j_datetime(record.get(date_field))
                        days_pending = max(0, (datetime.now(timezone.utc) - due_at).days) if due_at else 0
                        
                        existing_tasks.append({
                            "entity_id": record["id"],
                            "description": record.get("title", "Unknown research task"),
                            "days_pending": days_pending,
                            "priority": record.get("priority", 0.5),
                            "is_research": True,
                        })
                        task_count += 1
                        
                if task_count > 0:
                    logger.debug("Loaded %d research tasks using %s", task_count, date_field)
            except Exception as e:
                logger.debug("Research tasks query variant failed: %s", e)
                continue
        
        self._context["pending_tasks"] = existing_tasks
        if task_count == 0:
            logger.debug("No research tasks found with any schema variant")

    # ------------------------------------------------------------------
    # Self-initiative context loaders (v0.11.0)
    # ------------------------------------------------------------------

    async def _load_subsystem_health(self) -> None:
        """Query graph for degraded Colony subsystems."""
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        query = """
            MATCH (s:Subsystem)
            WHERE s.status <> 'active'
               OR (s.latency_ms IS NOT NULL AND s.latency_ms > 1000)
               OR (s.error_rate IS NOT NULL AND s.error_rate > 0.1)
            RETURN s.id as id, s.name as name, s.status as status,
                   s.latency_ms as latency, s.error_rate as error_rate
            ORDER BY s.error_rate DESC, s.latency_ms DESC
            LIMIT 10
        """

        issues = []
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query)
                async for record in result:
                    record = dict(record)
                    issues.append({
                        "entity_id": record.get("id") or record.get("name"),
                        "name": record.get("name", "Unknown"),
                        "status": record.get("status", "unknown"),
                        "latency_ms": record.get("latency"),
                        "error_rate": record.get("error_rate"),
                    })
            logger.debug("Loaded %d subsystem health issues", len(issues))
        except Exception as e:
            logger.debug("Subsystem health query failed: %s", e)

        self._context["subsystem_health"] = issues
        if not issues:
            self._context.setdefault("subsystem_health", [])

    async def _load_data_quality_issues(self) -> None:
        """Query graph for schema drift and orphan detection."""
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        issues = []

        # Check for Memory nodes without :ABOUT edges
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run("""
                    MATCH (m:Memory)
                    WHERE NOT (m)-[:ABOUT]->(:Person)
                    RETURN count(m) as orphan_count
                """)
                record = await result.single()
                if record and record.get("orphan_count", 0) > 0:
                    issues.append({
                        "entity_id": "orphan_memories",
                        "entity_type": "orphan_nodes",
                        "count": record.get("orphan_count"),
                        "description": f"{record.get('orphan_count')} Memory nodes without :ABOUT edges",
                    })
        except Exception as e:
            logger.debug("Orphan detection query failed: %s", e)

        # Check for schema drift: queries referencing non-existent relationships
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run("""
                    CALL db.schema.visualization() YIELD nodes, relationships
                    RETURN [r IN relationships | type(r)] as rel_types
                """)
                record = await result.single()
                if record:
                    rel_types = record.get("rel_types", [])
                    # Check if BELONGS_TO exists (it shouldn't if schema is clean)
                    if "BELONGS_TO" in rel_types:
                        issues.append({
                            "entity_id": "belongs_to_drift",
                            "entity_type": "schema_drift",
                            "description": "BELONGS_TO relationship exists but is deprecated",
                        })
        except Exception as e:
            logger.debug("Schema drift query failed: %s", e)

        self._context["data_quality_issues"] = issues
        if not issues:
            self._context.setdefault("data_quality_issues", [])

    async def _load_operational_tasks(self) -> None:
        """Check for operational hygiene needs (backups, disk space, etc.)."""
        import os
        from pathlib import Path

        tasks = []

        # Check backup age
        backup_dir = Path(os.path.expanduser("~/.colony/backups"))
        if backup_dir.exists():
            backups = sorted(backup_dir.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
            if backups:
                newest_age_days = (datetime.now(timezone.utc).timestamp() - backups[0].stat().st_mtime) / 86400
                if newest_age_days > 7:
                    tasks.append({
                        "entity_id": "database_backup",
                        "entity_type": "backup",
                        "description": f"Last backup was {newest_age_days:.0f} days ago",
                        "age_days": newest_age_days,
                    })
            else:
                tasks.append({
                    "entity_id": "database_backup",
                    "entity_type": "backup",
                    "description": "No backups found",
                    "age_days": 999,
                })

        # Check log sizes
        log_dir = Path(os.path.expanduser("~/.colony/logs"))
        if log_dir.exists():
            total_size_mb = sum(f.stat().st_size for f in log_dir.glob("*.log") if f.is_file()) / (1024 * 1024)
            if total_size_mb > 100:
                tasks.append({
                    "entity_id": "log_rotation",
                    "entity_type": "log_rotation",
                    "description": f"Log files total {total_size_mb:.1f} MB",
                    "threshold_mb": 100,
                })

        self._context["operational_tasks"] = tasks
        if not tasks:
            self._context.setdefault("operational_tasks", [])

    async def _load_initiative_categories(self) -> None:
        """Load dynamic initiative categories from the graph."""
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        query = """
            MATCH (c:InitiativeCategory)
            WHERE c.auto_execute = true
              AND (c.last_triggered IS NULL
                   OR c.last_triggered < datetime() - duration({minutes: c.cooldown_minutes}))
            RETURN c.id as id, c.name as name, c.description as description,
                   c.trigger_query as trigger_query, c.action_type as action_type,
                   c.executor_skill as executor_skill, c.priority_formula as priority_formula,
                   c.cooldown_minutes as cooldown_minutes, c.auto_execute as auto_execute,
                   c.requires_approval as requires_approval
            LIMIT 20
        """

        categories = []
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query)
                async for record in result:
                    record = dict(record)
                    categories.append({
                        "id": record.get("id"),
                        "name": record.get("name"),
                        "description": record.get("description"),
                        "trigger_query": record.get("trigger_query"),
                        "action_type": record.get("action_type"),
                        "executor_skill": record.get("executor_skill"),
                        "priority_formula": record.get("priority_formula"),
                        "cooldown_minutes": record.get("cooldown_minutes", 30),
                        "auto_execute": record.get("auto_execute", True),
                        "requires_approval": record.get("requires_approval", False),
                    })
            logger.debug("Loaded %d initiative categories", len(categories))
        except Exception as e:
            logger.debug("Initiative category query failed: %s", e)

        self._context["initiative_categories"] = categories
        if not categories:
            self._context.setdefault("initiative_categories", [])

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate(
        self,
        types: Optional[List[InitiativeType]] = None,
        min_priority: float = 0.5,
        cooldown_tasks: float = 12.0,
        cooldown_contacts: float = 72.0,
        max_initiatives: int = 20,
    ) -> List[Initiative]:
        """Generate proactive suggestions with deduplication.

        Args:
            types: If provided, only generate these types. None = all types.
            min_priority: Minimum priority threshold (0-1)
            cooldown_tasks: Don't repeat task initiatives within N hours (default 12)
            cooldown_contacts: Don't repeat contact initiatives within N hours (default 72)
            max_initiatives: Maximum number of initiatives to generate (default 20)
        """
        # Validate parameters (Bug 57, 58)
        min_priority = max(0.0, min(1.0, min_priority))
        cooldown_tasks = max(0.0, cooldown_tasks)
        cooldown_contacts = max(0.0, cooldown_contacts)
        max_initiatives = max(1, max_initiatives)
        
        # Load live data from graph before generating
        await self._load_graph_context()
        
        # Bug 33: Run generators in parallel with exception handling
        generators = []
        if not types or InitiativeType.FOLLOW_UP in types:
            generators.append(self._generate_follow_ups())
            generators.append(self._generate_task_completion_follow_ups())
        if not types or InitiativeType.RELATIONSHIP in types:
            generators.append(self._generate_relationship_suggestions())
        if not types or InitiativeType.HEALTH in types:
            generators.append(self._generate_health_suggestions())
        if not types or InitiativeType.SCHEDULING in types:
            generators.append(self._generate_scheduling_suggestions())
        # Self-initiative generators (v0.11.0)
        if not types or InitiativeType.SUBSYSTEM_HEALTH in types:
            generators.append(self._generate_subsystem_health_initiatives())
        if not types or InitiativeType.DATA_QUALITY in types:
            generators.append(self._generate_data_quality_initiatives())
        if not types or InitiativeType.OPERATIONAL in types:
            generators.append(self._generate_operational_initiatives())
        if not types or InitiativeType.CAPABILITY_GAP in types:
            generators.append(self._generate_capability_gap_initiatives())
        if not types or InitiativeType.KNOWLEDGE_ACQUISITION in types:
            generators.append(self._generate_knowledge_acquisition_initiatives())
        if not types or InitiativeType.BEHAVIORAL_CORRECTION in types:
            generators.append(self._generate_behavioral_correction_initiatives())
        
        initiatives: List[Initiative] = []
        if generators:
            results = await asyncio.gather(*generators, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning("Generator failed: %s", result)
                else:
                    initiatives.extend(result)

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
                    recent = self._goal_store.list_recent(
                        entity_type="initiative",
                        entity_id=entity_id,
                        hours=cooldown_tasks,
                    )
                    if recent:
                        continue  # Still in cooldown
                except Exception:
                    pass  # goal_store unavailable, skip cooldown check
            
            # Use goal_store cooldown for contact-type initiatives
            elif self._goal_store and entity_id and type_val == "relationship":
                try:
                    recent = self._goal_store.list_recent(
                        entity_type="initiative",
                        entity_id=entity_id,
                        hours=cooldown_contacts,
                    )
                    if recent:
                        continue  # Still in cooldown
                except Exception:
                    pass
            
            deduped.append(init)

        # Sort by priority desc
        deduped.sort(key=lambda i: i.priority, reverse=True)
        
        # Cap to max
        self._initiatives = deduped[:max_initiatives]
        
        logger.info(
            "Generated %d initiatives (requested max %d, min_priority %.2f)",
            len(self._initiatives),
            max_initiatives,
            min_priority,
        )
        
        return self._initiatives

    def get_initiatives(self) -> List[Initiative]:
        """Return current initiatives."""
        return self._initiatives.copy()

    def get_initiative(self, initiative_id: str) -> Optional[Initiative]:
        """Get a specific initiative by ID."""
        for initiative in self._initiatives:
            if initiative.id == initiative_id:
                return initiative
        return None

    def remove_initiative(self, initiative_id: str) -> bool:
        """Remove an initiative by ID.
        
        Returns:
            True if removed, False if not found.
        """
        for i, initiative in enumerate(self._initiatives):
            if initiative.id == initiative_id:
                del self._initiatives[i]
                logger.debug("Removed initiative %s", initiative_id)
                return True
        return False

    async def execute_initiative(self, initiative_id: str) -> Dict[str, Any]:
        """Execute a self-initiative using the skill registry (v0.11.0).

        Args:
            initiative_id: ID of the initiative to execute

        Returns:
            Dict with "status", "result", and "initiative" keys
        """
        initiative = self.get_initiative(initiative_id)
        if not initiative:
            return {"status": "not_found", "result": None, "initiative": None}

        # Only self-initiative types can be auto-executed
        if initiative.type not in {
            InitiativeType.SUBSYSTEM_HEALTH,
            InitiativeType.DATA_QUALITY,
            InitiativeType.OPERATIONAL,
            InitiativeType.CAPABILITY_GAP,
            InitiativeType.KNOWLEDGE_ACQUISITION,
            InitiativeType.BEHAVIORAL_CORRECTION,
        }:
            return {
                "status": "not_self_initiative",
                "result": None,
                "initiative": initiative,
            }

        # Build execution context
        exec_context = InitiativeExecutionContext(
            initiative_id=initiative.id,
            category_id=initiative.type.value,
            category_name=initiative.type.value,
            entity_id=initiative.entity_id,
            trigger_data={"description": initiative.description, "rationale": initiative.rationale},
            priority=initiative.priority,
        )

        # Find matching skill
        category = {"executor_skill": initiative.type.value.replace("_", "_")}
        skill = await self._skills.find_skill_for_category(category, {})

        if not skill:
            return {
                "status": "no_skill",
                "result": None,
                "initiative": initiative,
            }

        # Execute
        try:
            result = await skill.execute(exec_context)
            return {
                "status": "executed",
                "result": result,
                "initiative": initiative,
            }
        except Exception as e:
            logger.error("Initiative execution failed: %s", e)
            return {
                "status": "failed",
                "result": str(e),
                "initiative": initiative,
            }

    # ------------------------------------------------------------------
    # Initiative generators
    # ------------------------------------------------------------------

    async def _generate_follow_ups(self) -> List[Initiative]:
        """Generate follow-up initiatives from pending task context."""
        initiatives: List[Initiative] = []
        for task in self._context.get("pending_tasks", []):
            # Only generate follow-ups for truly blocked tasks
            days_pending = task.get("days_pending", 0)
            if days_pending < self._config.goal_block_threshold_days:
                continue
            
            desc = task.get("description", "Unknown task")
            entity_id = task.get("entity_id")
            priority = float(task.get("priority", 0.5))
            
            initiatives.append(
                Initiative(
                    id=f"followup-{entity_id or _uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.FOLLOW_UP,
                    description=f"Follow up on: {desc}",
                    priority=min(1.0, 0.5 + (days_pending / 14.0)),  # escalate over time
                    rationale=f"Task has been pending for {days_pending} days",
                    action_hint="Check status and unblock if possible",
                    entity_id=entity_id,
                    dedup_key=f"followup:{entity_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                )
            )
        return initiatives

    async def _generate_task_completion_follow_ups(self) -> List[Initiative]:
        """Generate Gap C follow-ups: tasks marked complete but with no result captured.

        Scans ``completed_tasks`` context (populated externally e.g. by
        AutonomyLoop) and produces ``FOLLOW_UP`` initiatives asking the
        user to document the outcome.
        """
        initiatives: List[Initiative] = []
        for task in self._context.get("completed_tasks", []):
            desc = task.get("description", "a completed task")
            entity_id = task.get("entity_id")
            initiatives.append(
                Initiative(
                    id=f"gapc-{entity_id or _uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.FOLLOW_UP,
                    description=f"Document outcome for: {desc}",
                    priority=0.6,
                    rationale="Task completed but no result was captured",
                    action_hint="Record what was accomplished and any next steps",
                    entity_id=entity_id,
                    dedup_key=f"gapc:{entity_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                )
            )
        return initiatives

    async def _generate_relationship_suggestions(self) -> List[Initiative]:
        """Generate relationship maintenance suggestions.

        Gates relationship initiatives behind:
        1. Colony has a MANAGES edge to the Person
        2. Colony has at least one communication channel to the Person
        """
        initiatives: List[Initiative] = []
        for contact in self._context.get("neglected_contacts", []):
            entity_id = contact.get("entity_id")
            name = contact.get("name", "Unknown")
            days = contact.get("days_since_contact", 0)

            # Gate: check if Colony MANAGES this person
            has_manages = False
            if self.graph and entity_id:
                try:
                    async with self.graph.driver.session(database=self.graph.database) as session:
                        result = await session.run(
                            "MATCH (:Agent)-[:MANAGES]->(p:Person {id: $id}) RETURN count(p) as cnt",
                            id=entity_id,
                        )
                        record = await result.single()
                        has_manages = record is not None and record.get("cnt", 0) > 0
                except Exception:
                    pass

            if not has_manages:
                logger.debug("Skipping relationship initiative for %s: no MANAGES edge", name)
                continue

            initiatives.append(
                Initiative(
                    id=f"rel-{entity_id or _uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.RELATIONSHIP,
                    description=f"Check in with {name}",
                    priority=min(1.0, 0.4 + (days / 14.0)),
                    rationale=f"No contact for {days} days",
                    action_hint="Send a message or schedule a call",
                    entity_id=entity_id,
                    dedup_key=f"relationship:{entity_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                )
            )
        return initiatives

    async def _generate_health_suggestions(self) -> List[Initiative]:
        """Generate health insights from signal context."""
        initiatives: List[Initiative] = []
        for alert in self._context.get("health_alerts", []):
            metric = alert.get("metric", "health")
            value = alert.get("value", 0.0)
            target = alert.get("target", 70.0)
            initiatives.append(
                Initiative(
                    id=f"health-{metric}-{_uuid_module.uuid4().hex[:8]}",
                    type=InitiativeType.HEALTH,
                    description=f"{metric.title()} is low ({value:.0f}% / target {target:.0f}%)",
                    priority=0.7,
                    rationale=f"{metric} below target for sustained period",
                    action_hint=f"Review {metric} patterns and adjust habits",
                    entity_id=metric,
                    dedup_key=f"health:{metric}",
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
            # Bug 45: Add dedup_key based on description hash
            dedup_key = f"schedule:{hash(desc) % 10000000}"
            initiatives.append(
                Initiative(
                    id=f"schedule-{_uuid_module.uuid4().hex[:12]}",
                    type=InitiativeType.SCHEDULING,
                    description=desc,
                    priority=min(1.0, priority),
                    rationale=slot.get("rationale", "Based on observed patterns"),
                    action_hint=slot.get("action_hint"),
                    entity_id=dedup_key.split(":", 1)[1] if ":" in dedup_key else None,
                    dedup_key=dedup_key,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
            )
        return initiatives

    # ------------------------------------------------------------------
    # Self-initiative generators (v0.11.0)
    # ------------------------------------------------------------------

    async def _generate_subsystem_health_initiatives(self) -> List[Initiative]:
        """Generate self-initiatives for degraded subsystems."""
        initiatives: List[Initiative] = []
        for issue in self._context.get("subsystem_health", []):
            entity_id = issue.get("entity_id", "unknown")
            name = issue.get("name", "Unknown")
            status = issue.get("status", "unknown")
            latency = issue.get("latency_ms")
            error_rate = issue.get("error_rate")

            priority = 0.6
            if latency and latency > 1000:
                priority = min(1.0, 0.6 + (latency - 1000) / 2000)
            if error_rate and error_rate > 0.1:
                priority = min(1.0, priority + error_rate)

            initiatives.append(
                Initiative(
                    id=f"subsys-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                    type=InitiativeType.SUBSYSTEM_HEALTH,
                    description=f"Subsystem {name} is {status}",
                    priority=priority,
                    rationale=f"Latency: {latency}ms, Error rate: {error_rate}" if latency or error_rate else "Status degraded",
                    action_hint=f"Diagnose and restart {name}" if status != "active" else "Investigate latency spike",
                    entity_id=entity_id,
                    dedup_key=f"subsystem:{entity_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )
        return initiatives

    async def _generate_data_quality_initiatives(self) -> List[Initiative]:
        """Generate self-initiatives for data quality issues."""
        initiatives: List[Initiative] = []
        for issue in self._context.get("data_quality_issues", []):
            entity_id = issue.get("entity_id", "unknown")
            entity_type = issue.get("entity_type", "unknown")
            description = issue.get("description", "Data quality issue")
            count = issue.get("count", 0)

            priority = 0.5
            if count > 10:
                priority = min(1.0, 0.5 + count / 100)

            initiatives.append(
                Initiative(
                    id=f"dq-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                    type=InitiativeType.DATA_QUALITY,
                    description=description,
                    priority=priority,
                    rationale=f"Detected {count} affected items" if count else "Schema drift detected",
                    action_hint="Run data quality fix" if entity_type == "orphan_nodes" else "Review schema migration",
                    entity_id=entity_id,
                    dedup_key=f"dataquality:{entity_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
                )
            )
        return initiatives

    async def _generate_operational_initiatives(self) -> List[Initiative]:
        """Generate self-initiatives for operational hygiene."""
        initiatives: List[Initiative] = []
        for task in self._context.get("operational_tasks", []):
            entity_id = task.get("entity_id", "unknown")
            entity_type = task.get("entity_type", "unknown")
            description = task.get("description", "Operational task")
            age_days = task.get("age_days", 0)

            priority = min(1.0, 0.4 + age_days / 14)

            initiatives.append(
                Initiative(
                    id=f"ops-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                    type=InitiativeType.OPERATIONAL,
                    description=description,
                    priority=priority,
                    rationale=f"Operational hygiene: {entity_type}",
                    action_hint="Execute maintenance task",
                    entity_id=entity_id,
                    dedup_key=f"operational:{entity_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
            )
        return initiatives

    async def _generate_capability_gap_initiatives(self) -> List[Initiative]:
        """Generate self-initiatives for missing capabilities.

        Currently a placeholder — will be populated by analyzing
        failed tool invocations and owner corrections.
        """
        return []

    async def _generate_knowledge_acquisition_initiatives(self) -> List[Initiative]:
        """Generate self-initiatives for low-confidence knowledge areas.

        Currently a placeholder — will be populated by analyzing
        project context and query patterns.
        """
        return []

    async def _generate_behavioral_correction_initiatives(self) -> List[Initiative]:
        """Generate self-initiatives for recurring correction patterns.

        Currently a placeholder — will be populated by analyzing
        memory tags and explicit corrections.
        """
        return []
