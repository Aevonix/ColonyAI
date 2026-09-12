"""Rejection feedback loop — regenerate once when the ResponseGuard blocks.

Rebuilt ResponseGuard-native: the original loop was an orphan of the retired
7-layer ResponseGate and depended on subsystems that no longer exist
(GatePayload sessions, MetaLearner.record_gate_rejection, an escalation
manager). The dataclasses are kept as the stable contract; the loop now runs
against ``ResponseGuard.evaluate`` and a caller-supplied ``regenerate`` hook,
and rejections persist in a small ``RejectionStore`` instead of MetaLearner.

Error contract (mirrors the guard's own):
  * A guard BLOCK is never overridden by a loop-internal error — if anything
    inside the loop raises after a block, the result stays blocked (fail
    CLOSED on the block side).
  * If the loop errors while the latest guard verdict is an allow, the allow
    stands (fail OPEN on the allow side) — a loop fault must never silence a
    reply the guard already cleared.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RejectionNotice:
    """Structured rejection fed back to the LLM for regeneration."""
    notice_id: str
    turn_id: str
    session_id: str
    contact_display_name: str
    block_reason_type: str
    blocking_layer: int
    redacted_excerpt: Optional[str]
    trust_tier: str
    retry_number: int
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def to_llm_prompt_fragment(self) -> str:
        excerpt_line = ""
        if self.redacted_excerpt:
            excerpt_line = f'\nFlagged content: "{self.redacted_excerpt}"'
        return (
            f"Your response to {self.contact_display_name} was blocked by the "
            f"response gate.\n\n"
            f"Reason: {self.block_reason_type}{excerpt_line}\n\n"
            f"Please generate a new response that does not contain this content. "
            f"The recipient's trust tier is {self.trust_tier}. Adjust accordingly."
        )


@dataclass
class GateRejectionEvent:
    """Learning signal for a gate rejection (persisted in RejectionStore)."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    contact_id: str = ""
    trust_tier: str = ""
    blocking_layer: int = 0
    block_reason: str = ""
    retry_number: int = 0
    eventually_succeeded: bool = False
    turn_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class FeedbackLoopResult:
    passed: bool
    payload: Optional[object]     # the (possibly regenerated) response text
    attempts: int


class RejectionStore:
    """SQLite persistence for gate rejections (replaces the MetaLearner
    dependency of the retired loop). Also the counting surface for the
    enforce circuit breaker."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path or ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS gate_rejections (
                    event_id TEXT PRIMARY KEY, session_id TEXT,
                    contact_id TEXT, trust_tier TEXT, blocking_layer INTEGER,
                    block_reason TEXT, retry_number INTEGER,
                    eventually_succeeded INTEGER, turn_id TEXT, ts TEXT
                )""")
            self._conn.commit()

    def record(self, event: GateRejectionEvent) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO gate_rejections
                   (event_id, session_id, contact_id, trust_tier,
                    blocking_layer, block_reason, retry_number,
                    eventually_succeeded, turn_id, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET
                    eventually_succeeded=excluded.eventually_succeeded,
                    retry_number=excluded.retry_number""",
                (event.event_id, event.session_id, event.contact_id,
                 event.trust_tier, event.blocking_layer, event.block_reason,
                 event.retry_number, 1 if event.eventually_succeeded else 0,
                 event.turn_id, event.timestamp.isoformat()))
            self._conn.commit()

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM gate_rejections ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def count_since(self, hours: float = 24.0) -> int:
        cutoff = (datetime.now(tz=timezone.utc)
                  - timedelta(hours=hours)).isoformat()
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM gate_rejections WHERE ts >= ?",
                (cutoff,)).fetchone()
        return int(r[0]) if r else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# regenerate(prompt_fragment, blocked_text) -> new text (or None to give up)
RegenerateFn = Callable[[str, str], Awaitable[Optional[str]]]


class RejectionFeedbackLoop:
    """One regenerate-and-re-check cycle after a ResponseGuard block.

    Usage::

        loop = RejectionFeedbackLoop(guard, store=store, regenerate=regen)
        result = await loop.run(text, initial_result=guard_result,
                                target_contact_id=..., ...)

    ``regenerate`` is a deployment-supplied async hook; without one the loop
    cannot revise, so a block simply stands.
    """

    def __init__(self, guard: Any, store: Optional[RejectionStore] = None,
                 regenerate: Optional[RegenerateFn] = None,
                 max_retries: int = 1) -> None:
        self._guard = guard
        self._store = store
        self._regenerate = regenerate
        self._max_retries = max(0, int(max_retries))

    def _record(self, event: Optional[GateRejectionEvent]) -> None:
        if event is None or self._store is None:
            return
        try:
            self._store.record(event)
        except Exception:
            logger.debug("rejection store record failed", exc_info=True)

    @staticmethod
    def _block_reason(result: Any) -> tuple[str, Optional[str]]:
        for f in getattr(result, "findings", None) or []:
            if getattr(f, "severity", "") == "block":
                return (getattr(f, "check", "blocked"),
                        getattr(f, "excerpt", None))
        return ("blocked", None)

    async def run(self, response_text: str, *,
                  initial_result: Any = None,
                  contact_display_name: str = "the recipient",
                  **eval_kwargs: Any) -> FeedbackLoopResult:
        """Evaluate (or take ``initial_result``), regenerate on block, re-check.

        ``eval_kwargs`` are forwarded verbatim to ``guard.evaluate`` so the
        re-check runs under exactly the same identity/context as the original.
        """
        text = response_text
        result = initial_result
        attempts = 0
        event: Optional[GateRejectionEvent] = None
        # Tracks the LAST KNOWN guard verdict so the exception handler can
        # honor the error contract (closed on block, open on allow).
        blocked = bool(getattr(result, "blocked", True))
        try:
            if result is None:
                result = await self._guard.evaluate(
                    response_text=text, **eval_kwargs)
                attempts = 1
                blocked = result.blocked

            for retry in range(self._max_retries + 1):
                if not result.blocked:
                    if event is not None:
                        event.eventually_succeeded = True
                        self._record(event)
                    return FeedbackLoopResult(passed=True, payload=text,
                                              attempts=max(attempts, 1))

                reason, excerpt = self._block_reason(result)
                event = GateRejectionEvent(
                    session_id=str(eval_kwargs.get("session_id", "")),
                    contact_id=str(eval_kwargs.get("target_contact_id", "")),
                    trust_tier=str(eval_kwargs.get("trust_tier", "")),
                    blocking_layer=0,
                    block_reason=reason,
                    retry_number=retry,
                    turn_id=str(eval_kwargs.get("turn_id", "")),
                )
                self._record(event)

                if retry >= self._max_retries or self._regenerate is None:
                    break

                notice = RejectionNotice(
                    notice_id=str(uuid.uuid4()),
                    turn_id=str(eval_kwargs.get("turn_id", "")),
                    session_id=str(eval_kwargs.get("session_id", "")),
                    contact_display_name=contact_display_name,
                    block_reason_type=reason,
                    blocking_layer=0,
                    redacted_excerpt=excerpt,
                    trust_tier=str(eval_kwargs.get("trust_tier", "")),
                    retry_number=retry + 1,
                )
                new_text = await self._regenerate(
                    notice.to_llm_prompt_fragment(), text)
                if not new_text or not str(new_text).strip():
                    break
                text = str(new_text).strip()
                result = await self._guard.evaluate(
                    response_text=text, **eval_kwargs)
                attempts += 1
                blocked = result.blocked

            return FeedbackLoopResult(passed=False, payload=None,
                                      attempts=max(attempts, 1))
        except Exception:
            logger.warning("RejectionFeedbackLoop internal error", exc_info=True)
            if not blocked:
                # allow verdict stands: fail open on the allow side
                return FeedbackLoopResult(passed=True, payload=text,
                                          attempts=max(attempts, 1))
            # a guard BLOCK is never overridden by a loop error
            return FeedbackLoopResult(passed=False, payload=None,
                                      attempts=max(attempts, 1))
