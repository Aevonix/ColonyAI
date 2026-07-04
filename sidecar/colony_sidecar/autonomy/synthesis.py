"""ConversationSynthesisTask — periodically scan stored conversation memories
for implicit goals, commitments, and action items.

Architecture:
  1. Query recent episodic Memory nodes from Neo4j
  2. Parse turn summaries back into ConversationMessage objects
  3. Run GoalInferencePipeline.process_history() on each batch
  4. Feed candidates to GoalEngine for deduplication and creation
  5. Persist last-processed watermark to avoid re-processing

This complements live goal inference (GoalEngine.on_message) by catching
items that were missed in real-time or that only become clear in retrospect
across multiple turns.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from colony_sidecar.goals.inference import (
    ConversationMessage,
    GoalDeduplicator,
    GoalInferencePipeline,
    InferenceCandidate,
)
from colony_sidecar.goals.models import GoalPriority, GoalSource, GoalStatus

logger = logging.getLogger(__name__)


# ── State persistence ───────────────────────────────────────────────────────

@dataclass
class SynthesisState:
    """Persistent watermark for the synthesis task."""

    last_processed_at: Optional[str] = None  # ISO-8601
    memories_processed: int = 0
    goals_created: int = 0
    last_run_at: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "SynthesisState":
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return cls(
                    last_processed_at=data.get("last_processed_at"),
                    memories_processed=data.get("memories_processed", 0),
                    goals_created=data.get("goals_created", 0),
                    last_run_at=data.get("last_run_at"),
                )
            except Exception:
                logger.warning("Failed to load synthesis state from %s", path)
        return cls()

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.__dict__, indent=2))
        except Exception as exc:
            logger.warning("Failed to save synthesis state: %s", exc)


# ── Memory parsing ────────────────────────────────────────────────────────

def _parse_turn_content(content: str) -> List[ConversationMessage]:
    """Parse a turn summary back into ConversationMessage objects.

    WARNING: This is tightly coupled to the format produced by
    ``turns_sync`` in ``api/routers/host.py``. If that format changes
    (e.g. prefix wording, line breaks), this parser will silently fall
    back to treating the entire content as a single user message.

    turns_sync currently stores combined summaries as:
        User: {text}
        Assistant: {text}

    Returns a list of ConversationMessage objects (user first, then assistant).
    """
    messages: List[ConversationMessage] = []
    if not content:
        return messages

    # Try to split on "User:" and "Assistant:" prefixes
    lines = content.split("\n")
    current_role: Optional[str] = None
    current_parts: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("User:"):
            # Flush previous
            if current_role and current_parts:
                messages.append(ConversationMessage(
                    message_id=f"turn_{len(messages)}",
                    role=current_role,
                    content=" ".join(current_parts).strip(),
                ))
            current_role = "user"
            current_parts = [stripped[5:].strip()]
        elif stripped.startswith("Assistant:"):
            if current_role and current_parts:
                messages.append(ConversationMessage(
                    message_id=f"turn_{len(messages)}",
                    role=current_role,
                    content=" ".join(current_parts).strip(),
                ))
            current_role = "assistant"
            current_parts = [stripped[10:].strip()]
        else:
            if current_role:
                current_parts.append(stripped)

    # Flush final
    if current_role and current_parts:
        messages.append(ConversationMessage(
            message_id=f"turn_{len(messages)}",
            role=current_role,
            content=" ".join(current_parts).strip(),
        ))

    # Fallback: if no prefixes found, treat entire content as user message
    if not messages and content.strip():
        messages.append(ConversationMessage(
            message_id="turn_0",
            role="user",
            content=content.strip(),
        ))

    return messages


# ── Main task ───────────────────────────────────────────────────────────

class ConversationSynthesisTask:
    """Scheduler task that scans conversation memories for implicit goals.

    Args:
        registry: The AutonomyLoop's subsystem registry (provides graph, goals)
        lookback_hours: How far back to look for memories (default 2.0)
        min_confidence: Minimum inference confidence to create a goal (default 0.35)
        max_memories_per_run: Safety cap on memories to process per tick (default 50)
        max_goals_per_run: Safety cap on goals to create per tick (default 5)
        state_file: Path to persist watermark state
        telemetry: Optional TelemetryStore for health monitoring
    """

    def __init__(
        self,
        registry: Any,
        lookback_hours: float = 2.0,
        min_confidence: float = 0.35,
        max_memories_per_run: int = 50,
        max_goals_per_run: int = 5,
        state_file: Optional[Path] = None,
        telemetry: Any = None,
    ) -> None:
        self.registry = registry
        self.lookback_hours = lookback_hours
        self.min_confidence = min_confidence
        self.max_memories_per_run = max_memories_per_run
        self.max_goals_per_run = max_goals_per_run
        self.state_file = state_file or (Path.home() / ".colony" / "data" / "autonomy_synthesis.json")
        self.telemetry = telemetry
        self.state = SynthesisState.load(self.state_file)
        self.pipeline = GoalInferencePipeline(min_confidence=min_confidence)
        self._deduplicator = GoalDeduplicator()

    # ------------------------------------------------------------------
    # Scheduler interface
    # ------------------------------------------------------------------

    async def run(self) -> Dict[str, Any]:
        """Execute one synthesis pass. Called by the scheduler.

        Returns:
            Dict with ``memories_scanned``, ``candidates_found``, ``goals_created``.
        """
        start = time.monotonic()
        graph = getattr(self.registry, "graph", None)
        goals = getattr(self.registry, "goals", None)

        if graph is None:
            logger.warning("ConversationSynthesis: no graph available")
            return {"memories_scanned": 0, "candidates_found": 0, "goals_created": 0, "error": "no_graph"}

        if goals is None:
            logger.warning("ConversationSynthesis: no goals engine available")
            return {"memories_scanned": 0, "candidates_found": 0, "goals_created": 0, "error": "no_goals"}

        # 1. Fetch recent episodic memories
        memories = await self._fetch_recent_memories(graph)
        if not memories:
            logger.debug("ConversationSynthesis: no new memories in last %.1f hours", self.lookback_hours)
            return {"memories_scanned": 0, "candidates_found": 0, "goals_created": 0}

        # 2. Filter out already-processed memories (by parsed timestamp)
        watermark = self.state.last_processed_at
        watermark_dt = self._parse_dt(watermark) if watermark else None
        new_memories = []
        for mem in memories:
            mem_time = mem.get("created_at")
            mem_dt = self._parse_dt(mem_time) if mem_time else None
            if watermark_dt and mem_dt and mem_dt <= watermark_dt:
                continue
            new_memories.append(mem)

        if not new_memories:
            logger.debug("ConversationSynthesis: all %d memories already processed", len(memories))
            return {"memories_scanned": 0, "candidates_found": 0, "goals_created": 0}

        # Safety cap
        capped = False
        if len(new_memories) > self.max_memories_per_run:
            logger.info(
                "ConversationSynthesis: capping %d memories to %d",
                len(new_memories),
                self.max_memories_per_run,
            )
            new_memories = new_memories[: self.max_memories_per_run]
            capped = True

        # 3. Process each memory
        all_candidates: List[InferenceCandidate] = []
        for mem in new_memories:
            content = mem.get("content", "")
            messages = _parse_turn_content(content)
            if not messages:
                continue

            # Run inference pipeline on parsed messages.
            # process_message already returns None for non-user roles,
            # so candidates only come from user messages.
            candidates = self.pipeline.process_history(messages)
            all_candidates.extend(candidates)

        # 4. Deduplicate candidates against each other (same tick)
        seen_titles: set[str] = set()
        unique_candidates: List[InferenceCandidate] = []
        for cand in all_candidates:
            normalized = cand.title.lower().strip()
            if normalized not in seen_titles:
                seen_titles.add(normalized)
                unique_candidates.append(cand)

        # 5. Load existing non-terminal goals for cross-run deduplication
        existing_goals: List[Any] = []
        try:
            proposed = goals.list_goals(status=GoalStatus.PROPOSED, limit=100)
            active = goals.list_goals(status=GoalStatus.ACTIVE, limit=100)
            existing_goals = proposed + active
        except Exception as exc:
            logger.warning("ConversationSynthesis: failed to load existing goals: %s", exc)

        # 6. Create goals via GoalEngine (with dedup, rate limit, and merge)
        goals_created = 0
        for cand in unique_candidates:
            # Stop if we've hit the per-run cap
            if goals_created >= self.max_goals_per_run:
                logger.info(
                    "ConversationSynthesis: hit max_goals_per_run (%d), skipping remaining candidates",
                    self.max_goals_per_run,
                )
                break

            # Check against existing goals
            if existing_goals:
                similarity = self._deduplicator.check(cand, existing_goals)
                if similarity:
                    if similarity.recommendation == "duplicate":
                        logger.debug(
                            "ConversationSynthesis: skipping duplicate '%s' (sim=%.2f, goal=%s)",
                            cand.title,
                            similarity.similarity_score,
                            similarity.goal_id_b,
                        )
                        continue
                    elif similarity.recommendation == "merge":
                        # Find the existing goal, merge context, and persist
                        for goal in existing_goals:
                            if goal.goal_id == similarity.goal_id_b:
                                self._deduplicator.merge(goal, cand)
                                # Persist merged changes back to the store
                                try:
                                    goals._store.save_goal(goal)
                                except Exception as save_exc:
                                    logger.warning(
                                        "ConversationSynthesis: failed to persist merge for goal %s: %s",
                                        goal.goal_id,
                                        save_exc,
                                    )
                                logger.info(
                                    "ConversationSynthesis: merged '%s' into existing goal %s (sim=%.2f)",
                                    cand.title,
                                    goal.goal_id,
                                    similarity.similarity_score,
                                )
                                break
                        continue

            # Root-cause guard: conversation memory content can carry raw
            # messaging/skill markup (e.g. <<RCSCTX ...>> injected by the RCS
            # bridge, or [IMPORTANT: ...skill...] system turns). Do not mint
            # goals from system/skill-origin turns, and store only clean titles
            # so downstream follow-ups are meaningful rather than junk.
            from colony_sidecar.delivery.reachout_policy import (
                is_system_origin, sanitize_text,
            )
            if is_system_origin(cand.title) or is_system_origin(cand.description):
                logger.debug(
                    "ConversationSynthesis: skipping system/skill-origin candidate %r",
                    str(cand.title)[:80],
                )
                continue
            clean_title = sanitize_text(cand.title)
            if not clean_title:
                logger.debug("ConversationSynthesis: skipping empty-after-sanitise candidate")
                continue
            clean_description = sanitize_text(cand.description) or clean_title

            try:
                goal = goals.propose_goal(
                    title=clean_title,
                    description=clean_description,
                    priority=cand.priority,
                    source=GoalSource.INFERRED,
                    context={
                        "inferred_confidence": cand.confidence,
                        "inferred_signals": [s.value for s in cand.signals],
                        "source_messages": cand.source_messages,
                    },
                )
                if goal is not None:
                    goals_created += 1
                    existing_goals.append(goal)  # prevent duplicates within same run
                    logger.info(
                        "ConversationSynthesis: created goal '%s' (confidence=%.2f, signals=%s)",
                        cand.title,
                        cand.confidence,
                        [s.value for s in cand.signals],
                    )
            except Exception as exc:
                logger.warning("ConversationSynthesis: failed to create goal '%s': %s", cand.title, exc)

        # 7. Update watermark and state
        newest_time = max(
            (m.get("created_at") for m in new_memories if m.get("created_at")),
            default=None,
        )
        if newest_time and not capped:
            self.state.last_processed_at = newest_time
        self.state.memories_processed += len(new_memories)
        self.state.goals_created += goals_created
        self.state.last_run_at = datetime.now(timezone.utc).isoformat()
        self.state.save(self.state_file)

        # 8. Telemetry
        if self.telemetry is not None and hasattr(self.telemetry, "touch"):
            try:
                await self.telemetry.touch("last_synthesis_at")
            except Exception:
                pass

        duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            "ConversationSynthesis: scanned=%d candidates=%d goals_created=%d (%.1fms)",
            len(new_memories),
            len(unique_candidates),
            goals_created,
            duration_ms,
        )

        return {
            "memories_scanned": len(new_memories),
            "candidates_found": len(unique_candidates),
            "goals_created": goals_created,
            "duration_ms": round(duration_ms, 1),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_dt(iso_str: str) -> Optional[datetime]:
        """Parse an ISO-8601 string to a timezone-aware datetime.

        Handles both 'Z' suffix and '+00:00' offset.
        """
        if not iso_str:
            return None
        try:
            normalized = iso_str.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)
        except (ValueError, TypeError):
            logger.debug("ConversationSynthesis: failed to parse datetime '%s'", iso_str)
            return None

    async def _fetch_recent_memories(
        self,
        graph: Any,
    ) -> List[Dict[str, Any]]:
        """Return recent episodic Memory nodes from Neo4j."""
        query = (
            "MATCH (m:Memory) "
            "WHERE m.type = 'episodic' "
            "  AND m.created_at >= datetime() - duration({hours: $hours}) "
            "RETURN m.id AS id, m.content AS content, "
            "       toString(m.created_at) AS created_at, m.strength AS strength "
            "ORDER BY m.created_at DESC "
            "LIMIT $limit"
        )

        # Support both ColonyGraph (driver.session) and mock with execute()
        if hasattr(graph, "driver"):
            async with graph.driver.session(database=graph.database) as session:
                result = await session.run(
                    query,
                    hours=self.lookback_hours,
                    limit=self.max_memories_per_run * 2,  # fetch a bit more for filtering
                )
                rows = [dict(record) async for record in result]
                return rows if rows else []

        # Mock / test path
        if hasattr(graph, "execute"):
            rows = await graph.execute(query, hours=self.lookback_hours, limit=self.max_memories_per_run * 2)
            return rows if rows else []

        logger.warning("ConversationSynthesis: graph has no driver or execute method")
        return []
