"""InferenceHandler — context-enriched LLM inference via the Colony router."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid as _uuid_mod
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from colony_sidecar.task_queue.handlers.base import JobHandler, Job
from colony_sidecar.task_queue.models import JobType

if TYPE_CHECKING:
    from colony_sidecar.router.router import LLMRouter
    from colony_sidecar.world_model.store import WorldModelStore
    from colony_sidecar.world_model.entities import BaseEntity
    from colony_sidecar.contacts.store import ContactStore
    from colony_sidecar.contacts.models import Contact

logger = logging.getLogger(__name__)

_COLONY_IDENTITY = (
    "You are Colony, an intelligent personal AI assistant. "
    "You help the user manage their relationships, tasks, and knowledge. "
    "You have access to context about the user's world and the people in it."
)

_ENTITY_TYPE_MAP = {
    "person": "PersonEntity",
    "company": "CompanyEntity",
    "location": "LocationEntity",
    "product": "ProductEntity",
}


def _build_system_prompt(
    contact: Optional["Contact"],
    wm_entities: List["BaseEntity"],
    explicit_prompt: Optional[str],
) -> str:
    parts = [_COLONY_IDENTITY]

    if contact:
        name = (
            contact.display_name
            or " ".join(p for p in [contact.given_name, contact.family_name] if p)
            or "this contact"
        )
        lines = [f"\nContact context: You are communicating with {name}."]
        lines.append(f"Trust tier: {contact.trust_tier} | Relationship score: {contact.relationship_score:.2f}")
        if contact.organization:
            lines.append(f"Organization: {contact.organization}")
        if contact.notes:
            lines.append(f"Notes: {contact.notes}")
        parts.append("\n".join(lines))

    if wm_entities:
        entity_lines = ["\nRelevant context from your world model:"]
        for e in wm_entities[:5]:
            desc = f"- {e.name} ({e.entity_type})"
            bio = getattr(e, "bio_summary", None) or getattr(e, "description", None)
            if bio:
                desc += f": {bio}"
            entity_lines.append(desc)
        parts.append("\n".join(entity_lines))

    if explicit_prompt:
        parts.append(f"\n{explicit_prompt}")

    return "\n".join(parts)


async def _update_world_model_async(
    wm: "WorldModelStore",
    user_text: str,
    assistant_text: str,
    source_id: str,
) -> None:
    """Extract entities from the exchange and write to world model.

    Runs fire-and-forget. All errors are swallowed to never block the response.
    """
    try:
        from colony_sidecar.world_model.extraction.conversation_extractor import ConversationExtractor
        from colony_sidecar.world_model.entities import (
            PersonEntity, CompanyEntity, LocationEntity, ProductEntity,
        )

        type_cls_map = {
            "person": PersonEntity,
            "company": CompanyEntity,
            "location": LocationEntity,
            "product": ProductEntity,
        }

        extractor = ConversationExtractor(min_message_length=20)
        full_text = f"{user_text}\n{assistant_text}"
        result = await extractor.extract(full_text, source_id=source_id)

        for candidate in result.entities:
            if candidate.confidence < 0.20:
                continue
            try:
                existing = await wm.find_entities(
                    candidate.text,
                    entity_type=candidate.entity_type,
                    min_confidence=0.20,
                    limit=1,
                )
                if existing:
                    await wm.add_observation(
                        existing[0].id,
                        None,
                        f"Mentioned in conversation: {candidate.context_window}",
                        source="inference",
                    )
                else:
                    entity_cls = type_cls_map.get(candidate.entity_type, PersonEntity)
                    ts = int(time.time() * 1000)
                    rand = secrets.token_hex(6)
                    new_entity = entity_cls(
                        id=f"we-{ts}-{rand}",
                        name=candidate.text,
                        confidence=candidate.confidence,
                    )
                    await wm.upsert_entity(new_entity)
                    await wm.add_observation(
                        new_entity.id,
                        None,
                        f"First mentioned in conversation: {candidate.context_window}",
                        source="inference",
                    )
                    logger.debug(
                        "World model: created entity %r (%s)",
                        candidate.text,
                        candidate.entity_type,
                    )
            except Exception:
                logger.debug(
                    "World model upsert failed for %r", candidate.text, exc_info=True
                )

    except Exception:
        logger.debug("Post-inference world model update failed", exc_info=True)


class _InferenceGateSessionStore:
    """Minimal session-store adapter for running the ResponseGate on task-queue jobs.

    Inference jobs are internal and have no real gateway session. This adapter
    creates ephemeral session records so that L1 (RecipientVerifier) and L3
    (CrossContextDetector) can operate without failing on missing-session lookups.
    """

    def __init__(self) -> None:
        self._sessions: dict = {}

    def register(self, session_id: str, contact_id: str, gateway: str) -> None:
        """Register an ephemeral session before calling gate.evaluate()."""
        self._sessions[session_id] = SimpleNamespace(contact_id=contact_id, gateway=gateway)

    async def get(self, session_id: str):
        return self._sessions.get(session_id)

    async def get_contact_gateways(self, contact_id: str) -> set:
        return {"api"}

    async def get_recent_other_sessions(
        self, exclude_session_id: str, lookback_hours: int
    ) -> dict:
        return {}

    async def get_display_name(self, contact_id: str) -> str:
        return contact_id


class InferenceHandler(JobHandler):
    """Run an LLM inference request via the Colony router.

    Job payload keys:
        messages (list[dict], optional): OpenAI-format messages list. Used as-is.
        prompt (str, optional): Plain string prompt; wrapped into a user message.
            One of ``messages`` or ``prompt`` must be provided.
        model_tier (str): "small" | "medium" | "large" (default: "small").
        system_prompt (str, optional): Additional system context appended after
            the enriched Colony identity + contact + world model prompt.
        max_tokens (int, optional): Limit (currently passed as context hint).
        contact_id (str, optional): If provided, contact record is fetched and
            included in the system prompt (name, trust tier, relationship score).

    Returns:
        {"result": str, "tokens_used": int, "model": str}
    """

    def __init__(
        self,
        router: "LLMRouter",
        world_model_store: Optional["WorldModelStore"] = None,
        contact_store: Optional["ContactStore"] = None,
        response_gate: Optional[Any] = None,
        gate_session_store: Optional["_InferenceGateSessionStore"] = None,
    ) -> None:
        self._router = router
        self._wm = world_model_store
        self._cs = contact_store
        self._wm_connected = False
        self._gate = response_gate
        self._gate_sessions = gate_session_store
        self._background_tasks: set = set()  # strong refs to fire-and-forget tasks

    async def _ensure_wm_connected(self) -> None:
        """Connect the world model store on first use.

        If no store was injected, create one from the default config so that
        inference jobs self-initialize world model support without requiring
        changes to startup code.
        """
        if self._wm_connected:
            return
        if self._wm is None:
            try:
                from colony_sidecar.world_model.store import WorldModelStore
                from colony_sidecar.world_model.config import WorldModelConfig
                import os
                colony_home = os.environ.get("COLONY_HOME", os.path.expanduser("~/.colony"))
                db_path = os.path.join(colony_home, "world_model.db")
                self._wm = WorldModelStore(WorldModelConfig(sqlite_path=db_path))
            except Exception:
                logger.warning("Could not create WorldModelStore", exc_info=True)
                return
        try:
            await self._wm.connect()
            self._wm_connected = True
        except Exception:
            logger.warning("Could not connect world model store", exc_info=True)
            self._wm = None

    async def _execute_thought(self, job: Job) -> Dict[str, Any]:
        """Execute exactly the digest-bound ThoughtJobV1 prompt contract."""

        from colony_sidecar.cognition.goal_spine import (
            ThoughtJobV1,
            bind_thought_output,
        )
        from colony_sidecar.router.tiers import ModelTier

        thought = ThoughtJobV1.from_payload(job.payload)
        if job.job_id != thought.thought_job_id:
            raise ValueError("thought queue job ID does not match authority digest")
        messages = [
            {"role": "system", "content": thought.system_prompt},
            {"role": "user", "content": thought.prompt},
        ]
        response = await self._router.complete(
            messages,
            force_tier=ModelTier.SMALL,
            context={
                "task": "thought_job",
                "max_output_tokens": thought.max_output_tokens,
            },
        )
        usage = response.usage or {}
        tokens_used = int(usage.get("total_tokens", 0) or 0)
        completion_tokens = int(
            usage.get("completion_tokens", 0) or 0
        )
        if completion_tokens > thought.max_output_tokens:
            raise ValueError("thought output exceeded its token budget")
        bound_output = bind_thought_output(response.content, thought)
        canonical_output = json.dumps(
            bound_output.payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return {
            "status": "completed",
            "summary": canonical_output,
            "action_plane": {"state": "completed"},
            "result": canonical_output,
            "thought_output": canonical_output,
            "tokens_used": tokens_used,
            "completion_tokens": completion_tokens,
            "model": response.model_id,
        }

    async def _gate_context(
        self,
        messages: list[dict],
        user_text: str,
        force_tier: Optional[Any],
        payload: Dict[str, Any],
    ) -> list[dict]:
        """Shrink oversized message content to the target tier's useful window.

        Uses the context gate (:mod:`colony_sidecar.contextgate`): when the
        assembled messages exceed the tier's ``useful_context_tokens`` the
        largest message content is chunked and retrieved/sampled down to
        budget. No-op when the gate is off, the budget is unknown, or the
        input already fits. Jobs may opt out via ``payload["context_gate"]
        = "off"`` and may pass an explicit ``payload["query"]`` to focus
        retrieval.
        """
        if payload.get("context_gate") == "off":
            return messages
        try:
            from colony_sidecar.contextgate import (
                GateConfig,
                estimate_tokens,
                prepare_context,
            )

            gcfg = GateConfig.from_env()
            if gcfg.mode == "off":
                return messages

            # Budget from the tier the call will actually use
            tier = force_tier
            if tier is None:
                try:
                    tier = self._router.route(user_text, {})[0]
                except Exception:
                    return messages
            tier_cfg = getattr(self._router, "tier_config", lambda _t: None)(tier)
            budget = getattr(tier_cfg, "useful_context_tokens", 0) or gcfg.default_budget_tokens
            if budget <= 0:
                return messages

            est_total = sum(
                estimate_tokens(str(m.get("content") or "")) for m in messages
            )
            if est_total <= budget * gcfg.headroom:
                return messages

            # Gate the single largest content block (usually the document)
            idx = max(
                range(len(messages)),
                key=lambda i: len(str(messages[i].get("content") or "")),
            )
            big = str(messages[idx].get("content") or "")
            # Query focus: explicit payload query wins; if the oversized
            # message is itself the user turn, its tail usually carries the
            # actual question, otherwise the whole user text is the query.
            query = payload.get("query") or (
                user_text[-1000:] if big == user_text else user_text[:2000]
            )
            overhead = est_total - estimate_tokens(big)
            prepared = await prepare_context(
                content=big,
                query=query,
                budget_tokens=max(1, budget - overhead),
                task_kind=payload.get("task_kind"),
                config=gcfg,
            )
            from colony_sidecar.contextgate import GateDecision

            if prepared.decision != GateDecision.PASS_THROUGH:
                gated = list(messages)
                gated[idx] = {**messages[idx], "content": prepared.text}
                logger.info(
                    "Inference context gated: %s, %d -> %d est tokens (tier=%s)",
                    prepared.decision.value,
                    prepared.est_tokens_in,
                    prepared.est_tokens_out,
                    getattr(tier, "value", tier),
                )
                return gated
            return messages
        except Exception:
            logger.warning("Context gate failed — sending messages ungated", exc_info=True)
            return messages

    async def execute(self, job: Job) -> Dict[str, Any]:
        payload = job.payload
        if (
            job.job_type is JobType.THOUGHT
            or payload.get("schema") == "ThoughtJobV1"
        ):
            if job.job_type is not JobType.THOUGHT:
                raise ValueError("ThoughtJobV1 requires the thought job type")
            return await self._execute_thought(job)
        cognition_read_only = payload.get("cognition_read_only") is True
        read_capabilities = set(payload.get("allowed_read_capabilities") or ())
        if cognition_read_only:
            invalid = [
                capability for capability in read_capabilities
                if capability not in {
                    "concerns:read", "directives:read", "memory:read",
                    "projects:read", "reasoning", "situation:read",
                    "web:read", "world_model:read",
                }
            ]
            if invalid:
                raise ValueError("read-only cognition job contains invalid capabilities")
        model_tier = payload.get("model_tier")
        contact_id: Optional[str] = payload.get("contact_id")
        explicit_system = payload.get("system_prompt")

        # ── Resolve tier ───────────────────────────────────────────────────
        force_tier = None
        try:
            from colony_sidecar.router.tiers import ModelTier
            force_tier = ModelTier(model_tier)
        except (ImportError, ValueError):
            pass

        # ── Extract user text ──────────────────────────────────────────────
        if "messages" in payload:
            messages_in: list[dict] = list(payload["messages"])
            user_text = next(
                (m.get("content", "") for m in reversed(messages_in) if m.get("role") == "user"),
                "",
            )
        else:
            user_text = payload.get("prompt", "")
            messages_in = None

        # ── Ensure world model is connected ────────────────────────────────
        if not cognition_read_only or "world_model:read" in read_capabilities:
            await self._ensure_wm_connected()

        # ── Contact lookup ─────────────────────────────────────────────────
        contact: Optional["Contact"] = None
        if contact_id and self._cs and not cognition_read_only:
            try:
                contact = await self._cs.get(contact_id)
            except Exception:
                logger.debug("Contact lookup failed for %s", contact_id, exc_info=True)

        # ── World model entity search ──────────────────────────────────────
        # Extract entity names from the user message first, then look each up
        # individually. Passing the full message as an FTS query fails because
        # stop words prevent name matching.
        wm_entities: List["BaseEntity"] = []
        if self._wm and user_text and (
            not cognition_read_only or "world_model:read" in read_capabilities
        ):
            try:
                from colony_sidecar.world_model.extraction.conversation_extractor import (
                    ConversationExtractor,
                )
                extractor = ConversationExtractor(min_message_length=5)
                extraction = await extractor.extract(user_text, source_id="pre-inference")
                seen_ids: set = set()
                for candidate in extraction.entities[:8]:
                    hits = await self._wm.find_entities(
                        candidate.text, limit=2, min_confidence=0.20
                    )
                    for e in hits:
                        if e.id not in seen_ids:
                            seen_ids.add(e.id)
                            wm_entities.append(e)
                            if len(wm_entities) >= 5:
                                break
                    if len(wm_entities) >= 5:
                        break
            except Exception:
                logger.debug("World model query failed", exc_info=True)

        # ── Build enriched system prompt ───────────────────────────────────
        enriched_system = _build_system_prompt(contact, wm_entities, explicit_system)

        # ── Assemble messages ──────────────────────────────────────────────
        if messages_in is not None:
            messages: list[dict] = messages_in
            if not any(m.get("role") == "system" for m in messages):
                messages.insert(0, {"role": "system", "content": enriched_system})
        else:
            messages = [
                {"role": "system", "content": enriched_system},
                {"role": "user", "content": user_text},
            ]

        # ── Context gate: fit input to the tier's useful window ───────────
        messages = await self._gate_context(messages, user_text, force_tier, payload)

        # ── LLM call ──────────────────────────────────────────────────────
        router_context: Dict[str, Any] = {}
        if cognition_read_only:
            router_context = {
                "task": "thought_job",
                "max_output_tokens": int(payload.get("max_output_tokens") or 768),
            }
        response = await self._router.complete(
            messages, force_tier=force_tier, context=router_context,
        )
        tokens_used = response.usage.get("total_tokens", 0) if response.usage else 0
        completion_tokens = (
            response.usage.get("completion_tokens", 0) if response.usage else 0
        )
        if cognition_read_only and completion_tokens > int(
            payload.get("max_output_tokens") or 768
        ):
            raise ValueError("thought output exceeded its token budget")

        # ── GAP-14: Log tier selection + feed RouterSelfLearner ────────────
        # Logging lets ops trace routing decisions; record_outcome lets the
        # self-learner improve future tier thresholds from inference-path data.
        logger.info(
            "Inference tier=%s model=%s tokens=%d cost_usd=%.6f latency_ms=%d job_id=%s",
            response.tier_used.value,
            response.model_id,
            tokens_used,
            response.cost_usd,
            response.latency_ms,
            getattr(job, "job_id", "?"),
        )
        # Latency/tokens above are telemetry.  Do not self-award a perfect
        # quality label merely because the model returned; RouterSelfLearner
        # must wait for downstream or owner evidence.

        # Queue inference is an observation path.  It must not hide a world-
        # model mutation behind a read-looking completion.  Entity ingestion
        # remains owned by the explicit conversation/memory ingestion paths.
        job_id_str = str(getattr(job, "job_id", None) or getattr(job, "id", _uuid_mod.uuid4()))

        # ── Response Gate evaluation ────────────────────────────────────────
        if (not cognition_read_only
                and self._gate is not None and self._gate_sessions is not None):
            from colony_sidecar.gate.models import GatePayload
            from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier

            session_id = payload.get("session_id") or f"inf-{job_id_str}"
            gateway = payload.get("gateway", "api")
            gate_contact_id = contact_id or "internal"

            # Resolve trust tier from contact record (fall back to PERIPHERAL)
            tier = TrustTier.PERIPHERAL
            if contact is not None:
                try:
                    tier = TrustTier(contact.trust_tier)
                except (ValueError, TypeError):
                    pass

            self._gate_sessions.register(session_id, gate_contact_id, gateway)
            gate_payload = GatePayload(
                response_text=response.content,
                target_contact_id=gate_contact_id,
                target_gateway=gateway,
                session_id=session_id,
                trust_tier=tier,
                mentioned_entities=frozenset(e.name for e in wm_entities),
                turn_id=job_id_str,
                incoming_message_text=user_text,
            )
            try:
                gate_decision = await self._gate.evaluate(gate_payload)
                if gate_decision.blocked:
                    logger.warning(
                        "Gate blocked inference response: layer=%d reason=%s turn_id=%s",
                        gate_decision.blocking_layer,
                        gate_decision.block_reason,
                        job_id_str,
                    )
                    return {
                        "status": "skipped",
                        "summary": "response blocked by the configured response gate",
                        "action_plane": {"state": "skipped"},
                        "result": f"[Response blocked by gate layer {gate_decision.blocking_layer}: {gate_decision.block_reason}]",
                        "tokens_used": tokens_used,
                        "model": response.model_id,
                        "gate_blocked": True,
                        "gate_reason": gate_decision.block_reason,
                        "gate_layer": gate_decision.blocking_layer,
                    }
                logger.debug(
                    "Gate passed inference response: turn_id=%s layers_evaluated=7",
                    job_id_str,
                )
            except Exception:
                logger.warning(
                    "Gate evaluation failed; applying recipient fallback: turn_id=%s",
                    job_id_str,
                    exc_info=True,
                )
                owner_id = os.environ.get(
                    "COLONY_OWNER_PERSON_ID",
                    os.environ.get("COLONY_OWNER_CONTACT_ID", "owner"),
                ).strip() or "owner"
                if contact_id and contact_id not in {"internal", owner_id}:
                    return {
                        "status": "skipped",
                        "summary": (
                            "response held because the recipient gate was "
                            "unavailable"
                        ),
                        "action_plane": {"state": "skipped"},
                        "result": "[Response held for recipient review]",
                        "tokens_used": tokens_used,
                        "model": response.model_id,
                        "gate_blocked": True,
                        "gate_reason": "recipient_gate_unavailable",
                    }

        return {
            "status": "completed",
            "summary": response.content,
            "action_plane": {"state": "completed"},
            "result": response.content,
            "tokens_used": tokens_used,
            "completion_tokens": completion_tokens,
            "model": response.model_id,
        }


class ThoughtOnlyInferenceHandler(InferenceHandler):
    """P3 handler that refuses every non-ThoughtJobV1 lane.

    This is a deployment boundary, not merely a branch in the generic
    inference handler. A worker carrying only this handler can advertise only
    ``thought`` and cannot race general workers for other queue lanes.
    """

    thought_only = True
    handler_contract = "thought_only:v1"

    async def execute(self, job: Job) -> Dict[str, Any]:
        if job.job_type is not JobType.THOUGHT:
            raise ValueError("thought-only handler refuses non-thought job type")
        if (job.payload or {}).get("schema") != "ThoughtJobV1":
            raise ValueError("thought-only handler requires ThoughtJobV1")
        return await self._execute_thought(job)
