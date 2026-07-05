"""Mining models: escalation records + verbatim turn capture.

Concept provenance: inspired by external scaffolding work (drowzeys
Keys-Setup) on turning cloud/escalation corrections into local supervision
and serving logs into training data. Concepts only; no code adopted
(unlicensed + immature upstream).

Env flags
---------
COLONY_ESCALATION_MINING = off | shadow | live   (default shadow)
    off:    no capture, no detection.
    shadow: capture verbatim turns + bank/journal escalation records.
    live:   shadow + feed banked escalations into skills-memory distillation.
COLONY_ESCALATION_CONSULT_REGEX
    Pattern that marks a turn as a build-agent consultation (the agent
    shelled out to a coding agent). Deployment-tunable.
COLONY_ESCALATION_HEAVY_RE
    Pattern over the per-turn model name that marks a provider escalation
    (heavy-model or cloud-failover turn). Empty (default) disables the
    provider detector; deployments list their heavy/cloud models.
COLONY_MINING_TURN_CAP
    Per-side verbatim capture cap in chars (default 8000).
COLONY_CORPUS_EXPORT_ENABLED
    Gate for the corpus exporter endpoint (default true; exports only run
    on explicit request and never leave the state dir).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ESCALATION_KINDS = ("consultation", "provider_escalation")


def mining_mode() -> str:
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_ESCALATION_MINING",
                   ("off", "shadow", "live"), "shadow")


def corpus_export_enabled() -> bool:
    return os.environ.get(
        "COLONY_CORPUS_EXPORT_ENABLED", "true"
    ).strip().lower() not in ("false", "0", "no", "off")


def consult_regex() -> str:
    return os.environ.get(
        "COLONY_ESCALATION_CONSULT_REGEX",
        r"claude\s+-p|claude[-_ ]code|\bcode agent\b",
    )


def heavy_model_regex() -> str:
    return os.environ.get("COLONY_ESCALATION_HEAVY_RE", "")


def turn_cap() -> int:
    try:
        return max(500, int(os.environ.get("COLONY_MINING_TURN_CAP", "8000")))
    except ValueError:
        return 8000


@dataclass
class MinedTurn:
    """One verbatim conversation exchange, banked for corpus export."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    contact_id: str = ""
    channel_id: str = ""
    user_text: str = ""
    assistant_text: str = ""
    summary: str = ""
    tools_used: List[str] = field(default_factory=list)
    model: str = ""
    ts: float = field(default_factory=time.time)

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "contact_id": self.contact_id,
            "channel_id": self.channel_id,
            "user_text": self.user_text,
            "assistant_text": self.assistant_text,
            "summary": self.summary,
            "tools_used": json.dumps(self.tools_used),
            "model": self.model,
            "ts": self.ts,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "MinedTurn":
        return cls(
            id=row["id"],
            session_id=row.get("session_id") or "",
            contact_id=row.get("contact_id") or "",
            channel_id=row.get("channel_id") or "",
            user_text=row.get("user_text") or "",
            assistant_text=row.get("assistant_text") or "",
            summary=row.get("summary") or "",
            tools_used=json.loads(row.get("tools_used") or "[]"),
            model=row.get("model") or "",
            ts=float(row.get("ts") or 0.0),
        )


@dataclass
class EscalationRecord:
    """A turn where the agent escalated: consulted a build agent, or the
    turn ran on a heavy/cloud model. High-value supervision material."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = "consultation"          # one of ESCALATION_KINDS
    session_id: str = ""
    contact_id: str = ""
    channel_id: str = ""
    task_context: str = ""              # what was being asked/attempted
    local_attempt: str = ""             # prior local answer in-session, if any
    escalated_answer: str = ""          # the answer produced with escalation
    model: str = ""
    matched: str = ""                   # which signature matched (audit)
    outcome: str = "unknown"            # unknown | followed_up
    outcome_note: str = ""              # next user message excerpt
    distilled: int = 0                  # fed to skills distillation (live mode)
    ts: float = field(default_factory=time.time)

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "session_id": self.session_id,
            "contact_id": self.contact_id,
            "channel_id": self.channel_id,
            "task_context": self.task_context,
            "local_attempt": self.local_attempt,
            "escalated_answer": self.escalated_answer,
            "model": self.model,
            "matched": self.matched,
            "outcome": self.outcome,
            "outcome_note": self.outcome_note,
            "distilled": self.distilled,
            "ts": self.ts,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "EscalationRecord":
        return cls(
            id=row["id"],
            kind=row.get("kind") or "consultation",
            session_id=row.get("session_id") or "",
            contact_id=row.get("contact_id") or "",
            channel_id=row.get("channel_id") or "",
            task_context=row.get("task_context") or "",
            local_attempt=row.get("local_attempt") or "",
            escalated_answer=row.get("escalated_answer") or "",
            model=row.get("model") or "",
            matched=row.get("matched") or "",
            outcome=row.get("outcome") or "unknown",
            outcome_note=row.get("outcome_note") or "",
            distilled=int(row.get("distilled") or 0),
            ts=float(row.get("ts") or 0.0),
        )
