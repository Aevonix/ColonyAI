"""Shared, shadow-only integration facade for the reviewed P8 primitives.

This module owns no authentication and grants no authority.  HTTP callers must
provide a server-sealed ``ViewerContextV1``; the autonomy observer uses only the
recipient and channel already resolved by the delivery bridge.  It composes the
existing visibility, arc, simulator, and audit implementations rather than
introducing alternate stores or policy engines.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from typing import Any, Mapping, Optional

from colony_sidecar.tom.arcs import ArcStore
from colony_sidecar.tom.fact_adapters import (
    FactPayloadV1,
    ServerFactAuthorityV1,
    build_fact_candidate,
)
from colony_sidecar.tom.recipient_audit import (
    AUDIT_SURFACES,
    RecipientSimulationAuditStore,
    evaluation_event_from_result,
    sample_event,
)
from colony_sidecar.tom.recipient_simulator import (
    RecipientSimulationRequestV1,
    RecipientSimulator,
    is_realtime_voice_surface,
    recipient_simulator_mode,
)
from colony_sidecar.tom.visibility import (
    FactCandidateV1,
    FactProjectionBatchV1,
    ViewerContextV1,
    content_digest,
    project_facts,
)
from colony_sidecar.tom.visibility_store import FactVisibilityStore


P8_INTEGRATION_MODES = frozenset({"off", "shadow"})
MAX_SHARED_FACT_ROWS = 512
MAX_OUTBOUND_FACT_REFS = 64
DEFAULT_MIN_CONFIDENCE = 0.5
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")


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


def _as_utc(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def p8_integration_mode() -> str:
    """Only explicit shadow enables shared wiring; live remains dark."""

    mode = recipient_simulator_mode()
    return "shadow" if mode == "shadow" else "off"


def p8_min_confidence() -> float:
    """Return a strictly positive integration floor; malformed fails safe."""

    try:
        value = float(os.environ.get(
            "COLONY_P8_FACT_MIN_CONFIDENCE",
            str(DEFAULT_MIN_CONFIDENCE),
        ))
    except (TypeError, ValueError):
        return DEFAULT_MIN_CONFIDENCE
    return value if 0.0 < value <= 1.0 else DEFAULT_MIN_CONFIDENCE


def _projection_min_confidence(override: Optional[float]) -> float:
    """Apply a caller override only when it tightens the integration floor."""

    floor = p8_min_confidence()
    if override is None:
        return floor
    try:
        value = float(override)
    except (TypeError, ValueError):
        return floor
    if not 0.0 < value <= 1.0:
        return floor
    return max(floor, value)


def _owner_person_id() -> str:
    return (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )


class P8ProjectedFactsView:
    """Read-compatible, current P8 projection for legacy ToM consumers.

    It deliberately implements only bounded ``get_fact``/``list_facts``
    semantics and never exposes an un-enveloped, stale, below-floor, or
    viewer-unauthorized SharedFacts row.
    """

    def __init__(
        self,
        *,
        runtime: "P8Runtime",
        viewer: ViewerContextV1,
        now: datetime | str,
    ) -> None:
        if not isinstance(viewer, ViewerContextV1) or not viewer.attested:
            raise ValueError("projected facts view requires attested viewer")
        self._runtime = runtime
        self._viewer = viewer
        self._now = _as_utc(now, field="now")
        self._cache: dict[str, Optional[dict[str, Any]]] = {}

    def _project_record(
        self,
        record: Mapping[str, Any],
    ) -> Optional[dict[str, Any]]:
        candidate = self._runtime._candidate_for_record(record)
        if candidate is None:
            return None
        batch = project_facts(
            (candidate,),
            self._viewer,
            now=self._now,
            min_confidence=p8_min_confidence(),
            max_facts=1,
            max_total_chars=12_000,
        )
        if not batch.facts:
            return None
        projected = batch.facts[0]
        return {
            "id": str(record.get("id") or ""),
            "contact_id": projected.subject_person_id,
            "fact": projected.content,
            "source": str(record.get("source") or ""),
            "confidence": projected.confidence,
            "created_at": str(record.get("created_at") or ""),
            "expires_at": record.get("expires_at"),
            "metadata": None,
        }

    def get_fact(self, fact_id: str) -> Optional[dict[str, Any]]:
        key = str(fact_id or "")
        if key not in self._cache:
            record = self._runtime.facts_store.get_fact(key)
            self._cache[key] = (
                None if record is None else self._project_record(record))
        row = self._cache[key]
        return None if row is None else dict(row)

    def list_facts(
        self,
        *,
        contact_id: Optional[str] = None,
        source: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        bounded_limit = max(1, min(int(limit), MAX_SHARED_FACT_ROWS))
        bounded_offset = max(0, int(offset))
        result = self._runtime.facts_store.list_facts(
            contact_id=contact_id,
            source=source,
            min_confidence=0.0,
            limit=MAX_SHARED_FACT_ROWS,
            offset=0,
        )
        rows = result if isinstance(result, list) else result.get("facts", ())
        projected = []
        floor = max(float(min_confidence), p8_min_confidence())
        for record in rows or ():
            row = self._project_record(record)
            if row is not None and float(row["confidence"]) >= floor:
                projected.append(row)
        window = projected[
            bounded_offset:bounded_offset + bounded_limit]
        return {
            "facts": window,
            "total": len(projected),
            "limit": bounded_limit,
            "offset": bounded_offset,
        }


class P8Runtime:
    """One-process owner of P8 stores and pure integration adapters."""

    def __init__(
        self,
        *,
        visibility_store: FactVisibilityStore,
        arc_store: ArcStore,
        audit_store: RecipientSimulationAuditStore,
        facts_store: Any,
        mode: str = "shadow",
    ) -> None:
        if mode != "shadow":
            raise ValueError("P8Runtime integration is shadow-only")
        if facts_store is None:
            raise ValueError("P8Runtime requires the canonical SharedFactsStore")
        self.visibility_store = visibility_store
        self.arc_store = arc_store
        self.audit_store = audit_store
        self.facts_store = facts_store
        self.mode = mode
        self._simulator = RecipientSimulator(arc_store=arc_store)
        self._closed = False

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "mode": self.mode,
            "visibility_store": "attached",
            "arc_store": "attached",
            "recipient_audit_store": "attached",
            "delivery_effect": False,
            "authority_granted": False,
            "synchronous_voice_gate": False,
            "recipient_audit_scope": "owner_wide_or_exact_scope_revision",
            "fact_min_confidence": p8_min_confidence(),
        }

    @staticmethod
    def shared_fact_ref(record: Mapping[str, Any]) -> str:
        row_id = str(record.get("id") or "").strip()
        if not row_id:
            raise ValueError("shared fact record requires an id")
        base = row_id if _REF_RE.fullmatch(row_id) else _digest(row_id)[:32]
        revision = _digest({
            "id": row_id,
            "contact_id": record.get("contact_id"),
            "content_digest": content_digest(str(record.get("fact") or "")),
            "confidence": record.get("confidence"),
            "created_at": record.get("created_at"),
            "expires_at": record.get("expires_at"),
        })[:24]
        return f"shared_fact:{base}:{revision}"

    @staticmethod
    def _fresh_until(record: Mapping[str, Any]) -> datetime:
        observed = _as_utc(record.get("created_at"), field="created_at")
        if record.get("expires_at"):
            fresh = _as_utc(record["expires_at"], field="expires_at")
            if fresh <= observed:
                raise ValueError("shared fact expiry must follow creation")
            return fresh
        raw_days = os.environ.get("COLONY_P8_FACT_TTL_DAYS", "30")
        try:
            days = int(raw_days)
        except ValueError:
            days = 30
        days = max(1, min(days, 3650))
        return observed + timedelta(days=days)

    def append_shared_fact(
        self,
        record: Mapping[str, Any],
        *,
        producer: ViewerContextV1,
        origin: str,
    ) -> FactCandidateV1:
        """Persist one current SharedFacts receipt through typed authority."""

        if not isinstance(producer, ViewerContextV1) or not producer.attested:
            raise ValueError("shared fact producer must be server-attested")
        subject = str(record.get("contact_id") or "").strip()
        if producer.viewer_person_id not in {subject, producer.owner_person_id}:
            raise ValueError("shared fact subject exceeds producer authority")
        payload = FactPayloadV1.from_untrusted({
            "content": record.get("fact"),
            "confidence": record.get("confidence"),
        }, origin=origin)
        fact_ref = self.shared_fact_ref(record)
        receipt_ref = f"shared_fact:{str(record.get('id') or '').strip()}"
        if not _REF_RE.fullmatch(receipt_ref):
            receipt_ref = f"shared_fact:{_digest(receipt_ref)[:32]}"
        authority = ServerFactAuthorityV1(
            fact_ref=fact_ref,
            source_ref=receipt_ref,
            subject_person_id=subject,
            viewer_scope=f"person:{subject}",
            shareability="subject_private",
            observed_at=record.get("created_at"),
            fresh_until=self._fresh_until(record),
            evidence_refs=(receipt_ref,),
        )
        candidate = build_fact_candidate(authority=authority, payload=payload)
        self.visibility_store.append(candidate)
        return candidate

    def _rows_for_person(self, person_id: str) -> tuple[Mapping[str, Any], ...]:
        result = self.facts_store.list_facts(
            contact_id=person_id,
            limit=MAX_SHARED_FACT_ROWS,
            offset=0,
        )
        rows = result if isinstance(result, list) else result.get("facts", ())
        return tuple(rows or ())[:MAX_SHARED_FACT_ROWS]

    def fact_candidates(
        self,
        *,
        subject_person_id: str,
    ) -> tuple[FactCandidateV1, ...]:
        """Rejoin current content to its immutable envelope; legacy misses skip."""

        candidates: list[FactCandidateV1] = []
        for record in self._rows_for_person(subject_person_id):
            candidate = self._candidate_for_record(record)
            if candidate is not None:
                candidates.append(candidate)
        return tuple(candidates)

    def _candidate_for_record(
        self,
        record: Mapping[str, Any],
    ) -> Optional[FactCandidateV1]:
        try:
            envelope = self.visibility_store.get(
                self.shared_fact_ref(record))
            if envelope is None:
                return None
            return FactCandidateV1(
                content=str(record.get("fact") or ""),
                visibility=envelope,
            )
        except Exception:
            # Changed/corrupt/unscoped records fail closed and never render.
            return None

    def projected_facts_view(
        self,
        viewer: ViewerContextV1,
        *,
        now: datetime | str,
    ) -> P8ProjectedFactsView:
        return P8ProjectedFactsView(
            runtime=self, viewer=viewer, now=now)

    def project_shared_facts(
        self,
        viewer: ViewerContextV1,
        *,
        now: datetime | str,
        subject_person_id: Optional[str] = None,
        max_facts: int = 5,
        max_total_chars: int = 8_000,
        min_confidence: Optional[float] = None,
    ) -> FactProjectionBatchV1:
        subject = str(
            subject_person_id or viewer.viewer_person_id or "").strip()
        return project_facts(
            self.fact_candidates(subject_person_id=subject),
            viewer,
            now=now,
            min_confidence=_projection_min_confidence(min_confidence),
            max_facts=max_facts,
            max_total_chars=max_total_chars,
        )

    def internal_recipient_viewer(
        self,
        person_id: str,
        *,
        surface: str,
    ) -> ViewerContextV1:
        """Bind the recipient already resolved by the server delivery bridge."""

        person = str(person_id or "").strip()
        if not _REF_RE.fullmatch(person):
            raise ValueError("delivery recipient is not a bounded person reference")
        owner = _owner_person_id()
        audiences = ("viewer", "owner") if person == owner else ("viewer",)
        revision = _digest({
            "principal": "internal:autonomy",
            "person": person,
            "owner": owner,
            "surface": str(surface or "").lower(),
        })
        return ViewerContextV1(
            principal_id="internal:autonomy",
            viewer_person_id=person,
            owner_person_id=owner,
            audiences=audiences,
            conversation_scope="",
            scope_revision=f"scope:{revision}",
            attested=True,
        )

    @staticmethod
    def _surface(preview: Mapping[str, Any]) -> str:
        target = preview.get("target")
        target = target if isinstance(target, Mapping) else {}
        chat = str(target.get("user_chat") or target.get("home_chat") or "")
        platform, separator, _chat_id = chat.partition(":")
        return platform.strip().lower() if separator else "chat"

    @staticmethod
    def _outbound_fact_refs(payload: Mapping[str, Any]) -> tuple[str, ...]:
        context = payload.get("context")
        context = context if isinstance(context, Mapping) else {}
        values = context.get("fact_refs") or payload.get("fact_refs") or ()
        if not isinstance(values, (list, tuple)):
            return ()
        refs = tuple(sorted(dict.fromkeys(
            str(value).strip() for value in values
            if _REF_RE.fullmatch(str(value).strip())
        )))
        return refs[:MAX_OUTBOUND_FACT_REFS]

    def observe_outbound_payload(
        self,
        payload: Mapping[str, Any],
        preview: Mapping[str, Any],
        *,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Journal an ignored shadow advisory for one sanitized text draft."""

        if self.mode != "shadow" or recipient_simulator_mode() != "shadow":
            return {"observed": False, "reason": "mode_disabled"}
        surface = self._surface(preview)
        if is_realtime_voice_surface(surface):
            return {"observed": False, "reason": "realtime_surface"}
        if surface not in AUDIT_SURFACES:
            return {"observed": False, "reason": "unsupported_surface"}
        person_id = str(preview.get("person_id") or "").strip()
        draft = str(
            payload.get("description") or payload.get("title") or "").strip()
        if not person_id or not draft:
            return {"observed": False, "reason": "draft_or_recipient_missing"}
        observed = (
            datetime.now(timezone.utc)
            if now is None else _as_utc(now, field="now")
        )
        viewer = self.internal_recipient_viewer(person_id, surface=surface)
        material = {
            "payload_id": str(payload.get("id") or ""),
            "draft_digest": content_digest(draft),
            "recipient": person_id,
            "surface": surface,
        }
        key = _digest(material)
        outbound_ref = f"outbound:{key}"
        sample = sample_event(
            event_id=f"audit:sample:{key}",
            idempotency_key=f"sample:{key}",
            outbound_item_ref=outbound_ref,
            recipient=viewer,
            high_salience=True,
            draft_text=draft,
            sampled_at=observed,
        )
        self.audit_store.append(sample)

        request = RecipientSimulationRequestV1(
            simulation_id=f"simulation:{key}",
            draft_text=draft,
            draft_fact_refs=self._outbound_fact_refs(payload),
            recipient=viewer,
            risk_class=(
                "medium" if person_id == viewer.owner_person_id else "high"),
            surface=surface,
            high_salience=True,
            created_at=observed.isoformat(),
        )
        result = self._simulator.simulate(
            request,
            fact_candidates=self.fact_candidates(
                subject_person_id=person_id),
            now=observed,
            min_confidence=p8_min_confidence(),
        )
        audit = evaluation_event_from_result(
            event_id=f"audit:evaluation:{key}",
            idempotency_key=f"evaluation:{key}",
            outbound_item_ref=outbound_ref,
            request=request,
            result=result,
            evaluated_at=observed,
        )
        self.audit_store.append(audit)
        return {
            "observed": True,
            "result": result.public(),
            "audit": audit.public(),
        }

    def deck_projection(
        self,
        viewer: ViewerContextV1,
        *,
        now: datetime | str,
        subject_person_id: Optional[str] = None,
        max_facts: int = 24,
        max_arcs: int = 24,
        max_audit_events: int = 64,
    ) -> dict[str, Any]:
        subject = str(
            subject_person_id or viewer.viewer_person_id or "").strip()
        facts = self.project_shared_facts(
            viewer,
            now=now,
            subject_person_id=subject,
            max_facts=max_facts,
            max_total_chars=24_000,
        )
        visibility = self.visibility_store.project_authorized(
            viewer,
            now=now,
            min_confidence=p8_min_confidence(),
            max_envelopes=max_facts,
        )
        arcs = self.arc_store.project_active(
            viewer, now=now, max_arcs=max_arcs, max_topic_chars=8_000)
        audit = self.audit_store.project(
            viewer, max_events=max_audit_events)
        coverage = self.audit_store.coverage(viewer)
        return {
            "enabled": True,
            "mode": self.mode,
            "subject_person_id": subject,
            "facts": facts.public(),
            "visibility": visibility.public(),
            "arcs": arcs.public(),
            "recipient_audit": audit.public(),
            "coverage": coverage.public(),
            "advisory_only": True,
            "synchronous_voice_gate": False,
            "recipient_audit_scope": "owner_wide_or_exact_scope_revision",
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for store in (self.audit_store, self.arc_store, self.visibility_store):
            try:
                store.close()
            except Exception:
                pass


__all__ = [
    "P8Runtime",
    "P8ProjectedFactsView",
    "P8_INTEGRATION_MODES",
    "p8_integration_mode",
    "p8_min_confidence",
]
