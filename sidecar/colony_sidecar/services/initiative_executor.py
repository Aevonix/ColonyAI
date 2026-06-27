"""Built-in Initiative Executor service -- closes the autonomy circuit.

Processes pending initiatives using Colony's own reasoning pipeline
(ReasoningLoop + LLMRouter + tools). This is the generic execution
backend that replaces external webhook delivery for same-machine
deployments.

What it does every cycle (default 30s):
  - Claims pending initiatives from the store
  - Builds a reasoning prompt from the initiative context
  - Runs the ReasoningLoop (LLM + Colony tools) to determine and
    execute the appropriate action
  - Reports results back (complete/fail with retry)
  - Respects rate limits and concurrency caps

Auto-starts at sidecar boot when COLONY_EXECUTOR_ENABLED is "true"
and an LLM provider is configured. Stays dormant otherwise.

Environment / config:
  COLONY_EXECUTOR_ENABLED           "false" (default) / "true"
  COLONY_EXECUTOR_CYCLE_SECS        cycle interval (default 30)
  COLONY_EXECUTOR_MAX_PER_CYCLE     max initiatives per cycle (default 5)
  COLONY_EXECUTOR_MODEL_TIER        LLM tier: small/medium/large (default "small")
  COLONY_EXECUTOR_TYPES             comma-separated initiative types to handle
                                    (default: all non-self types)
  COLONY_EXECUTOR_AGENT_ID          agent identity (default "colony-executor")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_TYPES = {
    "follow_up", "relationship", "health", "scheduling",
    "commitment", "calendar", "research", "task", "project", "system",
}

_SYSTEM_PROMPT = """\
You are an autonomous initiative executor for Colony, a personal \
intelligence system. You have been given an initiative to process.

Your job:
1. Understand what the initiative is asking for.
2. Use the available tools to gather context and take action.
3. Report a clear, concise result describing what you did.

Guidelines:
- Be direct and efficient. Use the minimum tools needed.
- If the initiative involves a person, look up their relationship first.
- If the initiative requires sending a message, compose it carefully.
- If you cannot complete the initiative (missing info, blocked), say so \
clearly so it can be retried or escalated.
- Never fabricate information. Use tools to verify facts.
"""


def _build_initiative_prompt(initiative: Any) -> str:
    itype = getattr(initiative, "type", "") or getattr(initiative, "initiative_type", "")
    desc = getattr(initiative, "description", "")
    rationale = getattr(initiative, "rationale", "")
    action_hint = getattr(initiative, "action_hint", "")
    entity_id = getattr(initiative, "entity_id", "")
    priority = getattr(initiative, "priority", 0.5)
    context = getattr(initiative, "context", None) or {}

    parts = [f"## Initiative: {itype}", f"**Priority**: {priority:.2f}"]
    if desc:
        parts.append(f"**Description**: {desc}")
    if rationale:
        parts.append(f"**Rationale**: {rationale}")
    if action_hint:
        parts.append(f"**Suggested action**: {action_hint}")
    if entity_id:
        parts.append(f"**Related entity**: {entity_id}")
    if context:
        parts.append(f"**Context**: {json.dumps(context, default=str, indent=2)}")

    parts.append(
        "\nProcess this initiative using the available tools. "
        "When done, summarize what you accomplished in 1-2 sentences."
    )
    return "\n".join(parts)


class InitiativeExecutorService:
    """Async service that executes pending initiatives via the reasoning loop."""

    def __init__(
        self,
        initiative_store: Any,
        reasoning_loop: Any,
        tool_executor: Any = None,
        cycle_secs: float = 30.0,
        max_per_cycle: int = 5,
        model_tier: str = "small",
        allowed_types: Optional[set[str]] = None,
        agent_id: str = "colony-executor",
    ):
        self._store = initiative_store
        self._reasoning = reasoning_loop
        self._tools = tool_executor
        self._cycle_secs = cycle_secs
        self._max_per_cycle = max_per_cycle
        self._model_tier = model_tier
        self._allowed_types = allowed_types or _DEFAULT_TYPES
        self._agent_id = agent_id

        self._running = False
        self._stop_event = asyncio.Event()
        self._stats = {
            "cycles": 0,
            "initiatives_processed": 0,
            "initiatives_completed": 0,
            "initiatives_failed": 0,
            "total_tool_calls": 0,
            "total_tokens": 0,
        }

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._reasoning is None:
            logger.info("Initiative executor: no reasoning loop, staying dormant")
            return

        self._running = True
        self._stop_event.clear()
        logger.info(
            "Initiative executor starting (cycle=%ds, max=%d/cycle, tier=%s, types=%s)",
            int(self._cycle_secs),
            self._max_per_cycle,
            self._model_tier,
            ",".join(sorted(self._allowed_types)),
        )

        try:
            while not self._stop_event.is_set():
                await self._cycle()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._cycle_secs
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False
            logger.info("Initiative executor stopped. Stats: %s", self._stats)

    async def stop(self) -> None:
        logger.info("Initiative executor stop requested")
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    async def _cycle(self) -> None:
        self._stats["cycles"] += 1

        pending = await self._claim_pending()
        if not pending:
            return

        for initiative in pending:
            if self._stop_event.is_set():
                break
            await self._execute_one(initiative)

    # ------------------------------------------------------------------
    # Claim pending initiatives
    # ------------------------------------------------------------------

    async def _claim_pending(self) -> list:
        if self._store is None:
            return []

        try:
            loop = asyncio.get_event_loop()
            pending = await loop.run_in_executor(
                None,
                lambda: self._store.list(
                    status=["pending"],
                    limit=self._max_per_cycle,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to list pending initiatives: %s", exc)
            return []

        claimed = []
        for initiative in pending:
            itype = getattr(initiative, "type", "") or getattr(initiative, "initiative_type", "")
            if itype not in self._allowed_types:
                continue

            iid = getattr(initiative, "id", "")
            try:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda id_=iid: self._store.assign(id_, self._agent_id),
                )
                if result is not None:
                    claimed.append(result)
            except Exception as exc:
                logger.debug("Failed to claim initiative %s: %s", iid, exc)

        if claimed:
            logger.info("Claimed %d initiatives for execution", len(claimed))
        return claimed

    # ------------------------------------------------------------------
    # Execute a single initiative
    # ------------------------------------------------------------------

    async def _execute_one(self, initiative: Any) -> None:
        iid = getattr(initiative, "id", "")
        itype = getattr(initiative, "type", "") or getattr(initiative, "initiative_type", "")
        start = time.monotonic()

        logger.info("Executing initiative %s (%s)", iid, itype)

        try:
            prompt = _build_initiative_prompt(initiative)
            result = await self._reasoning.run_turn(
                session_id=f"executor-{iid}",
                messages=[{"role": "user", "content": prompt}],
                system_prompt=_SYSTEM_PROMPT,
                model_override=self._model_tier,
            )

            elapsed = time.monotonic() - start
            usage = result.usage or {}
            self._stats["total_tokens"] += usage.get("total_tokens", 0)

            if result.status == "completed" and result.message:
                response_text = result.message.get("content", "")
                await self._complete_initiative(iid, response_text)
                self._stats["initiatives_completed"] += 1
                logger.info(
                    "Initiative %s completed in %.1fs (%d tokens): %s",
                    iid, elapsed,
                    usage.get("total_tokens", 0),
                    response_text[:120],
                )
            elif result.status == "error":
                await self._fail_initiative(iid, result.error or "reasoning error", retry=True)
                self._stats["initiatives_failed"] += 1
                logger.warning("Initiative %s failed: %s", iid, result.error)
            else:
                await self._fail_initiative(
                    iid, f"unexpected status: {result.status}", retry=True
                )
                self._stats["initiatives_failed"] += 1

        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("Initiative %s execution error (%.1fs): %s", iid, elapsed, exc)
            await self._fail_initiative(iid, str(exc), retry=True)
            self._stats["initiatives_failed"] += 1

        self._stats["initiatives_processed"] += 1

    # ------------------------------------------------------------------
    # Store callbacks
    # ------------------------------------------------------------------

    async def _complete_initiative(self, initiative_id: str, result: str) -> None:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._store.complete(
                    initiative_id,
                    agent_id=self._agent_id,
                    result=result,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to mark initiative %s complete: %s", initiative_id, exc)

    async def _fail_initiative(
        self, initiative_id: str, reason: str, retry: bool = False,
    ) -> None:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._store.fail(
                    initiative_id,
                    agent_id=self._agent_id,
                    reason=reason,
                    retry=retry,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to mark initiative %s failed: %s", initiative_id, exc)


# ---------------------------------------------------------------------------
# Factory + wiring
# ---------------------------------------------------------------------------

def create_from_env(
    initiative_store: Any = None,
    reasoning_loop: Any = None,
    tool_executor: Any = None,
) -> Optional[InitiativeExecutorService]:
    """Create an InitiativeExecutorService from environment variables.

    Returns None if COLONY_EXECUTOR_ENABLED is not "true" or required
    dependencies are missing.
    """
    if os.environ.get("COLONY_EXECUTOR_ENABLED", "false").lower() != "true":
        logger.info(
            "Initiative executor disabled (COLONY_EXECUTOR_ENABLED != true). "
            "Set COLONY_EXECUTOR_ENABLED=true to enable autonomous initiative processing."
        )
        return None

    if reasoning_loop is None:
        logger.warning(
            "Initiative executor: no ReasoningLoop available (LLM not configured). "
            "Cannot execute initiatives without an LLM."
        )
        return None

    if initiative_store is None:
        logger.warning("Initiative executor: no InitiativeStore available.")
        return None

    types_env = os.environ.get("COLONY_EXECUTOR_TYPES", "")
    allowed_types = (
        {t.strip() for t in types_env.split(",") if t.strip()}
        if types_env
        else None
    )

    return InitiativeExecutorService(
        initiative_store=initiative_store,
        reasoning_loop=reasoning_loop,
        tool_executor=tool_executor,
        cycle_secs=float(os.environ.get("COLONY_EXECUTOR_CYCLE_SECS", "30")),
        max_per_cycle=int(os.environ.get("COLONY_EXECUTOR_MAX_PER_CYCLE", "5")),
        model_tier=os.environ.get("COLONY_EXECUTOR_MODEL_TIER", "small"),
        allowed_types=allowed_types,
        agent_id=os.environ.get("COLONY_EXECUTOR_AGENT_ID", "colony-executor"),
    )
