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

    # Capability gap failure threshold (count — capabilities with at least
    # this many recorded failures in the recent window generate initiatives)
    capability_gap_failures: int = 3

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
            capability_gap_failures=_int("COLONY_CAPABILITY_GAP_FAILURES", 3),
        )


class InitiativeType(str, Enum):
    """Categories of proactive suggestions."""

    FOLLOW_UP = "follow_up"
    RELATIONSHIP = "relationship"
    INTRODUCTION = "introduction"  # propose connecting two people (owner-approved)
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

    # Agent-actionable initiatives (v0.13.0)
    AGENT_ACTION = "agent_action"

    # Autonomous work domains (v0.16.0) — Colony as the agent's
    # general-purpose work engine, not just relationship upkeep.
    # Context durability per type lives in initiatives/context_freshness.py.
    COMMITMENT = "commitment"      # promises and follow-through (durable)
    CALENDAR = "calendar"          # calendar awareness and prep (volatile)
    RESEARCH = "research"          # long-running research tasks (durable)
    TASK = "task"                  # task management and follow-up (durable)
    PROJECT = "project"            # milestones and project management (durable)
    SYSTEM = "system"              # infrastructure monitoring (volatile)


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
    # Logical (un-bucketed) dedup key. For recurring types, generate() rewrites dedup_key to a
    # per-period key and sets dedup_base to the original, so the store re-arms each period yet
    # still suppresses a duplicate while a prior instance is in flight. None for one-shot types.
    dedup_base: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trigger_data: Optional[Dict[str, Any]] = None


# Recurring initiative classes keyed by their dedup_key PREFIX (the part before the first ":"),
# mapped to a re-arm period in seconds. An initiative of one of these classes gets a per-period
# dedup_key so it can fire again each period; anything not listed (deliver, commitment,
# capability_gap, knowledge_gap, behavioral_correction, agent_action, ...) stays one-shot.
RECURRENCE_INTERVALS_SECS: Dict[str, int] = {
    "relationship": 72 * 3600,
    "health": 24 * 3600,
    "followup": 12 * 3600,
    "coding": 6 * 3600,
    "task": 6 * 3600,
    "calendar": 6 * 3600,
    "research": 12 * 3600,
    "project": 12 * 3600,
    "system": 6 * 3600,
    "subsystem": 6 * 3600,
    "dataquality": 12 * 3600,
    "operational": 12 * 3600,
}

# Bounded headroom for proactive social proposals (introductions). They are
# low-priority by design, so a busy operational loop saturating the cap would
# otherwise permanently starve them. Surface at most this many per cycle even
# when the cap is full (0 disables).
_INTRO_HEADROOM = max(0, int(os.environ.get("COLONY_INTRO_HEADROOM", "2")))


def _apply_recurrence_buckets(initiatives: "List[Initiative]") -> None:
    """For each recurring initiative, move its dedup_key to dedup_base and append a time bucket,
    so the store re-arms it every period instead of being permanently blocked by the prior
    period's terminal record. Mutates in place; no-op for one-shot types or already-bucketed."""
    now_ts = datetime.now(timezone.utc).timestamp()
    for init in initiatives:
        key = getattr(init, "dedup_key", None)
        if not key or getattr(init, "dedup_base", None):
            continue
        prefix = key.split(":", 1)[0]
        interval = RECURRENCE_INTERVALS_SECS.get(prefix)
        if not interval:
            continue
        bucket = int(now_ts // interval)
        init.dedup_base = key
        init.dedup_key = f"{key}:{bucket}"


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
        observation_store: Optional[Any] = None,  # agent-reported snapshots (v0.16.0)
    ) -> None:
        self.graph = graph_client
        self.events = event_bus
        self.mind_model = mind_model
        self._store = store
        self._goal_store = goal_store
        self._observation_store = observation_store
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
        """Convert Neo4j datetime or string to a tz-aware (UTC) Python datetime.

        Always normalizes to tz-aware UTC. A naive datetime reaching the
        callers (which subtract it from ``datetime.now(timezone.utc)``) raises
        ``TypeError: can't subtract offset-naive and offset-aware datetimes``,
        which the surrounding ``except Exception`` swallows — silently dropping
        every blocked-goal / pending-research initiative for that query variant.
        Normalizing here keeps that whole class of initiative from vanishing.
        """
        if value is None:
            return None
        dt: Optional[datetime] = None
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        elif hasattr(value, 'to_native'):
            # neo4j.time.DateTime
            dt = value.to_native()
        elif isinstance(value, datetime):
            dt = value
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

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
        # New self-initiative loaders (v0.11.1)
        if "capability_gaps" not in self._context:
            loaders.append(self._load_capability_gaps())
        if "knowledge_gaps" not in self._context:
            loaders.append(self._load_knowledge_gaps())
        if "behavioral_patterns" not in self._context:
            loaders.append(self._load_behavioral_patterns())
        
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

        # Owner exclusion is a relationship-domain policy: the agent must
        # not generate "check in with" work targeting its own operator.
        # Fail closed — if the owner's identity can't be established we
        # generate nothing rather than risk targeting the owner. (The old
        # COLONY_HOST_CONTACT_ID default of "owner" never matched anything,
        # which let the owner through every time.)
        from colony_sidecar.identity.resolver import (
            OwnerIdentityError,
            get_identity_resolver,
        )
        resolver = get_identity_resolver()
        try:
            await resolver.owner_identities()
        except OwnerIdentityError as exc:
            logger.critical(
                "Owner identity unresolved — skipping neglected-contact "
                "scan (fail closed): %s", exc,
            )
            self._context["neglected_contacts"] = []
            return

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
            RETURN p.id as id, p.name as name, p.score as score, days_since
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

                    # Skip the owner (any identifier format) and junk nodes
                    if await resolver.is_owner(entity_id) or await resolver.is_owner(name):
                        continue
                    if not self._is_meaningful_contact(name):
                        logger.debug("Skipping junk contact: %s (%s)", name, entity_id)
                        continue

                    if entity_id is not None and days_since is not None:
                        contacts.append({
                            "entity_id": entity_id,
                            "name": name,
                            "days_since_contact": int(days_since),
                            "relationship_score": record.get("score"),
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

    async def _load_capability_gaps(self) -> None:
        """Query graph for capabilities that have failed repeatedly.

        Schema-adaptive (same idiom as ``_load_blocked_goals``): the
        primary variant reads ``failure_count`` / ``last_failure_at``
        directly from :Capability nodes — the fields actually defined in
        ``intelligence/graph/schema.py``. The fallback variant reads the
        same counters from [:NEEDS_CAPABILITY] relationships, which is
        where the original v0.11.1 design recorded failures.

        The failure threshold comes from ``COLONY_CAPABILITY_GAP_FAILURES``
        (default 3) and only failures within the last 24 hours qualify.
        """
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        queries = [
            # Schema reality: failure counters live on the Capability node
            """
                MATCH (c:Capability)
                WHERE c.failure_count >= $threshold
                  AND c.last_failure_at > datetime() - duration({hours: 24})
                RETURN c.id as id, c.name as name, c.failure_count as failure_count,
                       c.last_failure_at as last_failure, c.status as failure_mode
                ORDER BY c.failure_count DESC
                LIMIT 10
            """,
            # v0.11.1 design: counters live on the NEEDS_CAPABILITY edge
            """
                MATCH (a:Agent)-[r:NEEDS_CAPABILITY]->(c:Capability)
                WHERE r.failure_count >= $threshold
                  AND r.last_failure_at > datetime() - duration({hours: 24})
                RETURN c.id as id, c.name as name, r.failure_count as failure_count,
                       r.last_failure_at as last_failure, r.failure_mode as failure_mode
                ORDER BY r.failure_count DESC
                LIMIT 10
            """,
        ]

        gaps = []
        for query in queries:
            if gaps:
                break
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    result = await session.run(
                        query,
                        threshold=self._config.capability_gap_failures,
                    )
                    async for record in result:
                        record = dict(record)
                        gaps.append({
                            "id": record.get("id") or record.get("name") or "unknown",
                            "name": record.get("name", "Unknown"),
                            "failure_count": record.get("failure_count", 0),
                            "failure_mode": record.get("failure_mode", "unknown"),
                            "last_failure": record.get("last_failure"),
                            "entity_type": "capability_gap",
                        })
                if gaps:
                    logger.debug("Loaded %d capability gaps", len(gaps))
            except Exception as e:
                logger.debug("Capability gap query variant failed: %s", e)
                continue

        self._context["capability_gaps"] = gaps
        if not gaps:
            self._context.setdefault("capability_gaps", [])

    async def _load_knowledge_gaps(self) -> None:
        """Query graph for open, low-confidence :Concept nodes.

        The Concept node type exists in ``intelligence/graph/schema.py``
        (confidence_score, encounter_count, status, last_researched_at).
        Concepts researched within the last 7 days are skipped so the
        same gap is not re-proposed while research is still fresh.
        """
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        query = """
            MATCH (c:Concept)
            WHERE c.confidence_score < 0.5
              AND c.status IN ['open', 'researching']
              AND (c.last_researched_at IS NULL
                   OR c.last_researched_at < datetime() - duration({days: 7}))
            RETURN c.id as id, c.name as name, c.confidence_score as confidence_score,
                   c.encounter_count as encounter_count, c.domain as domain,
                   c.source as source
            ORDER BY c.confidence_score ASC, c.encounter_count DESC
            LIMIT 10
        """

        gaps = []
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query)
                async for record in result:
                    record = dict(record)
                    gaps.append({
                        "id": record.get("id") or record.get("name") or "unknown",
                        "name": record.get("name", "Unknown"),
                        "confidence_score": record.get("confidence_score", 0.0),
                        "encounter_count": record.get("encounter_count", 0),
                        "domain": record.get("domain", "general"),
                        "source": record.get("source", "unknown"),
                        "entity_type": "knowledge_gap",
                    })
            logger.debug("Loaded %d knowledge gaps", len(gaps))
        except Exception as e:
            logger.debug("Knowledge gap query failed: %s", e)

        self._context["knowledge_gaps"] = gaps
        if not gaps:
            self._context.setdefault("knowledge_gaps", [])

    async def _load_behavioral_patterns(self) -> None:
        """Query graph for active, recurring behavioral patterns.

        The graph :Pattern node (``intelligence/graph/schema.py``) carries
        ``pattern_type`` (default 'behavioral'; 'correction' is the value
        the v0.11.1 design used for owner corrections) plus two occurrence
        counters (``recurrence_count`` and the older ``occurrences``).
        Both pattern_type values and both counters are accepted here.
        Note this is distinct from the SQLite PatternStore in
        ``colony_sidecar/patterns/`` whose pattern_type values
        (entity_cooccurrence, relation_frequency, ...) never reach the
        graph.
        """
        if self.graph is None or not hasattr(self.graph, 'driver'):
            return

        query = """
            MATCH (p:Pattern)
            WHERE p.pattern_type IN ['behavioral', 'correction']
              AND p.is_active = true
              AND coalesce(p.recurrence_count, p.occurrences, 0) >= 3
              AND (p.last_triggered_at IS NULL
                   OR p.last_triggered_at > datetime() - duration({days: 30}))
            RETURN p.id as id, p.trigger as trigger, p.action as action,
                   coalesce(p.recurrence_count, p.occurrences, 0) as recurrence_count,
                   p.confidence as confidence, p.pattern_type as pattern_type
            ORDER BY recurrence_count DESC, p.confidence DESC
            LIMIT 10
        """

        patterns = []
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query)
                async for record in result:
                    record = dict(record)
                    patterns.append({
                        "id": record.get("id", "unknown"),
                        "trigger": record.get("trigger", ""),
                        "action": record.get("action", ""),
                        "recurrence_count": record.get("recurrence_count", 0),
                        "confidence": record.get("confidence", 0.5),
                        "pattern_type": record.get("pattern_type", "behavioral"),
                        "entity_type": "behavioral_pattern",
                    })
            logger.debug("Loaded %d behavioral patterns", len(patterns))
        except Exception as e:
            logger.debug("Behavioral pattern query failed: %s", e)

        self._context["behavioral_patterns"] = patterns
        if not patterns:
            self._context.setdefault("behavioral_patterns", [])

    # ------------------------------------------------------------------
    # Self-initiative generators (v0.11.0)
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

        # Load agent-reported observations (v0.16.0, agent-as-sensor)
        self._load_observation_domains()

        # Bug 33: Run generators in parallel with exception handling
        generators = []
        if not types or InitiativeType.FOLLOW_UP in types:
            generators.append(self._generate_follow_ups())
            generators.append(self._generate_task_completion_follow_ups())
        if not types or InitiativeType.RELATIONSHIP in types:
            generators.append(self._generate_relationship_suggestions())
        if not types or InitiativeType.INTRODUCTION in types:
            generators.append(self._generate_introduction_initiatives())
        if not types or InitiativeType.HEALTH in types:
            generators.append(self._generate_health_suggestions())
        if not types or InitiativeType.SCHEDULING in types:
            generators.append(self._generate_scheduling_suggestions())
        # Autonomous work domains (v0.16.0)
        if not types or InitiativeType.COMMITMENT in types:
            generators.append(self._generate_commitment_initiatives())
        if not types or InitiativeType.CODING in types:
            generators.append(self._generate_coding_initiatives())
        if not types or InitiativeType.TASK in types:
            generators.append(self._generate_task_initiatives())
        if not types or InitiativeType.CALENDAR in types:
            generators.append(self._generate_calendar_initiatives())
        if not types or InitiativeType.RESEARCH in types:
            generators.append(self._generate_research_initiatives())
        if not types or InitiativeType.PROJECT in types:
            generators.append(self._generate_project_initiatives())
        if not types or InitiativeType.SYSTEM in types:
            generators.append(self._generate_system_initiatives())
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
        # Agent-action generators (v0.13.0)
        if not types or InitiativeType.AGENT_ACTION in types:
            generators.append(self._generate_agent_action_initiatives())
        
        initiatives: List[Initiative] = []
        if generators:
            results = await asyncio.gather(*generators, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning("Generator failed: %s", result)
                else:
                    initiatives.extend(result)

        # Re-arm recurring initiatives: rewrite their dedup_key to a per-period key (and stash the
        # logical key in dedup_base) so the store creates a fresh instance each period instead of
        # being blocked forever by the prior period's terminal record. One-shot types (deliver,
        # commitment, capability_gap, ...) are absent from the map and keep their stable key.
        _apply_recurrence_buckets(initiatives)

        filtered = [i for i in initiatives if i.priority >= min_priority]

        # Dedup: filter by cooldown per entity (v0.7.10)
        now = datetime.now(timezone.utc)
        deduped = []
        for init in filtered:
            entity_id = getattr(init, "entity_id", None)
            init_type = getattr(init, "type", InitiativeType.FOLLOW_UP)
            type_val = init_type.value if hasattr(init_type, "value") else str(init_type)

            # Use initiative_store cooldown for task-type initiatives
            if self._store and entity_id and type_val == "follow_up":
                try:
                    cutoff = now - timedelta(hours=cooldown_tasks)
                    recent = self._store.list(
                        status=["pending", "assigned", "acknowledged"],
                        type=type_val,
                        created_after=cutoff,
                        limit=1,
                    )
                    if recent:
                        continue  # Still in cooldown
                except Exception as exc:
                    logger.warning("Dedup cooldown check failed for follow_up: %s", exc)
            
            # Use initiative_store cooldown for contact-type initiatives
            elif self._store and entity_id and type_val == "relationship":
                try:
                    cutoff = now - timedelta(hours=cooldown_contacts)
                    recent = self._store.list(
                        status=["pending", "assigned", "acknowledged"],
                        type=type_val,
                        created_after=cutoff,
                        limit=1,
                    )
                    if recent:
                        continue  # Still in cooldown
                except Exception as exc:
                    logger.warning("Dedup cooldown check failed for relationship: %s", exc)
            
            deduped.append(init)

        self._initiatives = self._apply_cap(deduped, max_initiatives)

        logger.info(
            "Generated %d initiatives (requested max %d, min_priority %.2f)",
            len(self._initiatives),
            max_initiatives,
            min_priority,
        )
        
        return self._initiatives

    def _apply_cap(
        self, deduped: List[Initiative], max_initiatives: int
    ) -> List[Initiative]:
        """Cap the proposal batch to ``max_initiatives`` by priority, with two
        starvation guards on the cut tail:

        - **Owed deliverables** (``agent_deliver_message``) are added back
          UNBOUNDED — someone is actively waiting on them, they are not
          discretionary, and a busy loop must never drop one.
        - **Introductions** are topped up to ``_INTRO_HEADROOM`` total —
          proactive social proposals are low-priority by design, so without a
          little reserved headroom an operational backlog would permanently
          starve them.
        """
        ranked = sorted(deduped, key=lambda i: i.priority, reverse=True)
        capped = ranked[:max_initiatives]
        if len(ranked) <= max_initiatives:
            return capped
        cut = ranked[max_initiatives:]
        owed = [i for i in cut
                if getattr(i, "action_hint", "") == "agent_deliver_message"]
        if owed:
            capped = capped + owed
        if _INTRO_HEADROOM:
            already = sum(1 for i in capped
                          if getattr(i, "type", None) == InitiativeType.INTRODUCTION)
            if already < _INTRO_HEADROOM:
                intros = [i for i in cut
                          if getattr(i, "type", None) == InitiativeType.INTRODUCTION]
                if intros:
                    capped = capped + intros[:_INTRO_HEADROOM - already]
        return capped

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

    @staticmethod
    def _is_expired(initiative: "Initiative") -> bool:
        """Whether *initiative* has passed its ``expires_at`` (no expiry → never).

        A naive ``expires_at`` is interpreted in local time (Python convention);
        an aware one is compared against UTC. Production generators use aware UTC
        timestamps; this also tolerates naive ones.
        """
        exp = initiative.expires_at
        if exp is None:
            return False
        if exp.tzinfo is None:
            return exp <= datetime.now()
        return exp <= datetime.now(timezone.utc)

    async def get_active(self) -> List[Initiative]:
        """Return current, non-expired initiatives, highest priority first.

        Unlike :meth:`get_initiatives` (which returns everything still held in
        memory), this drops anything past its ``expires_at`` so expired
        suggestions are never surfaced.
        """
        active = [i for i in self._initiatives if not self._is_expired(i)]
        active.sort(key=lambda i: i.priority, reverse=True)
        return active

    async def dismiss(self, initiative_id: str) -> bool:
        """Dismiss an initiative the user rejected — drop it from the active set.

        Mirrors :meth:`remove_initiative` but is the user-facing verb; when a
        persistence store is attached, the dismissal is also recorded there so
        the suggestion isn't immediately regenerated.

        Returns:
            True if the initiative was present, False otherwise.
        """
        removed = self.remove_initiative(initiative_id)
        if removed and self._store is not None and hasattr(self._store, "cancel"):
            try:
                self._store.cancel(initiative_id)
            except Exception:
                logger.debug("InitiativeStore.cancel failed for %s", initiative_id, exc_info=True)
        return removed

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
            entity_type=(initiative.trigger_data or {}).get("entity_type"),
            trigger_data=initiative.trigger_data or {},
            priority=initiative.priority,
        )

        # Find matching skill
        _SKILL_NAME_MAP = {
            "subsystem_health": "subsystem_health",
            "data_quality": "data_quality",
            "operational": "operational_hygiene",
            "capability_gap": "capability_gap",
            "knowledge_acquisition": "knowledge_acquisition",
            "behavioral_correction": "behavioral_correction",
        }
        skill_name = _SKILL_NAME_MAP.get(initiative.type.value, initiative.type.value)
        category = {"executor_skill": skill_name}
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
        1. The subject is not the owner (owner exclusion, fail closed)
        2. Colony has a MANAGES edge to the Person
        3. Colony has at least one communication channel to the Person
        """
        from colony_sidecar.identity.resolver import (
            OwnerIdentityError,
            get_identity_resolver,
        )
        resolver = get_identity_resolver()
        try:
            await resolver.owner_identities()
        except OwnerIdentityError as exc:
            logger.critical(
                "Owner identity unresolved — relationship initiative "
                "generation disabled (fail closed): %s", exc,
            )
            return []

        initiatives: List[Initiative] = []
        for contact in self._context.get("neglected_contacts", []):
            entity_id = contact.get("entity_id")
            name = contact.get("name", "Unknown")
            days = contact.get("days_since_contact", 0)

            # Owner exclusion — context may be fed externally (loop tick),
            # so re-check here even though the graph loader also filters.
            if not entity_id or await resolver.is_owner(entity_id) or await resolver.is_owner(name):
                logger.debug("Skipping relationship initiative for owner/empty subject: %s", name)
                continue

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
                    # Agent-directed: evaluate the relationship and choose a
                    # disposition — this is not an instruction to message.
                    action_hint="evaluate_relationship",
                    entity_id=entity_id,
                    dedup_key=f"relationship:{entity_id}",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                )
            )
        return initiatives

    async def _generate_introduction_initiatives(self) -> List[Initiative]:
        """Propose connecting two people who share related work (social-graph autonomy).

        PROPOSE-ONLY by construction: an INTRODUCTION is NOT an agent_action, so the
        loop surfaces it for the OWNER to approve and never auto-executes it. The feed
        only supplies pairs where both sides are above the trust floor and neither is
        the owner; this re-checks owner exclusion (fail closed) since context can be
        fed externally. One-shot dedup_key (no time bucket): propose a given pair once,
        and the store's terminal-dedup stops it being re-proposed after it is acted on.
        """
        from colony_sidecar.identity.resolver import (
            OwnerIdentityError,
            get_identity_resolver,
        )
        resolver = get_identity_resolver()
        try:
            await resolver.owner_identities()
        except OwnerIdentityError as exc:
            logger.critical(
                "Owner identity unresolved — introduction generation disabled "
                "(fail closed): %s", exc,
            )
            return []

        initiatives: List[Initiative] = []
        for cand in self._context.get("introduction_candidates", []):
            a_id, b_id = cand.get("a_id"), cand.get("b_id")
            if not a_id or not b_id or a_id == b_id:
                continue
            if await resolver.is_owner(a_id) or await resolver.is_owner(b_id):
                continue
            a_name = cand.get("a_name") or a_id
            b_name = cand.get("b_name") or b_id
            shared = cand.get("organization") or "shared context"
            lo, hi = sorted((a_id, b_id))
            initiatives.append(
                Initiative(
                    id=f"intro-{lo}-{hi}",
                    type=InitiativeType.INTRODUCTION,
                    description=f"Introduce {a_name} and {b_name} (both at {shared})",
                    # Must clear the generate() min_priority confidence gate
                    # (autonomy default 0.7). An intro is high-CONFIDENCE (real
                    # shared-work signal + both above the trust floor), just
                    # low-urgency; the cap's social headroom handles volume.
                    priority=0.7,
                    rationale=f"Both are connected to {shared} but may not know each other",
                    # Owner-directed proposal: the agent never sends an intro unprompted.
                    action_hint="propose_introduction",
                    entity_id=f"{lo}:{hi}",
                    dedup_key=f"intro:{lo}:{hi}",
                    expires_at=datetime.now(timezone.utc) + timedelta(days=7),
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

    async def _generate_commitment_initiatives(self) -> List[Initiative]:
        """Generate commitment follow-through initiatives (v0.16.0).

        Reads ``upcoming_commitments`` context fed by the autonomy loop
        from Colony's own commitment store. The OWNER IS A VALID SUBJECT
        here — "follow up on what you promised the owner" is exactly the work
        this type exists for, so no owner-exclusion filter applies.
        Context is durable: the promise and its deadline do not go stale
        the way CI status does.
        """
        initiatives: List[Initiative] = []
        now = datetime.now(timezone.utc)
        for c in self._context.get("upcoming_commitments", []):
            commitment_id = c.get("commitment_id")
            if not commitment_id:
                continue
            desc = c.get("description", "untitled commitment")
            hours_until_due = c.get("hours_until_due")
            overdue = bool(c.get("overdue"))

            # An IMMEDIATE OWED DELIVERABLE (the introspection reflex's second output:
            # "text me the result") is not a reminder — the host must actually SEND it.
            # Route it to an agent_action carrying the recipient + content, so the queue
            # worker delivers it. Falls through to a normal reminder if it is malformed.
            meta = c.get("metadata") or {}
            is_deliverable = (
                (meta.get("kind") == "deliverable" or c.get("source_type") == "introspection")
                and not meta.get("delivered")
            )
            if is_deliverable:
                content = str(meta.get("content") or "").strip()
                person = c.get("person_id") or ""
                channel = meta.get("channel_hint") or "dm"
                if content and person:
                    initiatives.append(
                        Initiative(
                            id=f"deliver-{commitment_id}",
                            type=InitiativeType.AGENT_ACTION,
                            description=f"Deliver to {person} via {channel}: {content}",
                            # max priority: someone is actively waiting on this, so it must
                            # win the generation cap over routine proactive initiatives.
                            priority=1.0,
                            rationale="Owed deliverable from a recent conversation (introspection)",
                            action_hint="agent_deliver_message",
                            entity_id=commitment_id,
                            dedup_key=f"deliver:{commitment_id}",
                            expires_at=now + timedelta(hours=24),
                            # carried into the job context; RECIPIENT is the target_param the
                            # graduated policy resolves to auto-pass an authorized recipient.
                            trigger_data={
                                "RECIPIENT": person,
                                "MSG": content,
                                "channel_hint": channel,
                                "commitment_id": commitment_id,
                            },
                        )
                    )
                    continue  # do not ALSO emit a reminder for this commitment

            if overdue:
                priority = 0.9
                rationale = "Commitment is overdue"
                description = f"Follow up on overdue commitment: {desc}"
            else:
                priority = 0.85 if (hours_until_due is not None and hours_until_due < 4) else 0.7
                rationale = (
                    f"Due in {int(hours_until_due)}h"
                    if hours_until_due is not None else "Deadline approaching"
                )
                description = f"Follow up on commitment: {desc}"

            initiatives.append(
                Initiative(
                    id=f"commitment-{commitment_id}",
                    type=InitiativeType.COMMITMENT,
                    description=description,
                    priority=min(1.0, priority),
                    rationale=rationale,
                    action_hint="commitment_check_deadline",
                    entity_id=commitment_id,
                    dedup_key=f"commitment:{commitment_id}",
                    expires_at=now + timedelta(days=2),
                    trigger_data={
                        "commitment_text": desc,
                        "deadline": c.get("due_at"),
                        "status": c.get("status", "pending"),
                        "person_id": c.get("person_id"),
                        "hours_until_due": hours_until_due,
                    },
                )
            )
        return initiatives

    # ------------------------------------------------------------------
    # Per-entity context rebuild (v0.16.0)
    # ------------------------------------------------------------------

    async def rebuild_context(
        self,
        type_value: str,
        entity_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Rebuild the context snapshot for ONE initiative's subject.

        This is the volatile-context refresh path: the batch loaders
        (``_load_*_context``) only run during the generation tick, so the
        agent calls this (via POST /initiatives/{id}/context/refresh)
        before acting on a snapshot that has outlived its freshness TTL.

        Returns None when no per-entity rebuilder is registered for the
        type — callers must surface that, not silently serve stale data.
        New volatile types (calendar, coding, system) MUST register a
        rebuilder here when their batch loader lands.
        """
        rebuilders = {
            "relationship": self._rebuild_relationship_context,
            "commitment": self._rebuild_commitment_context,
        }
        rebuilder = rebuilders.get(type_value)
        if rebuilder is not None:
            return await rebuilder(entity_id)
        # Observation-backed domains share one rebuilder: the freshest
        # agent-reported snapshot for this entity.
        if type_value in self._OBSERVATION_DOMAIN_TYPES:
            return self._rebuild_observation_context(type_value, entity_id)
        return None

    async def _rebuild_relationship_context(
        self, entity_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Re-query days-since-contact and score for one Person node."""
        if not entity_id or self.graph is None or not hasattr(self.graph, "driver"):
            return None
        query = """
            MATCH (p:Person {id: $id})
            WITH p,
                 coalesce(p.lastCommunication, p.lastInteraction, p.lastSeen) AS last_seen
            RETURN p.name AS name, p.score AS score,
                   CASE WHEN last_seen IS NULL THEN null
                        ELSE duration.inDays(last_seen, datetime()).days END AS days_since
        """
        try:
            async with self.graph.driver.session(database=self.graph.database) as session:
                result = await session.run(query, id=entity_id)
                record = await result.single()
                if record is None:
                    return None
                record = dict(record)
                return {
                    "neglected_contact": {
                        "contact_id": entity_id,
                        "contact_name": record.get("name"),
                        "days_since_contact": record.get("days_since"),
                        "relationship_score": record.get("score"),
                    },
                    "context_captured_at": datetime.now(timezone.utc).isoformat(),
                }
        except Exception as exc:
            logger.warning("Relationship context rebuild failed for %s: %s", entity_id, exc)
            return None

    async def _rebuild_commitment_context(
        self, entity_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Re-read one commitment from the commitment store."""
        if not entity_id:
            return None
        try:
            # Late-bound like SubsystemRegistry — the engine is constructed
            # before the stores in some boot orders.
            from colony_sidecar.api.routers.host import _commitment_store
        except Exception:
            return None
        if _commitment_store is None or not hasattr(_commitment_store, "get"):
            return None
        try:
            commitment = _commitment_store.get(entity_id)
        except Exception as exc:
            logger.warning("Commitment context rebuild failed for %s: %s", entity_id, exc)
            return None
        if not commitment:
            return None
        return {
            "commitment": {
                "commitment_id": entity_id,
                "commitment_text": commitment.get("description"),
                "deadline": commitment.get("due_at"),
                "status": commitment.get("status"),
                "person_id": commitment.get("person_id"),
            },
            "context_captured_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Observation-backed domains (v0.16.0, agent-as-sensor)
    # ------------------------------------------------------------------
    # The agent observes the world through its own Hermes connections
    # and reports snapshots to the observation store. These loaders and
    # generators read observations — Colony never calls external APIs.

    _OBSERVATION_DOMAIN_TYPES = (
        "coding", "task", "calendar", "research", "project", "system",
    )

    def _obs_store(self) -> Any:
        """Observation store, late-bound like the other host singletons."""
        if self._observation_store is not None:
            return self._observation_store
        try:
            from colony_sidecar.api.routers.observations import get_observation_store
            return get_observation_store()
        except Exception:
            return None

    def _load_observation_domains(self) -> None:
        """Populate context for each observed domain (batch mode).

        Respects manually-fed context: a domain key that already exists
        (e.g. injected by a test or the loop) is left untouched.
        """
        store = self._obs_store()
        if store is None:
            return
        for domain in self._OBSERVATION_DOMAIN_TYPES:
            if self._context.get(domain):
                continue
            try:
                observations = store.list(domain, limit=100)
            except Exception as exc:
                logger.warning("Observation load failed for %s: %s", domain, exc)
                continue
            if observations:
                self._context[domain] = [
                    {
                        "entity_id": o.entity_id,
                        "observed_at": o.observed_at.isoformat(),
                        **(o.payload or {}),
                    }
                    for o in observations
                ]

    @staticmethod
    def _parse_iso(value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _observation_condition_cleared(domain: str, payload: Dict[str, Any]) -> bool:
        """Has the condition that justified an initiative gone away?

        Used by the refresh path to auto-close volatile initiatives
        instead of surfacing stale context (CI green again, service
        recovered, meeting already over).
        """
        healthy = {"healthy", "ok", "active", "up", "passing", "success", "green"}
        if domain == "coding":
            ci = str(payload.get("ci_status") or "").lower()
            ci_ok = (not ci) or ci in healthy
            return ci_ok and not payload.get("review_requested")
        if domain == "system":
            status = str(payload.get("status") or "").lower()
            error_rate = float(payload.get("error_rate") or 0.0)
            return status in healthy and error_rate <= 0.1
        if domain == "calendar":
            start = InitiativeEngine._parse_iso(payload.get("start_time"))
            return start is not None and start < datetime.now(timezone.utc)
        if domain == "task":
            return str(payload.get("state") or "").lower() not in ("open", "")
        if domain == "project":
            return int(payload.get("open_issues") or 0) == 0
        if domain == "research":
            return str(payload.get("status") or "").lower() in (
                "done", "complete", "published", "released",
            )
        return False

    def _rebuild_observation_context(
        self, domain: str, entity_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Per-entity refresh = the freshest stored observation."""
        if not entity_id:
            return None
        store = self._obs_store()
        if store is None:
            return None
        try:
            obs = store.get(domain, entity_id)
        except Exception as exc:
            logger.warning("Observation rebuild failed for %s/%s: %s", domain, entity_id, exc)
            return None
        if obs is None:
            return None
        return {
            domain: {"entity_id": entity_id, **(obs.payload or {})},
            "context_captured_at": obs.observed_at.isoformat(),
            "condition_cleared": self._observation_condition_cleared(domain, obs.payload or {}),
        }

    async def _generate_coding_initiatives(self) -> List[Initiative]:
        """PRs needing review and failing CI, from agent observations."""
        initiatives: List[Initiative] = []
        now = datetime.now(timezone.utc)
        for item in self._context.get("coding", []):
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            title = item.get("title", entity_id)
            ci = str(item.get("ci_status") or "").lower()
            ci_failing = ci in ("failing", "failure", "failed", "error", "red")
            review_needed = bool(item.get("review_requested")) and not item.get("draft")
            if ci_failing:
                description = f"Investigate failing CI on {title}"
                action_hint = "coding_check_ci"
                priority = 0.85
                rationale = f"CI status: {item.get('ci_status')}"
            elif review_needed:
                description = f"Review PR: {title}"
                action_hint = "coding_check_ci"
                priority = 0.75
                rationale = "Review requested"
            else:
                continue
            initiatives.append(
                Initiative(
                    id=f"coding-{entity_id}",
                    type=InitiativeType.CODING,
                    description=description,
                    priority=priority,
                    rationale=rationale,
                    action_hint=action_hint,
                    entity_id=str(entity_id),
                    dedup_key=f"coding:{entity_id}",
                    expires_at=now + timedelta(hours=24),
                    trigger_data={k: v for k, v in item.items() if k != "entity_id"},
                )
            )
        return initiatives

    async def _generate_task_initiatives(self) -> List[Initiative]:
        """Open tasks that have gone stale, from agent observations."""
        initiatives: List[Initiative] = []
        now = datetime.now(timezone.utc)
        for item in self._context.get("task", []):
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            if str(item.get("state") or "open").lower() != "open":
                continue
            stale_days = float(item.get("stale_days") or 0)
            if stale_days < 3 and not item.get("needs_follow_up"):
                continue
            title = item.get("title", entity_id)
            initiatives.append(
                Initiative(
                    id=f"task-{entity_id}",
                    type=InitiativeType.TASK,
                    description=f"Follow up on task: {title}",
                    priority=min(1.0, 0.5 + stale_days / 14.0),
                    rationale=f"Open with no movement for {int(stale_days)} days"
                    if stale_days else "Flagged for follow-up",
                    action_hint="task_check_status",
                    entity_id=str(entity_id),
                    dedup_key=f"task:{entity_id}",
                    expires_at=now + timedelta(days=3),
                    trigger_data={k: v for k, v in item.items() if k != "entity_id"},
                )
            )
        return initiatives

    async def _generate_calendar_initiatives(self) -> List[Initiative]:
        """Upcoming events needing preparation, from agent observations."""
        initiatives: List[Initiative] = []
        now = datetime.now(timezone.utc)
        for item in self._context.get("calendar", []):
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            if item.get("needs_prep") is False:
                continue
            start = self._parse_iso(item.get("start_time"))
            if start is None or start < now:
                continue
            hours_until = (start - now).total_seconds() / 3600
            if hours_until > 24:
                continue
            title = item.get("title", entity_id)
            initiatives.append(
                Initiative(
                    id=f"calendar-{entity_id}",
                    type=InitiativeType.CALENDAR,
                    description=f"Prepare for: {title}",
                    priority=0.85 if hours_until <= 2 else 0.7,
                    rationale=f"Starts in {hours_until:.1f}h",
                    action_hint="calendar_prepare_meeting",
                    entity_id=str(entity_id),
                    dedup_key=f"calendar:{entity_id}",
                    expires_at=start,
                    trigger_data={k: v for k, v in item.items() if k != "entity_id"},
                )
            )
        return initiatives

    async def _generate_research_initiatives(self) -> List[Initiative]:
        """Tracked research items due a check, from agent observations."""
        initiatives: List[Initiative] = []
        now = datetime.now(timezone.utc)
        for item in self._context.get("research", []):
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            status = str(item.get("status") or "").lower()
            if status in ("done", "complete", "published", "released"):
                continue
            last_checked = self._parse_iso(item.get("last_checked"))
            days_since_check = (
                (now - last_checked).total_seconds() / 86400
                if last_checked else None
            )
            if days_since_check is not None and days_since_check < self._config.research_task_age_days:
                continue
            title = item.get("title", entity_id)
            initiatives.append(
                Initiative(
                    id=f"research-{entity_id}",
                    type=InitiativeType.RESEARCH,
                    description=f"Check research item: {title}",
                    priority=0.6,
                    rationale=(
                        f"Not checked for {int(days_since_check)} days"
                        if days_since_check is not None else "Tracked and awaiting status"
                    ),
                    action_hint="research_check_paper",
                    entity_id=str(entity_id),
                    dedup_key=f"research:{entity_id}",
                    expires_at=now + timedelta(days=7),
                    trigger_data={k: v for k, v in item.items() if k != "entity_id"},
                )
            )
        return initiatives

    async def _generate_project_initiatives(self) -> List[Initiative]:
        """Milestones approaching with open work, from agent observations."""
        initiatives: List[Initiative] = []
        now = datetime.now(timezone.utc)
        for item in self._context.get("project", []):
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            open_issues = int(item.get("open_issues") or 0)
            if open_issues == 0:
                continue
            due = self._parse_iso(item.get("due_on"))
            if due is None:
                continue
            days_until = (due - now).total_seconds() / 86400
            if days_until > 7:
                continue
            title = item.get("title", entity_id)
            initiatives.append(
                Initiative(
                    id=f"project-{entity_id}",
                    type=InitiativeType.PROJECT,
                    description=(
                        f"Milestone {title}: {open_issues} open issue(s), "
                        f"due in {max(0, int(days_until))}d"
                    ),
                    priority=0.85 if days_until <= 2 else 0.7,
                    rationale="Milestone deadline approaching with open work",
                    action_hint="project_check_progress",
                    entity_id=str(entity_id),
                    dedup_key=f"project:{entity_id}",
                    expires_at=due,
                    trigger_data={k: v for k, v in item.items() if k != "entity_id"},
                )
            )
        return initiatives

    async def _generate_system_initiatives(self) -> List[Initiative]:
        """Unhealthy services, from agent observations."""
        initiatives: List[Initiative] = []
        now = datetime.now(timezone.utc)
        healthy = {"healthy", "ok", "active", "up", "passing", "green"}
        for item in self._context.get("system", []):
            entity_id = item.get("entity_id")
            if not entity_id:
                continue
            status = str(item.get("status") or "").lower()
            error_rate = float(item.get("error_rate") or 0.0)
            if status in healthy and error_rate <= 0.1:
                continue
            initiatives.append(
                Initiative(
                    id=f"system-{entity_id}",
                    type=InitiativeType.SYSTEM,
                    description=f"Investigate {entity_id}: {item.get('status', 'degraded')}",
                    priority=0.9,
                    rationale=item.get("message")
                    or f"status={item.get('status')}, error_rate={error_rate:.2f}",
                    action_hint="system_check_health",
                    entity_id=str(entity_id),
                    dedup_key=f"system:{entity_id}",
                    expires_at=now + timedelta(hours=2),
                    trigger_data={k: v for k, v in item.items() if k != "entity_id"},
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
                    trigger_data={**issue},
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
                    trigger_data={**issue},
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
                    trigger_data={**task},
                )
            )
        return initiatives

    async def _generate_capability_gap_initiatives(self) -> List[Initiative]:
        """Generate self-initiatives proposing to acquire/repair capabilities.

        Reads the ``capability_gaps`` context populated by
        ``_load_capability_gaps()`` from :Capability nodes whose
        ``failure_count`` met the COLONY_CAPABILITY_GAP_FAILURES threshold
        in the last 24h. Limitation: no code in the current tree writes
        those failure counters (the v0.11.1 ToolExecutor detection hook
        was never implemented), so this only surfaces gaps recorded by an
        external writer or operator until that hook lands.

        Defensive: returns [] on any failure; never raises.
        """
        initiatives: List[Initiative] = []
        try:
            for gap in self._context.get("capability_gaps", []):
                entity_id = gap.get("id") or gap.get("entity_id") or "unknown"
                name = gap.get("name", "Unknown capability")
                failure_count = int(gap.get("failure_count", 0) or 0)
                failure_mode = gap.get("failure_mode", "unknown")

                # 0.5 floor, +0.05 per recorded failure, capped at 0.75
                priority = min(0.75, 0.5 + failure_count * 0.05)

                initiatives.append(
                    Initiative(
                        id=f"capgap-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                        type=InitiativeType.CAPABILITY_GAP,
                        description=f"Acquire or repair capability: {name}",
                        priority=priority,
                        rationale=(
                            f"Capability '{name}' recorded {failure_count} "
                            f"failure(s) in the last 24h (mode: {failure_mode}, "
                            f"threshold: {self._config.capability_gap_failures})"
                        ),
                        action_hint="Register, repair, or research the failing capability",
                        entity_id=str(entity_id),
                        dedup_key=f"capability_gap:{entity_id}",
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
                        trigger_data={**gap},
                    )
                )
            logger.debug("Generated %d capability gap initiatives", len(initiatives))
        except Exception as exc:
            logger.debug("Capability gap generation failed: %s", exc)
            return []
        return initiatives

    async def _generate_knowledge_acquisition_initiatives(self) -> List[Initiative]:
        """Generate research initiatives for low-confidence knowledge areas.

        Reads the ``knowledge_gaps`` context populated by
        ``_load_knowledge_gaps()`` from open/researching :Concept nodes
        with confidence_score < 0.5. Limitation: the :Concept node type
        exists in the graph schema but the v0.11.1 web-search detection
        hook that was meant to create Concept nodes was never implemented,
        so gaps only surface once something writes Concept nodes.

        Defensive: returns [] on any failure; never raises.
        """
        initiatives: List[Initiative] = []
        try:
            for gap in self._context.get("knowledge_gaps", []):
                entity_id = gap.get("id") or gap.get("entity_id") or "unknown"
                name = gap.get("name", "Unknown concept")
                confidence = float(gap.get("confidence_score", 0.0) or 0.0)
                encounter_count = int(gap.get("encounter_count", 0) or 0)

                # Lower confidence -> higher priority, within 0.5-0.75:
                # confidence 1.0 -> 0.5, confidence 0.0 -> 0.75
                priority = min(0.75, max(0.5, 0.5 + (1.0 - confidence) * 0.25))

                initiatives.append(
                    Initiative(
                        id=f"knowgap-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                        type=InitiativeType.KNOWLEDGE_ACQUISITION,
                        description=f"Research concept: {name}",
                        priority=priority,
                        rationale=(
                            f"Concept '{name}' encountered {encounter_count} "
                            f"time(s) but confidence is only {confidence:.2f}"
                        ),
                        action_hint="Queue background research and update world model",
                        entity_id=str(entity_id),
                        dedup_key=f"knowledge_gap:{entity_id}",
                        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                        trigger_data={**gap},
                    )
                )
            logger.debug("Generated %d knowledge acquisition initiatives", len(initiatives))
        except Exception as exc:
            logger.debug("Knowledge acquisition generation failed: %s", exc)
            return []
        return initiatives

    async def _generate_behavioral_correction_initiatives(self) -> List[Initiative]:
        """Generate correction initiatives for recurring behavioral patterns.

        Reads the ``behavioral_patterns`` context populated by
        ``_load_behavioral_patterns()`` from active :Pattern nodes with
        pattern_type 'behavioral' or 'correction' and 3+ occurrences.
        Limitation: the v0.11.1 correction-detection hook that was meant
        to write these Pattern nodes was never implemented, so patterns
        only surface once something writes graph :Pattern nodes (the
        SQLite PatternStore is a separate store and does not feed this).

        Defensive: returns [] on any failure; never raises.
        """
        initiatives: List[Initiative] = []
        try:
            for pattern in self._context.get("behavioral_patterns", []):
                entity_id = pattern.get("id") or pattern.get("entity_id") or "unknown"
                trigger = pattern.get("trigger") or "unspecified trigger"
                action = pattern.get("action") or ""
                recurrence = int(pattern.get("recurrence_count", 0) or 0)

                # 0.5 floor, +0.05 per recurrence, capped at 0.75
                priority = min(0.75, 0.5 + recurrence * 0.05)

                rationale = f"Pattern recurred {recurrence} time(s)"
                if action:
                    rationale += f"; expected behavior: {action[:80]}"

                initiatives.append(
                    Initiative(
                        id=f"behav-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                        type=InitiativeType.BEHAVIORAL_CORRECTION,
                        description=f"Correct recurring behavior: {trigger[:80]}",
                        priority=priority,
                        rationale=rationale,
                        action_hint="Encode the correction as a preference or config rule",
                        entity_id=str(entity_id),
                        dedup_key=f"behavioral_correction:{entity_id}",
                        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
                        trigger_data={**pattern},
                    )
                )
            logger.debug("Generated %d behavioral correction initiatives", len(initiatives))
        except Exception as exc:
            logger.debug("Behavioral correction generation failed: %s", exc)
            return []
        return initiatives

    async def _generate_agent_action_initiatives(self) -> List[Initiative]:
        """Generate agent-actionable initiatives for autonomous execution (v0.13.0).

        These initiatives are routed to the task queue instead of the delivery
        bridge, and claimed by the host agent's external worker.
        """
        initiatives: List[Initiative] = []

        # 1. Repo status check — generated at most once every 4 hours
        now = datetime.now(timezone.utc)
        last_repo_check = self._last_self_initiative_at.get("repo_status")
        if last_repo_check and (now - last_repo_check) < timedelta(hours=4):
            pass  # Still within cooldown
        else:
            self._last_self_initiative_at["repo_status"] = now
            initiatives.append(
                Initiative(
                    id=f"repo-check-{_uuid_module.uuid4().hex[:8]}",
                    type=InitiativeType.AGENT_ACTION,
                    description="Check colony-work repo for uncommitted changes",
                    priority=0.4,
                    rationale="Periodic hygiene check to prevent stale work",
                    action_hint="agent_check_repo_status",
                    entity_id="colony-work",
                    dedup_key="agent_action:agent_check_repo_status:colony-work",
                    expires_at=now + timedelta(hours=4),
                )
            )

        # 2. Health check initiatives from subsystem health context
        for issue in self._context.get("subsystem_health", [])[:3]:
            entity_id = issue.get("entity_id", "unknown")
            name = issue.get("name", "Unknown")
            status_val = issue.get("status", "unknown")
            if status_val != "active":
                initiatives.append(
                    Initiative(
                        id=f"agent-health-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                        type=InitiativeType.AGENT_ACTION,
                        description=f"Investigate degraded subsystem: {name}",
                        priority=min(1.0, 0.7 + (issue.get("error_rate", 0) or 0)),
                        rationale=f"Subsystem {name} is {status_val}",
                        action_hint="agent_investigate_subsystem",
                        entity_id=entity_id,
                        dedup_key=f"agent_action:agent_investigate_subsystem:{entity_id}",
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
                        trigger_data={**issue},
                    )
                )

        # 3. Data quality — auto-fixable issues
        for issue in self._context.get("data_quality_issues", [])[:2]:
            entity_id = issue.get("entity_id", "unknown")
            entity_type = issue.get("entity_type", "unknown")
            if entity_type == "orphan_nodes":
                initiatives.append(
                    Initiative(
                        id=f"agent-dq-{entity_id}-{_uuid_module.uuid4().hex[:8]}",
                        type=InitiativeType.AGENT_ACTION,
                        description=f"Clean up orphan nodes in {entity_id}",
                        priority=0.5,
                        rationale="Auto-fixable data quality issue",
                        action_hint="agent_cleanup_orphans",
                        entity_id=entity_id,
                        dedup_key=f"agent_action:agent_cleanup_orphans:{entity_id}",
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
                        trigger_data={**issue},
                    )
                )

        return initiatives
