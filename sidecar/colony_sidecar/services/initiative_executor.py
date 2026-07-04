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
  COLONY_EXECUTOR_MAX_TOOL_ITERS    max tool-continuation rounds per initiative
                                    when the reasoning loop returns needs_tool
                                    (default 8)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Every initiative type this internal-tool backend is capable of processing.
_EXECUTABLE_TYPES = {
    "follow_up", "relationship", "introduction", "health", "scheduling",
    "coding", "subsystem_health", "data_quality", "operational",
    "capability_gap", "knowledge_acquisition", "behavioral_correction",
    "agent_action", "commitment", "calendar", "research", "task",
    "project", "system",
}


def _default_allowed_types() -> set[str]:
    """Internal-execution types = everything except reach-out types.

    Reach-out (person-facing) initiatives are delivered through the autonomy
    loop's guarded delivery path (push_initiative -> Hermes), where the host
    agent composes and sends them under the delivery rate limiter. Executing
    them here with the internal-only toolset would just swallow them into
    bookkeeping and starve delivery, so they are excluded by default. A
    deployment can still opt them back in via COLONY_EXECUTOR_TYPES.
    """
    try:
        from colony_sidecar.delivery.classification import reachout_types
        return set(_EXECUTABLE_TYPES) - set(reachout_types())
    except Exception:
        return set(_EXECUTABLE_TYPES)


_DEFAULT_TYPES = _default_allowed_types()

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


_TIMEOUT_MARKERS = (
    "timeout", "timed out", "deadline", "read operation timed out",
    "readtimeout", "timeouterror", "etimedout",
)

_TERM_STOP = frozenset({
    "the", "a", "an", "to", "of", "on", "in", "for", "with", "and", "or",
    "follow", "up", "check", "review", "investigate", "diagnose", "verify",
    "initiative", "task", "this", "that", "about", "into", "your", "my",
})


def _is_timeout_error(err: Optional[str]) -> bool:
    if not err:
        return False
    low = str(err).lower()
    return any(m in low for m in _TIMEOUT_MARKERS)


def _subject_terms(text: Optional[str]) -> set:
    """Significant tokens of an initiative subject, for repeat-work matching."""
    import re as _re
    toks = _re.findall(r"[a-z0-9][a-z0-9_\-]{2,}", (text or "").lower())
    return {t for t in toks if t not in _TERM_STOP}


def _assistant_tool_message(
    content: str, tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build an OpenAI-shaped assistant message carrying tool calls.

    Mirrors ``ReasoningLoop._build_assistant_message`` so the continuation
    messages we feed back into ``run_turn`` have the exact shape the model
    provider expects (tool_call ids on the assistant turn must match the
    following ``role: "tool"`` result turns).
    """
    msg: dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": (
                        tc["arguments"]
                        if isinstance(tc.get("arguments"), str)
                        else json.dumps(tc.get("arguments", {}))
                    ),
                },
            }
            for tc in tool_calls
        ]
    return msg


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
        max_tool_iterations: int = 8,
        directive_manager: Any = None,
        skill_store: Any = None,
        self_model: Any = None,
    ):
        self._store = initiative_store
        self._reasoning = reasoning_loop
        self._tools = tool_executor
        # Boundary enforcement: consulted before executing an initiative so the
        # executor never acts on a subject the owner told it to leave alone.
        self._directives = directive_manager
        # Compounding learning (item 3) + self-model/trust (item 4).
        self._skills = skill_store
        self._self_model = self_model
        self._cycle_secs = cycle_secs
        self._max_per_cycle = max_per_cycle
        self._model_tier = model_tier
        self._allowed_types = allowed_types or _DEFAULT_TYPES
        self._agent_id = agent_id
        # Upper bound on continuation rounds when the reasoning loop returns
        # ``needs_tool`` (its internal per-call budget was exhausted mid-action).
        # Each round executes the pending tool calls and re-enters run_turn.
        self._max_tool_iterations = max(1, int(max_tool_iterations))

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

        # Boundary gate: refuse to act on anything the owner set as off-limits.
        if self._directives is not None:
            try:
                from colony_sidecar.directives import Action
                desc = getattr(initiative, "description", "") or ""
                rationale = getattr(initiative, "rationale", "") or ""
                entity_id = getattr(initiative, "entity_id", "") or ""
                verdict = self._directives.check(Action(
                    kind="execute",
                    text=f"{desc} {rationale}",
                    target=entity_id,
                    entity_id=entity_id,
                ))
                if not verdict.allowed:
                    logger.warning(
                        "Initiative %s (%s) REFUSED by boundary: %s",
                        iid, itype, verdict.reason,
                    )
                    # Terminal: a boundary is not a transient failure, so do not
                    # retry (it would just be refused again until lifted).
                    await self._fail_initiative(
                        iid, f"refused: {verdict.reason}", retry=False,
                    )
                    self._stats["initiatives_failed"] += 1
                    self._stats["initiatives_processed"] += 1
                    return
            except Exception:
                logger.debug("boundary pre-check failed (allowing)", exc_info=True)

        # Repeat-work suppression: if a recent completion already covers this
        # subject, close it with a pointer instead of re-running the reasoning.
        try:
            dup = await self._find_recent_completion(initiative)
        except Exception:
            dup = None
        if dup is not None:
            dup_id = getattr(dup, "id", "?")
            dup_res = (getattr(dup, "result", "") or "")[:200]
            logger.info("Initiative %s (%s) already covered by %s — skipping re-run",
                        iid, itype, dup_id)
            await self._complete_initiative(
                iid, f"Already addressed by recent completion {dup_id}. {dup_res}")
            self._stats["initiatives_completed"] += 1
            self._stats["initiatives_processed"] += 1
            return

        try:
            prompt = _build_initiative_prompt(initiative)
            session_id = f"executor-{iid}"
            is_research = itype in ("research", "knowledge_acquisition")

            # Make the reasoner aware of standing boundaries (soft layer atop the
            # hard gate above and the per-tool gate below).
            system_prompt = _SYSTEM_PROMPT
            if self._directives is not None:
                try:
                    brief = self._directives.context_brief()
                    if brief:
                        system_prompt = (
                            _SYSTEM_PROMPT
                            + "\n\n## Standing boundaries from the owner "
                            + "(obey without exception)\n" + brief
                        )
                except Exception:
                    pass

            # Relevant past procedures (item 3) + self-assessment (item 4):
            # purely additive prompt context; informs, never acts.
            if self._skills is not None:
                try:
                    from colony_sidecar.skills_memory import (
                        format_block, relevant_skills, skills_enabled,
                    )
                    if skills_enabled():
                        block = format_block(
                            relevant_skills(self._skills,
                                            f"{itype} {getattr(initiative, 'description', '')}",
                                            k=3, domain=itype),
                            strategy_note=self._skills.get_note(itype))
                        if block:
                            system_prompt += "\n\n" + block
                except Exception:
                    logger.debug("skills block failed", exc_info=True)
            if self._self_model is not None:
                try:
                    sm_brief = self._self_model.brief()
                    if sm_brief:
                        system_prompt += "\n\n## Self-assessment\n" + sm_brief
                except Exception:
                    pass

            # Conversation accumulated across continuation rounds. The reasoning
            # loop runs its own internal tool loop, but returns ``needs_tool``
            # when its per-call iteration budget is exhausted while an action is
            # still pending. We then execute the pending tool calls ourselves and
            # re-enter run_turn with the results appended, extending the budget.
            working: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            total_tokens = 0
            iterations = 0
            done = False

            while iterations < self._max_tool_iterations:
                iterations += 1
                result = await self._run_turn_resilient(
                    session_id=session_id,
                    messages=working,
                    system_prompt=system_prompt,
                    is_research=is_research,
                )

                usage = result.usage or {}
                total_tokens += usage.get("total_tokens", 0)

                if result.status == "completed":
                    response_text = ""
                    if result.message:
                        response_text = result.message.get("content", "") or ""
                    await self._complete_initiative(iid, response_text)
                    self._stats["initiatives_completed"] += 1
                    self._stats["total_tokens"] += total_tokens
                    elapsed = time.monotonic() - start
                    logger.info(
                        "Initiative %s completed in %.1fs (%d tokens, %d round(s)): %s",
                        iid, elapsed, total_tokens, iterations,
                        response_text[:120],
                    )
                    self._record_outcome(itype, "success", elapsed)
                    await self._maybe_distill(initiative, itype, response_text)
                    done = True
                    break

                if result.status == "error":
                    await self._fail_initiative(
                        iid, result.error or "reasoning error", retry=True
                    )
                    self._stats["initiatives_failed"] += 1
                    self._stats["total_tokens"] += total_tokens
                    logger.warning("Initiative %s failed: %s", iid, result.error)
                    self._record_outcome(
                        itype,
                        "timeout" if _is_timeout_error(result.error) else "failure",
                        time.monotonic() - start)
                    self._note_failure(initiative, itype, result.error or "")
                    done = True
                    break

                if result.status == "needs_tool":
                    pending = list(result.tool_calls or [])
                    if not pending:
                        # needs_tool with nothing to run: no forward progress
                        # possible, so stop rather than spin.
                        await self._fail_initiative(
                            iid,
                            "needs_tool returned no tool calls",
                            retry=True,
                        )
                        self._stats["initiatives_failed"] += 1
                        self._stats["total_tokens"] += total_tokens
                        logger.warning(
                            "Initiative %s: needs_tool with empty tool_calls", iid
                        )
                        done = True
                        break

                    if self._tools is None:
                        await self._fail_initiative(
                            iid,
                            "needs_tool but no tool executor is wired",
                            retry=True,
                        )
                        self._stats["initiatives_failed"] += 1
                        self._stats["total_tokens"] += total_tokens
                        logger.warning(
                            "Initiative %s needs a tool but no ToolExecutor is "
                            "injected; cannot proceed", iid,
                        )
                        done = True
                        break

                    # Per-tool boundary gate (defense in depth): a boundary-
                    # violating tool call the model produced mid-reasoning is
                    # dropped and reported back so it adapts, rather than run.
                    if self._directives is not None:
                        pending = self._filter_tool_calls_by_boundary(
                            pending, working,
                        )
                        if not pending:
                            # everything was blocked; give the model the refusal
                            # and let it continue or wrap up.
                            working.append({
                                "role": "user",
                                "content": "Those actions violate a standing boundary "
                                           "from the owner and were refused. Do not "
                                           "attempt them; summarise and stop.",
                            })
                            continue

                    # Execute the pending tool calls through the SAME executor the
                    # reasoning loop uses internally (identical gating/handlers),
                    # then feed the results back for another round.
                    tool_results = await self._tools.execute_batch(
                        pending, session_id=session_id,
                    )
                    self._stats["total_tool_calls"] += len(pending)

                    working.append(_assistant_tool_message(
                        result.message.get("content", "") if result.message else "",
                        pending,
                    ))
                    for tr in tool_results:
                        working.append({
                            "role": "tool",
                            "tool_call_id": tr.get("tool_call_id", ""),
                            "content": tr.get("content", ""),
                        })
                    logger.info(
                        "Initiative %s round %d: executed %d tool call(s), continuing",
                        iid, iterations, len(pending),
                    )
                    continue

                # Any status outside the known domain must never be dropped
                # silently again.
                await self._fail_initiative(
                    iid, f"unexpected status: {result.status}", retry=True
                )
                self._stats["initiatives_failed"] += 1
                self._stats["total_tokens"] += total_tokens
                logger.warning(
                    "Initiative %s: unexpected reasoning status %r", iid, result.status
                )
                done = True
                break

            if not done:
                # Hit the continuation cap without completing.
                await self._fail_initiative(
                    iid,
                    f"tool-loop cap reached after {self._max_tool_iterations} rounds",
                    retry=True,
                )
                self._stats["initiatives_failed"] += 1
                self._stats["total_tokens"] += total_tokens
                self._record_outcome(itype, "failure",
                                     time.monotonic() - start)
                logger.warning(
                    "Initiative %s hit tool-loop cap (%d rounds) without completing",
                    iid, self._max_tool_iterations,
                )

        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("Initiative %s execution error (%.1fs): %s", iid, elapsed, exc)
            await self._fail_initiative(iid, str(exc), retry=True)
            self._stats["initiatives_failed"] += 1
            self._record_outcome(
                itype, "timeout" if _is_timeout_error(str(exc)) else "failure",
                elapsed)

        self._stats["initiatives_processed"] += 1

    # ------------------------------------------------------------------
    # Self-model + skills-memory hooks (items 3 + 4)
    # ------------------------------------------------------------------

    def _record_outcome(self, itype: str, outcome: str,
                        latency: Optional[float] = None) -> None:
        if self._self_model is None:
            return
        try:
            self._self_model.record(itype, outcome, latency_secs=latency)
        except Exception:
            pass

    def _note_failure(self, initiative: Any, itype: str, error: str) -> None:
        """Failure post-mortem: keep a short per-domain strategy note."""
        if self._skills is None:
            return
        try:
            desc = (getattr(initiative, "description", "") or "")[:80]
            self._skills.record_failure_note(
                itype, f"'{desc}' failed: {(error or 'unknown')[:120]}")
        except Exception:
            pass

    async def _maybe_distill(self, initiative: Any, itype: str,
                             response_text: str) -> None:
        """Distill a reusable procedure from a qualifying completion."""
        if self._skills is None:
            return
        try:
            from colony_sidecar.skills_memory import (
                distill_from_completion, should_distill, skills_distill_mode,
            )
            if skills_distill_mode() == "off":
                return
            attempts = int(getattr(initiative, "attempt_count", 0) or 0)
            if not should_distill(max(0, attempts - 1), response_text,
                                  self._skills):
                return
            router = getattr(self._reasoning, "_model", None)
            await distill_from_completion(
                router, self._skills, domain=itype,
                task_text=getattr(initiative, "description", "") or "",
                result_text=response_text,
                source_ref=getattr(initiative, "id", "") or "")
        except Exception:
            logger.debug("skill distillation failed", exc_info=True)

    def _filter_tool_calls_by_boundary(
        self, pending: list, working: list,
    ) -> list:
        """Drop tool calls that violate a standing boundary. Returns survivors."""
        if self._directives is None:
            return pending
        try:
            from colony_sidecar.directives import Action
        except Exception:
            return pending
        survivors = []
        for tc in pending:
            name = tc.get("name", "")
            args = tc.get("arguments", {}) if isinstance(tc.get("arguments"), dict) else {}
            try:
                verdict = self._directives.check(Action(
                    kind="execute_tool", tool_name=name, args=args,
                    text=name, high_risk=True,
                ))
            except Exception:
                verdict = None
            if verdict is not None and not verdict.allowed:
                logger.warning(
                    "Tool call %s REFUSED by boundary: %s", name, verdict.reason,
                )
                continue
            survivors.append(tc)
        return survivors

    # ------------------------------------------------------------------
    # Store callbacks
    # ------------------------------------------------------------------

    async def _run_turn_resilient(self, *, session_id, messages, system_prompt, is_research):
        """run_turn with adaptive retry+backoff on M3 timeouts (resilience -- 5).

        A transient timeout is absorbed by retrying in-call (no store attempt is
        burned). Research-type work gets more retries. Only a persistent timeout
        falls through to the normal error handling.
        """
        max_retries = 3 if is_research else 2
        backoff = 2.0
        result = None
        for attempt in range(max_retries + 1):
            result = await self._reasoning.run_turn(
                session_id=session_id, messages=messages,
                system_prompt=system_prompt, model_override=self._model_tier,
            )
            if result.status != "error" or not _is_timeout_error(result.error):
                return result
            if attempt < max_retries:
                logger.info("run_turn timeout (attempt %d/%d), backing off %.0fs",
                            attempt + 1, max_retries + 1, backoff)
                try:
                    await asyncio.sleep(backoff)
                except Exception:
                    pass
                backoff *= 2
        return result

    async def _find_recent_completion(self, initiative: Any) -> Any:
        """A recently-completed initiative that already covers this subject."""
        if self._store is None:
            return None
        itype = getattr(initiative, "type", "") or getattr(initiative, "initiative_type", "")
        iid = getattr(initiative, "id", "")
        entity_id = getattr(initiative, "entity_id", "") or ""
        my_terms = _subject_terms(getattr(initiative, "description", ""))
        if not itype:
            return None
        window_h = float(os.environ.get("COLONY_EXECUTOR_REPEAT_WINDOW_HOURS", "48"))
        since = datetime.now(timezone.utc) - timedelta(hours=window_h)
        try:
            loop = asyncio.get_event_loop()
            recent = await loop.run_in_executor(
                None,
                lambda: self._store.list(
                    status=["completed"], type=itype, created_after=since, limit=50),
            )
        except Exception:
            return None
        for r in recent or []:
            if getattr(r, "id", "") == iid:
                continue
            if entity_id and getattr(r, "entity_id", "") == entity_id:
                return r
            rt = _subject_terms(getattr(r, "description", ""))
            if my_terms and rt:
                overlap = len(my_terms & rt) / max(1, min(len(my_terms), len(rt)))
                if overlap >= 0.7:
                    return r
        return None

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
    directive_manager: Any = None,
    skill_store: Any = None,
    self_model: Any = None,
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
        directive_manager=directive_manager,
        skill_store=skill_store,
        self_model=self_model,
        cycle_secs=float(os.environ.get("COLONY_EXECUTOR_CYCLE_SECS", "30")),
        max_per_cycle=int(os.environ.get("COLONY_EXECUTOR_MAX_PER_CYCLE", "5")),
        model_tier=os.environ.get("COLONY_EXECUTOR_MODEL_TIER", "small"),
        allowed_types=allowed_types,
        agent_id=os.environ.get("COLONY_EXECUTOR_AGENT_ID", "colony-executor"),
        max_tool_iterations=int(os.environ.get("COLONY_EXECUTOR_MAX_TOOL_ITERS", "8")),
    )
