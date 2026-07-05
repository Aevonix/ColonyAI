"""EscalationMiner: detect + bank escalation events from the live turn stream.

Two detectors run on every synced turn:

1. CONSULTATION: the agent shelled out to a build/coding agent during the
   turn (a terminal-class tool ran AND the turn text matches
   COLONY_ESCALATION_CONSULT_REGEX). The situation was hard enough that the
   agent escalated to a stronger coding agent: exactly the material worth
   distilling into a skill and worth banking as a golden case.
2. PROVIDER ESCALATION: the turn's model (optional per-turn metadata from the
   host) matches COLONY_ESCALATION_HEAVY_RE: a heavy-model or cloud-failover
   turn.

Each record banks: task/prompt context, the prior local attempt in the same
session (when present), the escalated answer, channel, model, ts. Outcome is
tracked lightweight: the NEXT turn in the same session marks the record
followed_up with the user's reaction excerpt.

Modes (COLONY_ESCALATION_MINING): shadow banks + journals only; live also
feeds records into skills-memory distillation (domain "escalation"), which
itself honors COLONY_SKILLS_DISTILL. Everything is best-effort: a miner
failure must never affect the turn pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, List, Optional

from colony_sidecar.mining.models import (
    EscalationRecord,
    MinedTurn,
    consult_regex,
    heavy_model_regex,
    mining_mode,
    turn_cap,
)
from colony_sidecar.mining.store import MiningStore

logger = logging.getLogger(__name__)

_TERMINAL_TOOLS = {"terminal", "shell", "bash", "execute_code", "shell_exec"}


def _session_tool_text(session_id: str, max_lines: int = 400) -> str:
    """Recent host tool-activity summaries for a session (optional source).

    Consultations often show only in the tool COMMANDS (e.g. `claude -p ...`
    run via the terminal tool), not in the reply text. When the host exposes
    its tool-activity stream (COLONY_TOOL_ACTIVITY_FILE, a JSONL of
    {ts, session, tool, summary}), include those summaries in the detection
    text. Best-effort: any failure returns "".
    """
    import json as _json
    import os as _os

    path = _os.environ.get("COLONY_TOOL_ACTIVITY_FILE", "")
    if not path or not session_id:
        return ""
    try:
        with open(_os.path.expanduser(path), "rb") as f:
            try:
                f.seek(-200_000, 2)
            except OSError:
                f.seek(0)
            lines = f.read().decode("utf-8", "replace").splitlines()[-max_lines:]
        out = []
        for ln in lines:
            try:
                r = _json.loads(ln)
            except Exception:
                continue
            if r.get("session") == session_id and r.get("summary"):
                out.append(str(r["summary"]))
        return "\n".join(out[-50:])
    except Exception:
        return ""


class EscalationMiner:
    def __init__(
        self,
        store: MiningStore,
        *,
        skill_store: Any = None,
        router_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._store = store
        self._skill_store = skill_store
        self._router_getter = router_getter

    # -- turn intake -----------------------------------------------------------

    def observe_turn(
        self,
        *,
        session_id: str,
        contact_id: str,
        channel_id: str,
        user_text: str,
        assistant_text: str,
        summary: str = "",
        tools_used: Optional[List[str]] = None,
        model: str = "",
    ) -> Optional[EscalationRecord]:
        """Bank the verbatim turn, close out a pending escalation's outcome,
        and detect a new escalation. Returns the new record, if any."""
        mode = mining_mode()
        if mode == "off":
            return None
        try:
            cap = turn_cap()
            tools = list(tools_used or [])
            prior = self._store.last_turn_in_session(session_id)

            # Outcome follow-up: the next turn's user message is the reaction
            # to a previously banked escalation in this session.
            try:
                open_esc = self._store.latest_open_escalation(session_id)
                if open_esc is not None and user_text:
                    open_esc.outcome = "followed_up"
                    open_esc.outcome_note = user_text[:300]
                    self._store.update_escalation(open_esc)
            except Exception:
                logger.debug("escalation outcome follow-up failed", exc_info=True)

            turn = MinedTurn(
                session_id=session_id,
                contact_id=contact_id,
                channel_id=channel_id,
                user_text=(user_text or "")[:cap],
                assistant_text=(assistant_text or "")[:cap],
                summary=(summary or "")[:1000],
                tools_used=tools[:20],
                model=model or "",
            )
            self._store.add_turn(turn)

            record = self._detect(turn, prior)
            if record is None:
                return None
            self._store.add_escalation(record)
            self._journal(record, mode)
            if mode == "live":
                self._feed_distiller(record)
            return record
        except Exception:
            logger.debug("escalation miner observe_turn failed", exc_info=True)
            return None

    # -- detection --------------------------------------------------------------

    def _detect(
        self, turn: MinedTurn, prior: Optional[MinedTurn]
    ) -> Optional[EscalationRecord]:
        text = f"{turn.user_text}\n{turn.assistant_text}\n{turn.summary}"
        tool_text = _session_tool_text(turn.session_id)
        if tool_text:
            text = f"{text}\n{tool_text}"

        # (a) build-agent consultation
        ran_terminal = any(t in _TERMINAL_TOOLS for t in turn.tools_used)
        pattern = consult_regex()
        if ran_terminal and pattern:
            try:
                m = re.search(pattern, text, re.IGNORECASE)
            except re.error:
                m = None
                logger.warning("invalid COLONY_ESCALATION_CONSULT_REGEX; detector idle")
            if m:
                return EscalationRecord(
                    kind="consultation",
                    session_id=turn.session_id,
                    contact_id=turn.contact_id,
                    channel_id=turn.channel_id,
                    task_context=turn.user_text[:2000],
                    local_attempt=(prior.assistant_text[:2000] if prior else ""),
                    escalated_answer=turn.assistant_text[:4000],
                    model=turn.model,
                    matched=m.group(0)[:120],
                )

        # (b) heavy-model / cloud-failover turn
        heavy = heavy_model_regex()
        if heavy and turn.model:
            try:
                hm = re.search(heavy, turn.model, re.IGNORECASE)
            except re.error:
                hm = None
                logger.warning("invalid COLONY_ESCALATION_HEAVY_RE; detector idle")
            if hm:
                return EscalationRecord(
                    kind="provider_escalation",
                    session_id=turn.session_id,
                    contact_id=turn.contact_id,
                    channel_id=turn.channel_id,
                    task_context=turn.user_text[:2000],
                    local_attempt=(prior.assistant_text[:2000] if prior else ""),
                    escalated_answer=turn.assistant_text[:4000],
                    model=turn.model,
                    matched=hm.group(0)[:120],
                )
        return None

    # -- consumers ---------------------------------------------------------------

    def _journal(self, record: EscalationRecord, mode: str) -> None:
        try:
            from colony_sidecar.events.journal import append_event

            append_event(
                "mining.escalation",
                {
                    "escalation_id": record.id,
                    "kind": record.kind,
                    "channel_id": record.channel_id,
                    "session_id": record.session_id,
                    "contact_id": record.contact_id,
                    "model": record.model,
                    "matched": record.matched,
                    "mode": mode,
                    "summary": record.task_context[:200],
                },
            )
        except Exception:
            logger.debug("mining.escalation journal failed", exc_info=True)

    def _feed_distiller(self, record: EscalationRecord) -> None:
        """Live mode: escalations are high-value distillation inputs."""
        try:
            from colony_sidecar.skills_memory import (
                distill_from_completion,
                skills_distill_mode,
            )

            if skills_distill_mode() == "off" or self._skill_store is None:
                return
            router = self._router_getter() if self._router_getter else None
            if router is None:
                return
            result_text = record.escalated_answer
            if record.local_attempt:
                result_text = (
                    f"Local attempt (before escalating):\n{record.local_attempt}\n\n"
                    f"Escalated resolution:\n{record.escalated_answer}"
                )

            async def _run() -> None:
                try:
                    skill = await distill_from_completion(
                        router,
                        self._skill_store,
                        domain="escalation",
                        task_text=record.task_context,
                        result_text=result_text,
                        source_ref=record.id,
                    )
                    if skill is not None:
                        record.distilled = 1
                        self._store.update_escalation(record)
                except Exception:
                    logger.debug("escalation distillation failed", exc_info=True)

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_run())
            except RuntimeError:
                asyncio.run(_run())
        except Exception:
            logger.debug("escalation distiller feed failed", exc_info=True)

    # -- reads -------------------------------------------------------------------

    def recent(self, *, kind: Optional[str] = None, limit: int = 50) -> List[dict]:
        return [e.to_row() for e in self._store.list_escalations(kind=kind, limit=limit)]

    def stats(self) -> dict:
        s = self._store.escalation_stats()
        s["mode"] = mining_mode()
        return s
