"""Deterministic, advisory recipient simulation for P8 social boundaries.

The simulator is deliberately less powerful than the delivery plane.  It can
project facts and arcs through a server-attested recipient scope and report
risks, but it cannot send, authorize, approve, mutate an arc, or grant a tool
capability.  ``shadow`` is observation-only.  Even ``live`` is an advisory
result for a future caller to interpret; real-time voice surfaces are always
asynchronous observation and never enter their synchronous turn path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence

from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    ViewerContextV1,
    content_digest,
    project_facts,
)


SCHEMA_VERSION = 1
SIMULATOR_MODES = frozenset({"off", "shadow", "live"})
RISK_CLASSES = frozenset({"low", "medium", "high", "critical"})
REALTIME_VOICE_SURFACES = frozenset({
    "voice",
    "phone",
    "intercom",
    "meet",
    "google_meet",
    "google-meet",
    "googlemeet",
    "gmeet",
    "pstn",
    "sip",
    "webrtc",
    "telephone",
    "telephony",
    "voice_call",
    "phone_call",
    "facetime",
})
_REALTIME_VOICE_TOKENS = frozenset({
    "voice", "phone", "intercom", "meet", "call", "pstn", "sip",
    "webrtc", "telephone", "telephony", "gmeet", "facetime",
})

MAX_DRAFT_CHARS = 12_000
MAX_DRAFT_FACT_REFS = 64
MAX_RISKS = 64
MAX_REPAIRS = 32

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")
_PRESSURE_RE = re.compile(
    r"\b(urgent|must|immediately|right\s+now|now|hurry|no\s+excuses|"
    r"do\s+it|finish\s+it)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

FAIL_BEHAVIOR_BY_RISK: Mapping[str, str] = MappingProxyType({
    "low": "observe",
    "medium": "review",
    "high": "hold",
    "critical": "hold",
})

_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ref(value: Any, *, field: str, allow_empty: bool = False) -> str:
    normalized = str(value or "").strip()
    if allow_empty and not normalized:
        return ""
    if not _REF_RE.fullmatch(normalized):
        raise ValueError(f"{field} is not a bounded opaque reference")
    return normalized


def _refs(values: Sequence[str], *, field: str,
          maximum: int) -> tuple[str, ...]:
    normalized = tuple(sorted(dict.fromkeys(
        _ref(value, field=field) for value in values
    )))
    if len(normalized) > maximum:
        raise ValueError(f"{field} exceeds bounded reference limit")
    return normalized


def _iso(value: datetime | str, *, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def recipient_simulator_mode(environ: Optional[Mapping[str, str]] = None) -> str:
    """Return an explicit safe mode; unknown and unset values are ``off``."""

    source = os.environ if environ is None else environ
    mode = str(source.get("COLONY_RECIPIENT_SIMULATOR_MODE", "off")) \
        .strip().lower()
    return mode if mode in SIMULATOR_MODES else "off"


def is_realtime_voice_surface(surface: str) -> bool:
    """Classify stable names and compound deployment aliases conservatively."""

    normalized = str(surface or "").strip().lower()
    if normalized in REALTIME_VOICE_SURFACES:
        return True
    tokens = {
        token for token in re.split(r"[_:./+\-]+", normalized) if token
    }
    return bool(tokens.intersection(_REALTIME_VOICE_TOKENS))


@dataclass(frozen=True, slots=True)
class RecipientSimulationRequestV1:
    simulation_id: str
    draft_text: str
    draft_fact_refs: tuple[str, ...]
    recipient: ViewerContextV1
    risk_class: str
    surface: str
    high_salience: bool
    created_at: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported recipient simulation version")
        simulation_id = _ref(self.simulation_id, field="simulation_id")
        if not isinstance(self.draft_text, str) or not self.draft_text.strip():
            raise ValueError("simulation draft text is required")
        if len(self.draft_text) > MAX_DRAFT_CHARS:
            raise ValueError("simulation draft exceeds bounded length")
        refs = _refs(
            self.draft_fact_refs,
            field="draft_fact_ref",
            maximum=MAX_DRAFT_FACT_REFS,
        )
        if not isinstance(self.recipient, ViewerContextV1):
            raise ValueError("recipient must be a ViewerContextV1")
        risk_class = str(self.risk_class or "").strip().lower()
        if risk_class not in RISK_CLASSES:
            raise ValueError("unknown recipient simulation risk class")
        surface = _ref(self.surface, field="surface").lower()
        created_at = _iso(self.created_at, field="created_at")
        object.__setattr__(self, "simulation_id", simulation_id)
        object.__setattr__(self, "draft_fact_refs", refs)
        object.__setattr__(self, "risk_class", risk_class)
        object.__setattr__(self, "surface", surface)
        object.__setattr__(self, "high_salience", bool(self.high_salience))
        object.__setattr__(self, "created_at", created_at)

    @property
    def audit_digest(self) -> str:
        return _digest({
            "schema_version": self.schema_version,
            "simulation_id": self.simulation_id,
            "draft_digest": content_digest(self.draft_text),
            "draft_fact_refs": self.draft_fact_refs,
            "recipient_digest": self.recipient.audit_digest,
            "risk_class": self.risk_class,
            "surface": self.surface,
            "high_salience": self.high_salience,
            "created_at": self.created_at,
        })


@dataclass(frozen=True, slots=True)
class SimulationRiskV1:
    code: str
    severity: str
    fact_ref: str = ""
    arc_ref: str = ""

    def __post_init__(self) -> None:
        code = _ref(self.code, field="risk_code")
        severity = str(self.severity or "").strip().lower()
        if severity not in _SEVERITY_RANK:
            raise ValueError("unknown simulation risk severity")
        fact_ref = _ref(
            self.fact_ref, field="risk_fact_ref", allow_empty=True)
        arc_ref = _ref(
            self.arc_ref, field="risk_arc_ref", allow_empty=True)
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "fact_ref", fact_ref)
        object.__setattr__(self, "arc_ref", arc_ref)

    def public(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
        }
        if self.fact_ref:
            value["fact_ref"] = self.fact_ref
        if self.arc_ref:
            value["arc_ref"] = self.arc_ref
        return value


@dataclass(frozen=True, slots=True)
class RepairSuggestionV1:
    code: str
    priority: str
    related_ref: str = ""

    def __post_init__(self) -> None:
        code = _ref(self.code, field="repair_code")
        priority = str(self.priority or "").strip().lower()
        if priority not in _SEVERITY_RANK:
            raise ValueError("unknown repair priority")
        related_ref = _ref(
            self.related_ref, field="repair_related_ref", allow_empty=True)
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "related_ref", related_ref)

    def public(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "priority": self.priority,
        }
        if self.related_ref:
            value["related_ref"] = self.related_ref
        return value


@dataclass(frozen=True, slots=True)
class RecipientSimulationResultV1:
    simulation_id: str
    mode: str
    evaluated: bool
    risk_class: str
    fail_behavior: str
    recommended_action: str
    would_recommend: str
    risks: tuple[SimulationRiskV1, ...]
    repairs: tuple[RepairSuggestionV1, ...]
    authorized_fact_refs: tuple[str, ...]
    active_arc_refs: tuple[str, ...]
    fact_projection_digest: str
    arc_projection_digest: str
    evaluation_path: str
    external_effect: bool
    authority_granted: bool
    synchronous_gate: bool
    request_digest: str
    audit_digest: str
    schema_version: int = SCHEMA_VERSION

    def public(self) -> dict[str, Any]:
        """Return reference-only output; draft and fact content are omitted."""

        return {
            "schema_version": self.schema_version,
            "simulation_id": self.simulation_id,
            "mode": self.mode,
            "evaluated": self.evaluated,
            "risk_class": self.risk_class,
            "fail_behavior": self.fail_behavior,
            "recommended_action": self.recommended_action,
            "would_recommend": self.would_recommend,
            "risks": [risk.public() for risk in self.risks],
            "repairs": [repair.public() for repair in self.repairs],
            "authorized_fact_refs": list(self.authorized_fact_refs),
            "active_arc_refs": list(self.active_arc_refs),
            "fact_projection_digest": self.fact_projection_digest,
            "arc_projection_digest": self.arc_projection_digest,
            "evaluation_path": self.evaluation_path,
            "external_effect": self.external_effect,
            "authority_granted": self.authority_granted,
            "synchronous_gate": self.synchronous_gate,
            "request_digest": self.request_digest,
            "audit_digest": self.audit_digest,
        }


def _risk_sort(risk: SimulationRiskV1) -> tuple[Any, ...]:
    return (
        -_SEVERITY_RANK[risk.severity],
        risk.code,
        risk.fact_ref,
        risk.arc_ref,
    )


def _repair_sort(repair: RepairSuggestionV1) -> tuple[Any, ...]:
    return (
        -_SEVERITY_RANK[repair.priority],
        repair.code,
        repair.related_ref,
    )


def _dedupe_risks(values: Sequence[SimulationRiskV1]) \
        -> tuple[SimulationRiskV1, ...]:
    unique = {
        (item.code, item.severity, item.fact_ref, item.arc_ref): item
        for item in values
    }
    return tuple(sorted(unique.values(), key=_risk_sort)[:MAX_RISKS])


def _dedupe_repairs(values: Sequence[RepairSuggestionV1]) \
        -> tuple[RepairSuggestionV1, ...]:
    unique = {
        (item.code, item.priority, item.related_ref): item
        for item in values
    }
    return tuple(sorted(unique.values(), key=_repair_sort)[:MAX_REPAIRS])


def _would_recommend(
    risks: Sequence[SimulationRiskV1],
    repairs: Sequence[RepairSuggestionV1],
) -> str:
    rank = max((_SEVERITY_RANK[risk.severity] for risk in risks), default=0)
    if rank >= _SEVERITY_RANK["high"]:
        return "hold"
    if rank >= _SEVERITY_RANK["medium"]:
        return "repair" if repairs else "review"
    if rank:
        return "observe"
    return "send"


def _topic_overlap(draft: str, topic: str) -> bool:
    draft_tokens = set(_TOKEN_RE.findall(draft.lower()))
    topic_tokens = {
        token for token in _TOKEN_RE.findall(topic.lower()) if len(token) >= 4
    }
    return bool(topic_tokens and draft_tokens.intersection(topic_tokens))


class RecipientSimulator:
    """Pure advisory evaluator over recipient-authorized projections."""

    def __init__(self, *, arc_store: Any = None) -> None:
        self._arc_store = arc_store

    def _result(
        self,
        request: RecipientSimulationRequestV1,
        *,
        mode: str,
        evaluated: bool,
        risks: Sequence[SimulationRiskV1] = (),
        repairs: Sequence[RepairSuggestionV1] = (),
        authorized_fact_refs: Sequence[str] = (),
        active_arc_refs: Sequence[str] = (),
        fact_projection_digest: str = "",
        arc_projection_digest: str = "",
        dependency_failed: bool = False,
    ) -> RecipientSimulationResultV1:
        normalized_risks = _dedupe_risks(tuple(risks))
        normalized_repairs = _dedupe_repairs(tuple(repairs))
        fail_behavior = FAIL_BEHAVIOR_BY_RISK[request.risk_class]
        would = (
            fail_behavior
            if dependency_failed
            else _would_recommend(normalized_risks, normalized_repairs)
        )
        if not evaluated:
            recommended = "no_effect"
            would = "no_effect"
            evaluation_path = "disabled"
        elif is_realtime_voice_surface(request.surface):
            recommended = "observe_async"
            evaluation_path = "async_observation"
        elif mode == "shadow":
            recommended = "observe_only"
            evaluation_path = "shadow_observation"
        else:
            recommended = would
            evaluation_path = "pre_send_advisory"

        payload = {
            "schema_version": SCHEMA_VERSION,
            "simulation_id": request.simulation_id,
            "mode": mode,
            "evaluated": evaluated,
            "risk_class": request.risk_class,
            "fail_behavior": fail_behavior,
            "recommended_action": recommended,
            "would_recommend": would,
            "risks": [item.public() for item in normalized_risks],
            "repairs": [item.public() for item in normalized_repairs],
            "authorized_fact_refs": tuple(sorted(authorized_fact_refs)),
            "active_arc_refs": tuple(sorted(active_arc_refs)),
            "fact_projection_digest": fact_projection_digest,
            "arc_projection_digest": arc_projection_digest,
            "evaluation_path": evaluation_path,
            "external_effect": False,
            "authority_granted": False,
            "synchronous_gate": False,
            "request_digest": request.audit_digest,
        }
        return RecipientSimulationResultV1(
            simulation_id=request.simulation_id,
            mode=mode,
            evaluated=evaluated,
            risk_class=request.risk_class,
            fail_behavior=fail_behavior,
            recommended_action=recommended,
            would_recommend=would,
            risks=normalized_risks,
            repairs=normalized_repairs,
            authorized_fact_refs=tuple(sorted(authorized_fact_refs)),
            active_arc_refs=tuple(sorted(active_arc_refs)),
            fact_projection_digest=fact_projection_digest,
            arc_projection_digest=arc_projection_digest,
            evaluation_path=evaluation_path,
            external_effect=False,
            authority_granted=False,
            synchronous_gate=False,
            request_digest=request.audit_digest,
            audit_digest=_digest(payload),
        )

    def simulate(
        self,
        request: RecipientSimulationRequestV1,
        *,
        fact_candidates: Sequence[FactCandidateV1],
        now: datetime | str,
        min_confidence: float = 0.0,
    ) -> RecipientSimulationResultV1:
        if not isinstance(request, RecipientSimulationRequestV1):
            raise ValueError("request must be RecipientSimulationRequestV1")
        mode = recipient_simulator_mode()
        if mode == "off":
            return self._result(request, mode=mode, evaluated=False)

        risks: list[SimulationRiskV1] = []
        repairs: list[RepairSuggestionV1] = []
        authorized_fact_refs: tuple[str, ...] = ()
        active_arc_refs: tuple[str, ...] = ()
        fact_projection_digest = ""
        arc_projection_digest = ""

        # An unattested identity is a boundary result, not permission to query
        # broader stores.  In particular, no global or owner fallback occurs.
        if not request.recipient.attested \
                or not request.recipient.viewer_person_id:
            risks.append(SimulationRiskV1(
                code="recipient_identity_unattested", severity="critical"))
            return self._result(
                request,
                mode=mode,
                evaluated=True,
                risks=risks,
                authorized_fact_refs=(),
                active_arc_refs=(),
            )

        try:
            fact_projection = project_facts(
                tuple(fact_candidates),
                request.recipient,
                now=now,
                min_confidence=min_confidence,
                max_facts=64,
                max_total_chars=24_000,
            )
            authorized_fact_refs = tuple(
                fact.fact_ref for fact in fact_projection.facts)
            fact_projection_digest = fact_projection.audit_digest

            arcs = ()
            if self._arc_store is not None:
                arc_projection = self._arc_store.project_active(
                    request.recipient,
                    now=now,
                    max_arcs=24,
                    max_topic_chars=8_000,
                )
                arcs = arc_projection.arcs
                active_arc_refs = tuple(arc.arc_id for arc in arcs)
                arc_projection_digest = arc_projection.audit_digest
                if arc_projection.corrupt_count or arc_projection.truncated:
                    risks.append(SimulationRiskV1(
                        code="arc_projection_incomplete", severity="medium"))
                    repairs.append(RepairSuggestionV1(
                        code="review_arc_context", priority="medium"))

            authorized = set(authorized_fact_refs)
            for fact_ref in request.draft_fact_refs:
                if fact_ref not in authorized:
                    risks.append(SimulationRiskV1(
                        code="fact_ref_not_recipient_authorized",
                        severity="critical",
                        fact_ref=fact_ref,
                    ))

            if request.high_salience and not request.draft_fact_refs:
                risks.append(SimulationRiskV1(
                    code="high_salience_provenance_unknown",
                    severity="medium",
                ))
                repairs.append(RepairSuggestionV1(
                    code="add_fact_provenance", priority="medium"))

            if _PRESSURE_RE.search(request.draft_text):
                for arc in arcs:
                    if arc.arc_type == "stress_topic" \
                            and _topic_overlap(request.draft_text, arc.topic):
                        risks.append(SimulationRiskV1(
                            code="stress_topic_pressure",
                            severity="medium",
                            arc_ref=arc.arc_id,
                        ))
                        repairs.append(RepairSuggestionV1(
                            code="soften_pressure_language",
                            priority="medium",
                            related_ref=arc.arc_id,
                        ))

        except Exception:
            # Dependency details can contain private content or host topology;
            # only the stable class is emitted from this boundary.
            risks.append(SimulationRiskV1(
                code="simulation_dependency_error",
                severity=request.risk_class,
            ))
            return self._result(
                request,
                mode=mode,
                evaluated=True,
                risks=risks,
                repairs=repairs,
                authorized_fact_refs=(),
                active_arc_refs=(),
                dependency_failed=True,
            )

        return self._result(
            request,
            mode=mode,
            evaluated=True,
            risks=risks,
            repairs=repairs,
            authorized_fact_refs=authorized_fact_refs,
            active_arc_refs=active_arc_refs,
            fact_projection_digest=fact_projection_digest,
            arc_projection_digest=arc_projection_digest,
        )


__all__ = [
    "FAIL_BEHAVIOR_BY_RISK",
    "REALTIME_VOICE_SURFACES",
    "RecipientSimulationRequestV1",
    "RecipientSimulationResultV1",
    "RecipientSimulator",
    "RepairSuggestionV1",
    "SIMULATOR_MODES",
    "SimulationRiskV1",
    "is_realtime_voice_surface",
    "recipient_simulator_mode",
]
