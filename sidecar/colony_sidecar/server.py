"""Colony sidecar FastAPI server.

Intelligence sidecar server mounted by agent frameworks (OpenClaw, Hermes,
etc.) as a plugin via the ``/v1/host`` API surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# Load ~/.colony/.env before any config reads (mirrors CLI behaviour for
# service/standalone launches that skip the CLI entrypoint).
_env_loaded = False
_skip_dotenv = os.environ.get("COLONY_SKIP_DOTENV", "").strip().lower() in {
    "1", "true", "yes", "on",
}
if not _env_loaded and not _skip_dotenv:
    for _env_path in (Path.home() / ".colony" / ".env", Path.cwd() / ".env"):
        if _env_path.exists():
            with open(_env_path) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    if "=" in _line:
                        _k, _v = _line.split("=", 1)
                        _k = _k.strip()
                        _v = _v.strip()
                        if _k not in os.environ:
                            os.environ[_k] = _v
            break
    _env_loaded = True

from fastapi import FastAPI

from colony_sidecar.api.routers.host import (
    router as host_router,
    v2_router as host_v2_router,
    set_llm_router,
    set_autonomy_loop,
    set_scheduler,
    set_chain_manager,
    set_reasoning_loop,
    set_tool_executor,
    set_graph,
    set_consolidator,
    set_response_gate,
    set_signal_collector,
    set_embedder,
    set_reranker,
    set_goals_engine,
    set_contacts_store,
    set_briefings_engine,
    set_world_store,
    set_extraction_pipeline,
    set_metalearner,
    set_research_pipeline,
    set_search_orchestrator,
    set_delivery_bridge,
    set_connection_discoverer,
    set_insight_store,
    set_learner,
    set_skills_registry,
    set_skill_executor,
    set_secrets_manager,
    set_session_store,
    set_task_queue,
    set_commitment_store,
    set_affect_store,
    set_facts_store,
    set_p8_runtime,
    set_context_provenance_store,
    set_response_guard,
    set_engagement_store,
    set_comms_log,
    set_preference_learner,
    set_pattern_store,
    set_surprise_store,
    set_tom_extractor,
    # Multi-Agent v0.7.0
    set_agent_store,
    set_invite_store,
    set_initiative_store,
    set_assignment_engine,
    set_websocket_manager,
    set_telemetry,
    set_session_report_store,
    set_agent_bridge,
    set_initiative_executor,
    set_situation_spine,
    set_cognition_evidence,
    set_drive_governance,
    set_cognition_spine,
    set_cognition_attachment_status,
    set_external_event_intake,
    set_worker_governor,
    supported_capabilities,
)

from colony_sidecar import get_state_dir

logger = logging.getLogger(__name__)


def _state_dir() -> Path:
    """Resolve the Colony state directory (wrapper for get_state_dir)."""
    return get_state_dir()


def _embedded_worker_enabled() -> bool:
    """Whether this process may construct its in-process execution worker.

    Generic installs retain the historical default (enabled). Deployment
    cutovers can pin ``COLONY_EMBEDDED_WORKER_ENABLED=false`` while keeping
    queue maintenance, health, and durable outcome reconciliation online.
    """

    raw = os.environ.get(
        "COLONY_EMBEDDED_WORKER_ENABLED", "true"
    ).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        "COLONY_EMBEDDED_WORKER_ENABLED must be true or false"
    )


def _configured_embedded_worker_enabled() -> bool:
    """Read the same posture for startup wiring outside worker construction.

    The host's release attestation intentionally reserves
    ``_embedded_worker_enabled`` for the exact lifespan branch that constructs
    ``WorkerNode``. Cognition readiness still needs the same strict setting
    before that branch, so keep this non-construction reader behaviorally
    identical and lock both parsers together in lifecycle tests.
    """

    raw = os.environ.get(
        "COLONY_EMBEDDED_WORKER_ENABLED", "true"
    ).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        "COLONY_EMBEDDED_WORKER_ENABLED must be true or false"
    )


def _resolve_guard_mode(raw: Optional[str]):
    """Strict COLONY_GUARD_MODE parser: 'enforce', 'shadow', or unset only.

    An unrecognised value (e.g. a typo of 'enforce') must never silently
    fall back to shadow — that would quietly disable the enforcement the
    operator asked for. Log loudly and raise; the ResponseGuard then stays
    uninitialized and the gate endpoints report unavailable (fail closed).
    """
    from colony_sidecar.gate.response_guard import GuardMode
    value = (raw or "").strip().lower()
    if value in ("", "shadow"):
        return GuardMode.SHADOW
    if value == "enforce":
        return GuardMode.ENFORCE
    logger.error(
        "COLONY_GUARD_MODE=%r is not a recognised guard mode "
        "(expected 'enforce' or 'shadow') — refusing to silently "
        "fall back to shadow", raw,
    )
    raise RuntimeError(
        f"COLONY_GUARD_MODE must be 'enforce' or 'shadow', got {raw!r}"
    )


def _cognition_owner_spec(*, router, node_id: str):
    """Build the mechanically isolated embedded ThoughtJobV1 owner."""

    from colony_sidecar.task_queue.handlers.inference import (
        ThoughtOnlyInferenceHandler,
    )
    from colony_sidecar.task_queue.models import JobType, WorkerCapabilities
    from colony_sidecar.task_queue.routing import THOUGHT_ROUTE

    handlers = {
        JobType.THOUGHT: ThoughtOnlyInferenceHandler(router),
    }
    capabilities = WorkerCapabilities(
        node_id=node_id,
        capabilities={"cognition_scoped", THOUGHT_ROUTE},
        job_types={JobType.THOUGHT},
        max_concurrent=1,
    )
    return handlers, capabilities


def _cognition_worker_profile(*, configured_mode: str, attached: bool) -> str:
    """Select the embedded worker profile without inspecting partial objects.

    A configured cognition owner is never allowed to become a generic action
    worker merely because P3 attachment failed.  The caller must hold worker
    startup when this returns ``held``.
    """

    mode = str(configured_mode or "off").strip().lower()
    if mode in {"shadow", "live"}:
        return "thought_only" if attached else "held"
    return "generic"


def _queue_seconds_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _attach_p8_runtime(*, state_dir: Path, facts_store, graph=None):
    """Atomically attach the reviewed P8 stores in explicit shadow mode.

    Unset, off, unknown, and live all stay dark and create no P8 artifacts.
    Live is intentionally excluded from this integration slice: recipient
    simulation remains an ignored non-real-time shadow observation.
    """

    from colony_sidecar.tom.integration import P8Runtime, p8_integration_mode

    set_p8_runtime(None)
    graph_policy = getattr(graph, "set_recall_source_exclusions", None)
    if callable(graph_policy):
        # Clear any policy left on a reused graph before resolving this mode.
        graph_policy(())
    if p8_integration_mode() != "shadow":
        return None
    if facts_store is None:
        raise RuntimeError("P8 shadow requires the canonical SharedFactsStore")

    visibility = None
    arcs = None
    audit = None
    try:
        from colony_sidecar.tom.arcs import ArcStore
        from colony_sidecar.tom.recipient_audit import (
            open_recipient_simulation_audit_store,
        )
        from colony_sidecar.tom.visibility_store import (
            open_visibility_envelope_store,
        )

        visibility = open_visibility_envelope_store(
            state_dir / "colony-p8-visibility.db", enabled=True)
        arcs = ArcStore(str(state_dir / "colony-p8-arcs.db"))
        audit = open_recipient_simulation_audit_store(
            state_dir / "colony-p8-recipient-audit.db", mode="shadow")
        if visibility is None or audit is None:
            raise RuntimeError("P8 shadow stores failed to open")
        runtime = P8Runtime(
            visibility_store=visibility,
            arc_store=arcs,
            audit_store=audit,
            facts_store=facts_store,
            mode="shadow",
        )
        if graph is not None:
            if not callable(graph_policy):
                raise RuntimeError(
                    "P8 shadow requires graph-wide recall source exclusions")
            # SharedFacts graph rows are compatibility mirrors, not an
            # authorized content path. Typed projection is their only reader.
            graph_policy(
                ("tom:shared_fact",),
                legacy_metadata_markers=("shared_fact",),
            )
        set_p8_runtime(runtime)
        return runtime
    except Exception:
        if callable(graph_policy):
            try:
                graph_policy(())
            except Exception:
                pass
        for store in (audit, arcs, visibility):
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass
        set_p8_runtime(None)
        raise


def _build_research_pipeline(*, graph, p8_runtime):
    """Preserve legacy research ownership unless P8 needs the governed graph."""

    from colony_sidecar.research.pipeline import ResearchPipeline

    if p8_runtime is None:
        return ResearchPipeline()
    return ResearchPipeline(
        graph=graph,
        allow_fallback_graph=False,
    )


async def _governed_autonomy_stop_signal(autonomy_loop) -> None:
    """Request a prompt stop without awaiting the host route's task join."""

    if autonomy_loop is not None:
        await autonomy_loop.stop()


def _work_order_runtime_hold_reason(job, project_store, concern_store) -> str:
    """Resolve one canonical cognition WorkOrder to its current source hold."""

    from collections.abc import Mapping
    from colony_sidecar.self_model.event_concerns import (
        project_turn_concern_hold_reason,
    )
    from colony_sidecar.task_queue.models import JobType
    from colony_sidecar.work_orders import WorkOrderV1

    payload = getattr(job, "payload", None)
    if not isinstance(payload, Mapping) or payload.get("schema") != "WorkOrderV1":
        return ""
    try:
        order = WorkOrderV1.from_payload(payload)
    except Exception:
        # A malformed row is not a canonical WorkOrder.  Fail closed only
        # when it asserts the cognition-spine source this fence owns; generic
        # and external queue jobs retain their existing lifecycle.
        if str(payload.get("source") or "") == "cognition_spine":
            raise
        return ""
    if order.source != "cognition_spine":
        return ""
    if (
        getattr(job, "job_type", None) is not JobType.AGENT_ACTION
        or str(getattr(job, "job_id", "") or "") != order.work_order_id
    ):
        raise RuntimeError("canonical cognition WorkOrder queue identity mismatch")
    ledger = project_store.get_work_order(order.work_order_id)
    if ledger is None:
        raise RuntimeError("canonical cognition WorkOrder ledger row unavailable")
    ledger_order = WorkOrderV1.from_payload(ledger.get("payload") or {})
    if (
        ledger_order.work_order_digest != order.work_order_digest
        or str(ledger.get("work_order_digest") or "")
        != order.work_order_digest
        or str(ledger.get("project_id") or "") != order.project_id
        or str(ledger.get("step_id") or "") != order.step_id
    ):
        raise RuntimeError("canonical cognition WorkOrder ledger binding mismatch")
    project = project_store.get_project(order.project_id)
    if project is None or str(project.source or "") != order.source:
        raise RuntimeError("canonical cognition WorkOrder project binding mismatch")
    return project_turn_concern_hold_reason(project, concern_store)


_COGNITION_RUNTIME_INITIALIZATION_HOLD = (
    "cognition_runtime_initialization_pending"
)


def _cognition_work_order_startup_hold_reason(job) -> str:
    """Hold only cognition WorkOrders until their durable stores are wired.

    This deliberately relies only on the queue payload declaration.  The
    digest-bound verifier cannot run until ProjectStore initialization has
    succeeded, so a payload asserting the owned source must remain held even
    when the rest of that payload is malformed.
    """

    from collections.abc import Mapping

    payload = getattr(job, "payload", None)
    if not isinstance(payload, Mapping):
        return ""
    if (
        payload.get("schema") == "WorkOrderV1"
        and str(payload.get("source") or "") == "cognition_spine"
    ):
        return _COGNITION_RUNTIME_INITIALIZATION_HOLD
    return ""


def _install_cognition_work_order_startup_fence(task_queue) -> None:
    """Install the pre-initialization queue fence when a queue is present."""

    if task_queue is not None:
        task_queue.queue.configure_runtime_claim_hold(
            _cognition_work_order_startup_hold_reason,
        )


def _install_cognition_work_order_runtime_fence(
    task_queue, project_store, concern_store,
) -> None:
    """Replace the startup fence with the complete digest-bound verifier."""

    if task_queue is None:
        return

    def _runtime_hold(job) -> str:
        return _work_order_runtime_hold_reason(
            job, project_store, concern_store,
        )

    task_queue.queue.configure_runtime_claim_hold(_runtime_hold)


def _attach_cognition_spine(
    *,
    state_dir: Path,
    task_queue,
    workspace,
    concern_store,
    project_store,
    project_engine,
    directive_manager,
    llm_router=None,
    embedded_worker_enabled: Optional[bool] = None,
    proposal_store=None,
):
    """Attach the typed P3 cognition spine when its migration flag is on.

    This is kept as one small, directly testable startup seam.  In particular,
    the default-off path must not create a database, and an enabled spine must
    never fall back to a partial attachment when a durable dependency is
    absent.
    """

    from colony_sidecar.cognition.goal_spine import (
        CognitionSpine,
        CognitionSpineStore,
        ThoughtQueueAdapter,
        ThoughtProposalPresentationSink,
        cognition_spine_enabled,
        cognition_spine_mode,
    )
    from colony_sidecar.cognition.runtime import CognitionRuntimeContractV1

    configured_mode = cognition_spine_mode()
    configured_catalog = (
        ["thought"] if configured_mode in {"shadow", "live"} else []
    )
    set_cognition_spine(None, attachment_status={
        "configured_mode": configured_mode,
        "state": "attaching" if cognition_spine_enabled() else "off",
        "reason": (
            "attachment_in_progress" if cognition_spine_enabled()
            else "cognition_not_configured"
        ),
        "configured_handler_catalog": configured_catalog,
        "effective_handler_catalog": [],
    })
    if not cognition_spine_enabled():
        return None

    required = {
        "task_queue": task_queue,
        "workspace": workspace,
        "concern_store": concern_store,
        "project_store": project_store,
        "project_engine": project_engine,
        "directive_manager": directive_manager,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise RuntimeError(
            "P3 cognition spine missing durable dependencies: "
            + ", ".join(missing)
        )

    if cognition_spine_mode() == "live":
        from colony_sidecar.directives import Action
        from colony_sidecar.projects.models import projects_mode

        if projects_mode() != "live":
            raise RuntimeError(
                "P3 live cognition requires ProjectEngine live mode"
            )
        if getattr(project_engine, "_work_orders", None) is None:
            raise RuntimeError(
                "P3 live cognition requires the canonical WorkOrder adapter"
            )
        try:
            # This is a read-only health probe. An active boundary (including
            # an observation blackout) may deny it; either verdict proves the
            # store was readable. Only an exception or malformed result is
            # unhealthy. A read avoids manufacturing a global-pause action
            # block during startup.
            boundary_probe = directive_manager.check(Action(
                kind="read",
                text="colony cognition startup boundary health probe",
                target="cognition startup",
                high_risk=False,
            ))
        except Exception as exc:
            raise RuntimeError(
                "P3 live cognition requires a readable DirectiveGuard"
            ) from exc
        if not isinstance(getattr(boundary_probe, "allowed", None), bool):
            raise RuntimeError(
                "P3 live cognition received a malformed DirectiveGuard verdict"
            )
        if llm_router is None:
            raise RuntimeError(
                "P3 live cognition requires the ThoughtJobV1 LLM router"
            )
        enabled = (
            _configured_embedded_worker_enabled()
            if embedded_worker_enabled is None
            else bool(embedded_worker_enabled)
        )
        if not enabled:
            raise RuntimeError(
                "P3 live cognition requires the embedded strict ThoughtJobV1 "
                "handler"
            )
        configured_owner = os.environ.get(
            "COLONY_THOUGHT_WORKER_NODE_ID", ""
        ).strip()
        if not configured_owner:
            raise RuntimeError(
                "P3 live cognition requires COLONY_THOUGHT_WORKER_NODE_ID"
            )
        from colony_sidecar.chain.node import get_or_create_node_id

        actual_owner = get_or_create_node_id(state_dir)
        if configured_owner != actual_owner:
            raise RuntimeError(
                "COLONY_THOUGHT_WORKER_NODE_ID does not match the local "
                "cognition worker node"
            )

    try:
        project_limit = int(
            os.environ.get("COLONY_PROJECTS_MAX_CONCURRENT", "3")
        )
    except ValueError as exc:
        raise RuntimeError(
            "COLONY_PROJECTS_MAX_CONCURRENT must be an integer"
        ) from exc
    if not 1 <= project_limit <= 100:
        raise RuntimeError(
            "COLONY_PROJECTS_MAX_CONCURRENT must be between 1 and 100"
        )

    available_capabilities = {
        value.strip()
        for value in os.environ.get(
            "COLONY_COGNITION_AVAILABLE_CAPABILITIES",
            "memory:read,reasoning,web:read",
        ).split(",")
        if value.strip()
    }

    def _charter(proposal, concern):
        # Parsing already enforces the typed schema.  This deployment seam
        # adds a deterministic minimum: a goal needs an objective and cited
        # evidence from its originating concern.
        allowed = bool(proposal.objective and proposal.evidence_refs)
        return allowed, (
            "typed_goal_with_source_evidence"
            if allowed
            else "typed_goal_missing_objective_or_source_evidence"
        )

    def _situation(proposal, concern):
        active = project_engine.open_capacity_used()
        allowed = active < project_limit
        return allowed, (
            "capacity_available" if allowed else "project_capacity_exhausted"
        )

    runtime_bindings = {
        "charter_store": None,
        "situation_status": None,
    }

    def _runtime_contract():
        from colony_sidecar.cognition.drive_governance import (
            drive_governance_mode,
        )
        from colony_sidecar.self_model.event_concerns import event_concern_mode
        from colony_sidecar.self_model.workspace import workspace_mode

        active_id = None
        blockers = []
        charter_store = runtime_bindings["charter_store"]
        if drive_governance_mode() == "live" and charter_store is not None:
            try:
                active = charter_store.active_revision("default")
                active_id = active.revision_id if active is not None else None
            except Exception:
                blockers.append("active_charter_read_failed")
        return CognitionRuntimeContractV1.compose(
            requested_mode=cognition_spine_mode(),
            workspace_mode=workspace_mode(),
            event_concern_mode=event_concern_mode(),
            drive_governance_mode=drive_governance_mode(),
            charter_revision_id=active_id,
            charter_store_attached=charter_store is not None,
            attachment_blockers=tuple(blockers),
        )

    def _revision_snapshot():
        try:
            boundary_payload = directive_manager.context_brief() or ""
        except Exception:
            boundary_payload = "directive-store-unavailable"
        policy_payload = {
            "goal_admission_policy": "v1",
            "available_capabilities": sorted(available_capabilities),
        }
        try:
            capacity_payload = {
                "planning": project_store.count("planning"),
                "active": project_store.count("active"),
                "used": project_engine.open_capacity_used(),
                "limit": project_limit,
            }
        except Exception:
            capacity_payload = {"status": "capacity-store-unavailable"}
        situation_provider = runtime_bindings["situation_status"]
        if callable(situation_provider):
            try:
                governed_situation = situation_provider()
            except Exception:
                governed_situation = {"status": "unavailable"}
        else:
            governed_situation = {"status": "p6-not-attached"}
        situation_payload = {
            "capacity": capacity_payload,
            "governed_situation": governed_situation,
        }

        def _revision(label, payload):
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")
            return f"{label}:{hashlib.sha256(encoded).hexdigest()[:24]}"

        return {
            "policy_revision": _revision("policy", policy_payload),
            "situation_revision": _revision("situation", situation_payload),
            "boundary_revision": _revision("boundary", boundary_payload),
        }

    cognition_store = CognitionSpineStore(
        str(state_dir / "colony-cognition.db")
    )
    spine = CognitionSpine(
        concern_store=concern_store,
        cognition_store=cognition_store,
        project_engine=project_engine,
        thought_queue=ThoughtQueueAdapter(
            task_queue, cognition_store=cognition_store
        ),
        directive_manager=directive_manager,
        charter_validator=_charter,
        situation_validator=_situation,
        available_capabilities=available_capabilities,
        enforce_runtime_contract=True,
        runtime_contract_provider=_runtime_contract,
        revision_provider=_revision_snapshot,
        worker_health_provider=(
            task_queue.queue.execution_readiness
            if callable(getattr(task_queue.queue, "execution_readiness", None))
            else None
        ),
        proposal_presentation_sink=(
            ThoughtProposalPresentationSink(proposal_store)
            if proposal_store is not None else None
        ),
        owner_person_id=(
            os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
            or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
            or "owner"
        ),
    )
    spine._runtime_bindings = runtime_bindings
    workspace.cognition_spine = spine
    set_cognition_spine(spine, attachment_status={
        "configured_mode": configured_mode,
        "state": "attached",
        "reason": "cognition_spine_attached_worker_pending",
        "configured_handler_catalog": configured_catalog,
        "effective_handler_catalog": [],
    })
    return spine


def _validator_allowed(result) -> bool:
    """Interpret P3's existing validator shapes without changing its verdict."""

    if isinstance(result, dict):
        return result.get("allowed") is True
    if isinstance(result, (tuple, list)):
        return bool(result and result[0] is True)
    return result is True


def _situation_failure(reason: str) -> dict:
    """Stable fail-closed result consumed by P3's policy normalizer."""

    return {
        "allowed": False,
        "reason": reason,
        "evidence_refs": [],
        "does_not_grant_authority": True,
    }


def _capacity_plus_attachment_failure(original_validator, reason: str):
    """Preserve an existing P3 denial but never allow past failed P6 startup."""

    def _validator(proposal, concern):
        try:
            capacity = original_validator(proposal, concern)
        except Exception:
            logger.exception("P3 capacity validator failed during P6 failure")
            return _situation_failure("capacity_validator_failed_closed")
        if not _validator_allowed(capacity):
            return capacity
        return _situation_failure(reason)

    return _validator


def _attach_situation_spine(
    *,
    state_dir: Path,
    cognition_spine,
    scheduler=None,
    task_queue=None,
):
    """Attach P6 as an observer in shadow and a composed P3 gate in live.

    The off path clears stale in-process router handles before returning and
    constructs nothing.  Shadow deliberately does not replace P3's situation
    validator.  Live preserves the exact capacity denial and consults P6 only
    after capacity allows; any reducer, snapshot, or gate failure is a denial.
    """

    from colony_sidecar.self_model.situation import (
        AppropriatenessGate,
        SituationReducer,
        SituationStore,
        situation_spine_enabled,
        situation_spine_mode,
        task_queue_resource_observation,
    )

    set_situation_spine(None, None)
    if not situation_spine_enabled():
        return None

    mode = situation_spine_mode()
    if mode == "live" and cognition_spine is None:
        raise RuntimeError("P6 live situation spine requires the P3 cognition spine")
    original_validator = (
        getattr(cognition_spine, "_situation", None)
        if mode == "live" else None
    )
    if mode == "live" and not callable(original_validator):
        raise RuntimeError(
            "P6 live situation spine requires P3's capacity validator"
        )
    queue_resource = getattr(task_queue, "queue", None)
    if queue_resource is not None and (
        not callable(getattr(queue_resource, "execution_readiness", None))
        or not callable(getattr(queue_resource, "get_queue_stats", None))
    ):
        raise RuntimeError(
            "P6 task queue observation requires readiness and worker truth"
        )

    store = SituationStore(str(state_dir / "colony-situation.db"))
    try:
        reducer = SituationReducer(store)
        gate = AppropriatenessGate()
        try:
            initial_status = reducer.run_once(limit=100)
        except Exception as exc:  # the live wrapper remains explicitly fail closed
            initial_status = {
                "enabled": True,
                "mode": mode,
                "processed": 0,
                "error": f"initial_reduce_failed:{type(exc).__name__}",
            }

        if scheduler is not None:
            try:
                interval = int(
                    os.environ.get(
                        "COLONY_SITUATION_REDUCE_INTERVAL_SECONDS", "30"
                    )
                )
            except ValueError:
                interval = 30
            interval = max(5, min(3600, interval))

            if queue_resource is None:

                def _reduce_situation():
                    return reducer.run_once(limit=100)

            else:

                async def _reduce_situation():
                    observation = await task_queue_resource_observation(
                        queue_resource,
                    )
                    observed = store.ingest(observation)
                    result = dict(reducer.run_once(limit=100))
                    result["resource_observation"] = {
                        "disposition": observed["disposition"],
                        "observation_id": observation.observation_id,
                        "state": observation.state,
                    }
                    return result

            scheduler.register(
                "situation_reduce",
                _reduce_situation,
                interval_seconds=interval,
                metadata={
                    "description": (
                        "Reduce durable evidence into the scoped P6 situation spine"
                    ),
                    "mode": mode,
                },
            )
    except Exception:
        store.close()
        raise

    if mode == "live":

        def _capacity_plus_situation(proposal, concern):
            try:
                capacity = original_validator(proposal, concern)
            except Exception:
                logger.exception("P3 capacity validator failed before P6")
                return _situation_failure("capacity_validator_failed_closed")
            if not _validator_allowed(capacity):
                return capacity
            try:
                reduced = reducer.run_once(limit=100)
                if not isinstance(reduced, dict) or reduced.get("error"):
                    return _situation_failure("situation_reducer_unhealthy")
                snapshot = store.snapshot(
                    subject_person_id=proposal.subject_person_id,
                    viewer_scope=proposal.viewer_scope,
                )
                verdict = gate.for_goal_proposal(proposal, concern, snapshot)
                if not isinstance(verdict, dict):
                    return _situation_failure("situation_gate_invalid_result")
                return verdict
            except Exception:
                logger.exception("P6 live gate failed closed")
                return _situation_failure("situation_gate_failed_closed")

        cognition_spine._situation = _capacity_plus_situation

    runtime_bindings = getattr(cognition_spine, "_runtime_bindings", None)
    if isinstance(runtime_bindings, dict):
        runtime_bindings["situation_status"] = reducer.status

    set_situation_spine(store, reducer)
    return {
        "mode": mode,
        "store": store,
        "reducer": reducer,
        "gate": gate,
        "initial_status": initial_status,
        "original_validator": original_validator,
    }


def _attach_cognition_evidence(
    *,
    state_dir: Path,
    project_store,
    self_model=None,
    expectations=None,
    scheduler=None,
):
    """Attach the project outbox and optional receipt-derived learning loop.

    Project events are operational truth, so their journal projector remains
    active even when learning is off. Off mode keeps only a minimal durable
    passthrough cursor so outcomes already counted by the legacy writer cannot
    be replayed as new learning after live is re-enabled. Live mode requires
    the canonical SelfModel and fails closed on an unhealthy initial
    projection/reduction instead of falling back to self-reported success.
    """

    from colony_sidecar.cognition.evidence_pipeline import (
        CognitionEvidenceReducer,
        CognitionEvidenceStore,
        cognition_evidence_mode,
    )
    from colony_sidecar.projects.event_outbox import ProjectEventProjector

    mode = cognition_evidence_mode()
    set_cognition_evidence(None, None, None, {
        "configured_mode": mode,
        "state": "attaching" if project_store is not None else "off",
        "reason": (
            "attachment_in_progress" if project_store is not None
            else "project_store_unavailable"
        ),
    })
    if project_store is None:
        if mode == "live":
            raise RuntimeError(
                "live cognition evidence requires the canonical ProjectStore"
            )
        return None
    if mode == "live" and self_model is None:
        raise RuntimeError(
            "live cognition evidence requires the canonical SelfModel"
        )
    if mode == "live" and scheduler is None:
        raise RuntimeError(
            "live cognition evidence requires the autonomy scheduler"
        )

    projector = ProjectEventProjector(project_store)
    evidence_store = None
    reducer = None
    try:
        initial_outbox = projector.run_once(limit=100)
        if mode == "live" and (
            initial_outbox.get("failed")
            or initial_outbox.get("acknowledgement_failures")
            or initial_outbox.get("outbox", {}).get("last_error")
        ):
            raise RuntimeError(
                "live cognition evidence project outbox is unhealthy"
            )

        try:
            interval = int(os.environ.get(
                "COLONY_COGNITION_EVIDENCE_INTERVAL_SECONDS", "30",
            ))
        except ValueError as exc:
            raise RuntimeError(
                "COLONY_COGNITION_EVIDENCE_INTERVAL_SECONDS must be an integer"
            ) from exc
        interval = max(5, min(3600, interval))

        evidence_store = CognitionEvidenceStore(
            str(state_dir / "colony-cognition-evidence.db")
        )
        reducer = CognitionEvidenceReducer(
            evidence_store,
            project_store=project_store,
            self_model=self_model,
            expectations=expectations,
            project_event_projector=projector,
        )
        attachment_armed = {"value": False}
        if scheduler is not None:

            def _reduce_cognition_evidence():
                if not attachment_armed["value"]:
                    return {
                        "enabled": mode != "off", "mode": mode,
                        "processed": 0, "error": "attachment_not_armed",
                    }
                return reducer.run_once(limit=100)

            # Register before any live sink can mutate. A registration failure
            # therefore leaves competence, expectations, and the evidence
            # cursor untouched. If a scheduler ticks concurrently during
            # attachment, the captured callback remains inert until armed.
            scheduler.register(
                "cognition_evidence_reduce",
                _reduce_cognition_evidence,
                interval_seconds=interval,
                metadata={
                    "description": (
                        "Checkpoint or reduce receipt-bound project evidence"
                    ),
                    "mode": mode,
                },
            )
        initial_status = reducer.run_once(limit=100)
        if mode == "live" and initial_status.get("error"):
            raise RuntimeError(
                "live cognition evidence initial reduction failed: "
                + str(initial_status["error"])[:300]
            )
        attachment = {
            "configured_mode": mode,
            "state": "attached" if scheduler is not None else "degraded",
            "reason": (
                "evidence_passthrough_cursor_attached_learning_off"
                if mode == "off" and scheduler is not None else
                "evidence_passthrough_cursor_restart_only"
                if mode == "off" else
                "receipt_derived_evidence_attached"
                if scheduler is not None else
                "receipt_derived_evidence_restart_replay_only"
            ),
        }
        set_cognition_evidence(
            evidence_store, reducer, projector, attachment,
        )
        attachment_armed["value"] = True
        return {
            "mode": mode,
            "store": evidence_store,
            "reducer": reducer,
            "projector": projector,
            "initial_status": initial_status,
            "attachment": attachment,
        }
    except Exception:
        if evidence_store is not None:
            evidence_store.close()
        set_cognition_evidence(None, None, None, {
            "configured_mode": mode,
            "state": "failed",
            "reason": "cognition_evidence_attachment_failed",
        })
        raise


def _compose_p7_charter_admission(
    original_validator, charter_store, directive_manager=None,
):
    """Narrow P3 admission to the active owner-ratified P7 charter.

    P7 lifecycle activation is already bound to the canonical approval
    authority. This adapter consumes that durable fact; it never creates a
    second approval path and never treats charter prose as executable policy.
    """

    from colony_sidecar.cognition.drive_governance import (
        CharterAdmissionConstraintsV1,
        ScopeV1,
    )

    def _parts(result):
        if isinstance(result, dict):
            return (
                result.get("allowed") is True,
                str(result.get("reason") or "charter_validator_denied")[:500],
                [str(ref) for ref in result.get("evidence_refs") or ()],
            )
        if isinstance(result, (tuple, list)):
            return (
                bool(result and result[0] is True),
                str(result[1] if len(result) > 1 else "charter_validator_denied")[:500],
                [str(ref) for ref in (result[2] if len(result) > 2 else ())],
            )
        return result is True, "charter_validator_denied", []

    def _validator(proposal, concern):
        try:
            base = original_validator(proposal, concern)
        except Exception:
            return {
                "allowed": False,
                "reason": "base_charter_validator_failed",
                "evidence_refs": [],
            }
        allowed, _reason, evidence = _parts(base)
        if not allowed:
            return base
        try:
            active = charter_store.active_revision("default")
        except Exception:
            return {
                "allowed": False,
                "reason": "active_charter_read_failed",
                "evidence_refs": list(dict.fromkeys(evidence)),
            }
        if active is None:
            return {
                "allowed": False,
                "reason": "active_owner_ratified_charter_required",
                "evidence_refs": list(dict.fromkeys(evidence)),
            }
        try:
            proposal_scope = ScopeV1(
                proposal.subject_person_id,
                proposal.viewer_scope,
                proposal.shareability,
            )
        except Exception:
            return {
                "allowed": False,
                "reason": "goal_scope_invalid_for_active_charter",
                "evidence_refs": list(dict.fromkeys(evidence)),
            }
        charter_evidence = [
            active.revision_id,
            f"charter-active:{active.revision_id}",
            *active.evidence_refs,
        ]
        combined = list(dict.fromkeys([*charter_evidence, *evidence]))[:30]
        if not active.scope.permits_child(proposal_scope):
            return {
                "allowed": False,
                "reason": "active_charter_scope_holds_goal",
                "evidence_refs": combined,
            }
        constraints = (
            active.admission_constraints
            if getattr(active, "admission_constraints", None) is not None
            else CharterAdmissionConstraintsV1()
        )
        required_boundaries = set(constraints.required_boundary_refs)
        if required_boundaries:
            if directive_manager is None:
                return {
                    "allowed": False,
                    "reason": "charter_boundary_reader_unavailable",
                    "evidence_refs": combined,
                }
            try:
                active_boundaries = {
                    str(item.id) for item in directive_manager.active()
                }
            except Exception:
                return {
                    "allowed": False,
                    "reason": "charter_boundary_read_failed",
                    "evidence_refs": combined,
                }
            if not required_boundaries.issubset(active_boundaries):
                return {
                    "allowed": False,
                    "reason": "charter_required_boundary_missing",
                    "evidence_refs": combined,
                }
        objective = (
            f"{getattr(proposal, 'title', '')} "
            f"{getattr(proposal, 'objective', '')}"
        ).casefold()
        if any(term in objective for term in constraints.objective_deny_terms):
            return {
                "allowed": False,
                "reason": "charter_objective_explicitly_denied",
                "evidence_refs": combined,
            }
        if constraints.objective_allow_terms and not any(
            term in objective for term in constraints.objective_allow_terms
        ):
            return {
                "allowed": False,
                "reason": "charter_objective_not_explicitly_allowed",
                "evidence_refs": combined,
            }
        destructive_terms = {
            "delete", "destroy", "drop", "format", "overwrite", "wipe",
        }
        if (
            any(term in objective for term in destructive_terms)
            and not constraints.allow_destructive
        ):
            return {
                "allowed": False,
                "reason": "charter_destructive_objective_not_allowed",
                "evidence_refs": combined,
            }
        requested = set(getattr(proposal, "required_capabilities", ()) or ())
        denied = sorted(requested.intersection(constraints.capability_deny))
        if denied:
            return {
                "allowed": False,
                "reason": "charter_capability_explicitly_denied:" + ",".join(denied),
                "evidence_refs": combined,
            }
        if not requested.issubset(set(constraints.capability_ceiling)):
            return {
                "allowed": False,
                "reason": "charter_capability_ceiling_exceeded",
                "evidence_refs": combined,
            }
        if "root:shell" in requested and not constraints.allow_root_shell:
            return {
                "allowed": False,
                "reason": "charter_root_shell_not_allowed",
                "evidence_refs": combined,
            }
        if "messaging:send" in requested:
            if not constraints.allow_messaging:
                return {
                    "allowed": False,
                    "reason": "charter_messaging_not_allowed",
                    "evidence_refs": combined,
                }
            # GoalProposalV1 deliberately has no recipient field. Until a
            # digest-bound recipient envelope exists, even an explicit
            # charter allowance cannot infer one from the concern/model.
            return {
                "allowed": False,
                "reason": "charter_recipient_envelope_required",
                "evidence_refs": combined,
            }
        if proposal.shareability not in set(constraints.allowed_shareability):
            return {
                "allowed": False,
                "reason": "charter_shareability_not_allowed",
                "evidence_refs": combined,
            }
        return {
            "allowed": True,
            "reason": "active_owner_ratified_charter",
            "evidence_refs": combined,
        }

    return _validator


def _attach_drive_governance(
    *,
    state_dir: Path,
    cognition_spine,
    workspace,
    project_store,
    directive_manager,
    approval_authority=None,
):
    """Attach P7 only to the complete, durable P3 dependency graph."""

    from colony_sidecar.cognition.drive_governance import (
        DriveGovernance,
        DriveGovernanceStore,
        DriveRanker,
        drive_governance_mode,
    )
    from colony_sidecar.initiatives.approval_authority import (
        ApprovalAuthorityStore,
    )

    set_drive_governance(None, None, None)
    mode = drive_governance_mode()
    if mode == "off":
        return None

    required = {
        "cognition_spine": cognition_spine,
        "workspace": workspace,
        "project_store": project_store,
        "directive_manager": directive_manager,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    cognition_store = getattr(cognition_spine, "store", None)
    project_engine = getattr(cognition_spine, "project_engine", None)
    if cognition_store is None:
        missing.append("cognition_store")
    if project_engine is None:
        missing.append("project_engine")
    elif getattr(project_engine, "store", None) is not project_store:
        missing.append("shared_project_store")
    if missing:
        raise RuntimeError(
            "P7 drive governance missing durable dependencies: "
            + ", ".join(sorted(set(missing)))
        )
    resolver = getattr(cognition_store, "get_policy_decision", None)
    if not callable(resolver):
        raise RuntimeError(
            "P7 drive governance requires the P3 policy decision resolver"
        )

    store = DriveGovernanceStore(state_dir / "cognition-drive-governance.db")
    try:
        shared_authority = approval_authority
        if mode in {"bootstrap", "live"} and shared_authority is None:
            shared_authority = ApprovalAuthorityStore(
                state_dir / "approval_authority.db"
            )
        governance = DriveGovernance(store, shared_authority, mode=mode)
        ranker = DriveRanker(
            store,
            policy_decision_resolver=resolver,
            directive_manager=directive_manager,
        )
    except Exception:
        store.close()
        raise
    original_charter_validator = getattr(cognition_spine, "_charter", None)
    if mode == "live":
        if not callable(original_charter_validator):
            store.close()
            raise RuntimeError(
                "P7 live governance requires P3's charter validator"
            )
        cognition_spine._charter = _compose_p7_charter_admission(
            original_charter_validator, store, directive_manager,
        )
    runtime_bindings = getattr(cognition_spine, "_runtime_bindings", None)
    if isinstance(runtime_bindings, dict):
        runtime_bindings["charter_store"] = store
    workspace.drive_governance = governance
    workspace.drive_ranker = ranker
    set_drive_governance(governance, ranker, project_store)
    return {
        "mode": mode,
        "store": store,
        "governance": governance,
        "ranker": ranker,
        "approval_authority": shared_authority,
        "original_charter_validator": original_charter_validator,
    }


def _initialize_controlled_learning(
    *,
    state_dir: Path,
    adaptive_params,
    journal=None,
) -> dict:
    """Build the one P4 evidence/experiment graph for this process.

    The migration flags retain their historical meanings: P4 mode defaults to
    ``off`` (legacy weekly evaluation), while the independent benchmark and
    experiment feature flags control whether their databases are opened at
    all.  Setters are cleared before construction so a second lifespan in the
    same interpreter cannot retain stale authority or a stale writer.
    """

    from colony_sidecar.api.routers.host import (
        set_benchmark,
        set_experiments,
        set_learning_feedback_store,
    )
    from colony_sidecar.initiatives.approval_authority import (
        ApprovalAuthorityStore,
    )
    from colony_sidecar.intelligence.learning.feedback_store import (
        FeedbackStore,
    )
    from colony_sidecar.self_model.benchmark import (
        BenchmarkStore,
        SelfhoodBenchmark,
        benchmark_enabled,
    )
    from colony_sidecar.self_model.experiments import (
        ExperimentEngine,
        ExperimentStore,
        experiment_pregrants_from_env,
        experiments_enabled,
    )

    set_learning_feedback_store(None)
    set_benchmark(None)
    set_experiments(None)

    correction_store = FeedbackStore(
        db_path=str(state_dir / "colony-learning-feedback.db"))

    benchmark = None
    if benchmark_enabled():
        benchmark = SelfhoodBenchmark(
            BenchmarkStore(db_path=str(state_dir / "colony-benchmark.db")),
            corrections=correction_store,
        )

    experiments = None
    approval_authority = None
    if experiments_enabled():
        if adaptive_params is None:
            raise RuntimeError(
                "P4 experiments require the adaptive parameter store")
        if benchmark is None:
            raise RuntimeError(
                "P4 experiments require the canonical SelfhoodBenchmark")
        # This is the same approval_authority.db used by the queue approval
        # routes.  Do not create an experiment-specific authority ledger.
        approval_authority = ApprovalAuthorityStore()
        experiments = ExperimentEngine(
            ExperimentStore(
                db_path=str(state_dir / "colony-experiments.db")),
            params=adaptive_params,
            benchmark=benchmark,
            journal=journal,
            approval_authority=approval_authority,
            pregranted_ranges=experiment_pregrants_from_env(),
        )

    # Publish only after the complete configured graph constructed.  A bad
    # pregrant or missing dependency must not expose a partially wired P4.
    set_learning_feedback_store(correction_store)
    set_benchmark(benchmark)
    set_experiments(experiments)

    return {
        "corrections": correction_store,
        "benchmark": benchmark,
        "experiments": experiments,
        "approval_authority": approval_authority,
    }


def _wire_controlled_learning_pipeline(
    cognition_pipeline,
    controlled_learning: dict,
) -> None:
    """Attach P4's detector adapter and correction reader to shared objects."""

    if cognition_pipeline is None:
        return
    correction_store = controlled_learning.get("corrections")
    if correction_store is not None:
        cognition_pipeline.meta_learner.set_feedback_store(correction_store)
    experiments = controlled_learning.get("experiments")
    if experiments is not None:
        # StrategyAdjuster is proposal-only; ExperimentEngine remains the sole
        # adaptive-parameter writer and the only component that can start.
        cognition_pipeline.strategy_adjuster.set_experiment_proposer(
            experiments)


def _scheduler_health_check(autonomy_loop) -> dict:
    """Periodic health_check task body (module-level so it is testable).

    Reports the actual wiring state instead of an unconditional
    ``{"status": "ok"}``. No blanket try/except: an exception here must
    surface as a scheduler failure receipt, never be swallowed into green.
    """
    import colony_sidecar.api.routers.host as _h
    wired = 0
    for _n in ("_commitment_store", "_goals_store", "_affect_store",
               "_contacts_store", "_delivery_bridge", "_workspace",
               "_metalearner"):
        if getattr(_h, _n, None) is not None:
            wired += 1
    return {"status": "ok" if wired else "degraded",
            "subsystems_wired": wired,
            "autonomy_running": bool(getattr(autonomy_loop, "_running", False))}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize subsystems on startup, tear down on shutdown."""
    from colony_sidecar.initiatives.approval_authority import (
        GRANT_MAX_TTL_ENV,
        GRANT_MAX_USES_ENV,
        GRANT_UNLIMITED_SENTINEL,
        resolve_grant_envelope,
    )

    try:
        grant_envelope = resolve_grant_envelope()
    except RuntimeError as exc:
        logger.error(
            "INVALID APPROVAL GRANT ENVELOPE — refusing startup: %s", exc,
        )
        raise
    app.state.grant_envelope = grant_envelope
    if grant_envelope.standing_dimensions:
        descriptions = []
        if grant_envelope.max_ttl_seconds is None:
            descriptions.append(f"{GRANT_MAX_TTL_ENV} (no expiry)")
        if grant_envelope.max_uses is None:
            descriptions.append(f"{GRANT_MAX_USES_ENV} (no use cap)")
        logger.warning(
            "STANDING APPROVAL AUTHORITY ENABLED — %s configured as %r; "
            "exact-scope grants persist in those dimensions until revoked",
            "; ".join(descriptions),
            GRANT_UNLIMITED_SENTINEL,
        )
    else:
        logger.info(
            "Approval grant envelope bounded: %s=%s, %s=%s",
            GRANT_MAX_TTL_ENV,
            grant_envelope.max_ttl_seconds,
            GRANT_MAX_USES_ENV,
            grant_envelope.max_uses,
        )
    state_dir = _state_dir()
    _p8_wiring = None
    # Clear stale process authority before any queue/worker can be constructed.
    # A repeated lifespan that later fails remains fail-closed in live mode.
    set_worker_governor(None)

    # --- Phase C external text/system evidence intake ---
    _external_event_intake = None
    set_external_event_intake(None)
    try:
        from colony_sidecar.cognition.external_events import (
            ExternalEventInboxStore,
            ExternalEventIntake,
        )
        _external_event_intake = ExternalEventIntake(
            ExternalEventInboxStore(
                str(state_dir / "external-cognition-events.db")
            )
        )
        set_external_event_intake(_external_event_intake)
        logger.info(
            "External cognition event intake initialized (text/system only; db=%s)",
            state_dir / "external-cognition-events.db",
        )
    except Exception as exc:
        logger.error("External cognition event intake failed: %s", exc)

    # --- 0. Adaptive parameters (meta-learning read-back path) ---
    # Created first so downstream consumers (consolidator, graph recall,
    # cognition pipeline) can take a handle; the ActionJournal is attached
    # in the self-model section once it exists.
    _adaptive_params = None
    try:
        from colony_sidecar.self_model.params import (
            AdaptiveParamStore, register_core_params,
        )
        _adaptive_params = AdaptiveParamStore(
            db_path=str(state_dir / "colony-params.db"))
        register_core_params(_adaptive_params)
        try:
            from colony_sidecar.api.routers.host import set_adaptive_params
            set_adaptive_params(_adaptive_params)
        except ImportError:
            pass
        logger.info("AdaptiveParamStore initialized (db=%s)",
                    state_dir / "colony-params.db")
    except Exception as exc:
        logger.warning("AdaptiveParamStore init failed: %s", exc)

    # --- 1. LLM Router ---
    llm_router = None
    try:
        from colony_sidecar.router.router import LLMRouter
        from colony_sidecar.router.tiers import build_tiers_from_host
        import json as _json

        config_path = state_dir / ".colony-llm-config.json"
        if config_path.exists():
            try:
                host_llm_config = _json.loads(config_path.read_text())
                tiers = build_tiers_from_host(host_llm_config)
                llm_router = LLMRouter(tiers=tiers)
                logger.info(
                    "LLMRouter initialized from persisted host config (provider=%s)",
                    host_llm_config.get("provider", "unknown"),
                )
            except Exception as cfg_exc:
                logger.warning("Failed to load persisted LLM config, using defaults: %s", cfg_exc)
                llm_router = LLMRouter()
                logger.info("LLMRouter initialized with default tiers")
        else:
            llm_router = LLMRouter()
            logger.info("LLMRouter initialized with default tiers (no host config yet)")
    except Exception as exc:
        logger.warning("LLMRouter init failed — reasoning will not be available: %s", exc)

    if llm_router is not None:
        set_llm_router(llm_router)

    # --- 2. Reasoning loop ---
    if llm_router is not None:
        try:
            from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor
            tool_executor = ToolExecutor()
            reasoning_loop = ReasoningLoop(model=llm_router, tools=tool_executor)
            set_reasoning_loop(reasoning_loop)
            # Native tools will be registered after search orchestrator is wired
            set_tool_executor(tool_executor)
            logger.info("ReasoningLoop initialized")
        except Exception as exc:
            logger.warning("ReasoningLoop init failed: %s", exc)

    # --- 3. Neo4j Graph memory ---
    graph = None
    try:
        from colony_sidecar.intelligence.graph.client import ColonyGraph, GraphConfig
        from pydantic import SecretStr
        neo4j_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        neo4j_user = os.environ.get("NEO4J_USER", "neo4j")
        neo4j_pass = os.environ.get("NEO4J_PASSWORD", "")
        # Neo4j Community Edition only has the "neo4j" database.
        # Enterprise users can override via NEO4J_DATABASE.
        neo4j_db = os.environ.get("NEO4J_DATABASE", "neo4j")
        graph_config = GraphConfig(
            uri=neo4j_uri,
            auth=(neo4j_user, SecretStr(neo4j_pass)) if neo4j_pass else None,
            database=neo4j_db,
        )
        graph = ColonyGraph(graph_config)
        # Apply graph schema constraints/indexes before any queries run
        try:
            from colony_sidecar.intelligence.graph.migrations import run_migrations
            await run_migrations(graph.driver, database=neo4j_db)
        except Exception as exc:
            logger.warning("Graph migrations failed (queries may be degraded): %s", exc)
        set_graph(graph)
        # Wire graph into ToolExecutor for capability-gap detection
        try:
            import colony_sidecar.api.routers.host as _host_router
            te = _host_router._tool_executor
            if te is not None:
                te._graph = graph
        except Exception:
            logger.warning("ToolExecutor graph wiring failed (capability-gap detection degraded)")
        logger.info("ColonyGraph initialized (uri=%s db=%s)", neo4j_uri, neo4j_db)

        # Ensure Colony self-representation in graph (v0.11.0)
        try:
            await graph.ensure_colony_self()
        except Exception as self_exc:
            logger.warning("Colony self-representation setup skipped: %s", self_exc)

        # Wire consolidator (adaptive merge threshold when params wired)
        try:
            from colony_sidecar.intelligence.graph.consolidator import MemoryConsolidator
            consolidator = MemoryConsolidator(graph, params=_adaptive_params)
            set_consolidator(consolidator)
            logger.info("MemoryConsolidator initialized")
        except Exception as cexc:
            logger.warning("MemoryConsolidator init skipped: %s", cexc)
        if _adaptive_params is not None:
            try:
                graph.set_adaptive_params(_adaptive_params)
            except Exception:
                logger.debug("graph adaptive-params wiring failed", exc_info=True)
    except Exception as exc:
        logger.warning("ColonyGraph init failed — memory endpoints will be degraded: %s", exc)

    # --- 4. Response Gate (safety pipeline) ---
    _gate_ref = None
    _gate_config = None
    _gate_audit = None
    try:
        from colony_sidecar.gate import ResponseGate, GateConfig
        from colony_sidecar.gate.audit import InMemoryAuditLog
        # L7 send-delay (the cancel window) is env-tunable; default 0 = no
        # hold. Set COLONY_GATE_SEND_DELAY_SECS>0 to enable a real cancel
        # window on the request-path gate.
        try:
            _gate_delay = float(os.environ.get("COLONY_GATE_SEND_DELAY_SECS", "0"))
        except ValueError:
            _gate_delay = 0.0
        gate_config = GateConfig(send_delay_seconds=_gate_delay)
        gate_audit = InMemoryAuditLog()
        gate = ResponseGate(gate_config, session_store=None, audit_log=gate_audit)
        set_response_gate(gate)
        # Stash refs for re-wiring after session store is available
        _gate_ref = gate
        _gate_config = gate_config
        _gate_audit = gate_audit
        logger.info("ResponseGate initialized (send_delay=%.1fs, secondary_review=%s)",
                    _gate_delay, getattr(gate_config, "enable_secondary_review", False))

        # Re-wire ResponseGate with session store once available
    except Exception as exc:
        logger.warning("ResponseGate init failed — safety checks will pass-through: %s", exc)

    # --- 5. Signal Collector ---
    signal_collector = None
    if graph is not None:
        try:
            from colony_sidecar.intelligence.mind_model.graph_baseline import GraphBaselineStore
            from colony_sidecar.intelligence.mind_model.signal_collector import SignalCollector
            baseline_store = GraphBaselineStore(graph)
            signal_collector = SignalCollector(baseline_store=baseline_store, graph=graph)
            set_signal_collector(signal_collector)
            logger.info("SignalCollector initialized (GraphBaselineStore backed by Neo4j)")
        except Exception as exc:
            logger.warning("SignalCollector init failed: %s", exc)
    else:
        logger.warning("SignalCollector skipped — ColonyGraph not available")

    # --- 6. Embedding pipeline ---
    embed_provider = os.environ.get("COLONY_EMBED_PROVIDER", "")
    embed_model = os.environ.get("COLONY_EMBED_MODEL", "")
    embed_dims = os.environ.get("COLONY_EMBED_DIMS", "")
    reranker_model = os.environ.get("COLONY_RERANKER_MODEL", "")

    # Auto-detect tier if not explicitly configured
    if not embed_provider or not embed_model:
        try:
            from colony_sidecar.vector.scanner import scan
            from colony_sidecar.vector.tiers import get_tier_by_memory
            hw = scan()
            tier = get_tier_by_memory(hw.vram_gb, hw.ram_gb)
            spec = tier.text_embedder
            if spec:
                if hw.gpu_type == "cuda":
                    embed_provider = embed_provider or "cuda"
                elif hw.gpu_type == "mlx":
                    # Prefer native MLX when the package is available
                    try:
                        import mlx_embeddings  # noqa: F401
                        embed_provider = embed_provider or "native_mlx"
                    except ImportError:
                        embed_provider = embed_provider or "mlx"
                else:
                    embed_provider = embed_provider or "cpu"
                embed_model = embed_model or spec.model_id
                embed_dims = embed_dims or str(spec.dims)
                reranker_model = reranker_model or (tier.text_reranker.model_id if tier.text_reranker else "")
                logger.info(
                    "Auto-detected embedding tier: %s (GPU=%s %dGB, RAM=%dGB) -> %s",
                    tier.label, hw.gpu_name, hw.vram_gb, hw.ram_gb, spec.model_id,
                )
        except Exception as exc:
            logger.warning("Hardware scan failed, using defaults: %s", exc)
            embed_provider = embed_provider or "cpu"
            embed_model = embed_model or "sentence-transformers/all-MiniLM-L6-v2"
            embed_dims = embed_dims or "384"

    try:
        from colony_sidecar.vector.embedder import EmbeddingPipeline
        from colony_sidecar.vector.config import EmbeddingConfig
        embed_config = EmbeddingConfig(
            provider=embed_provider,
            model_id=embed_model,
            dimensions=int(embed_dims) if embed_dims else 384,
        )
        from colony_sidecar.vector.embedder import make_provider
        provider = make_provider(embed_config)
        if provider is not None and embed_provider == "openai_api" and hasattr(provider, "configure"):
            # openai_api needs explicit endpoint config (the text-only path
            # never called configure(); only the multimodal branch passed these).
            provider.configure(
                os.environ.get("COLONY_EMBED_BASE_URL", ""),
                os.environ.get("COLONY_EMBED_API_KEY", ""),
            )
        if provider is None:
            # 'skip' provider — embeddings disabled entirely
            logger.info("EmbeddingPipeline skipped (provider=skip) — embeddings disabled")
        else:
            pipeline = EmbeddingPipeline(provider)

            # Wire up multimodal if enabled
            multimodal_enabled = os.environ.get("COLONY_MULTIMODAL", "false").lower() == "true"
            if multimodal_enabled:
                try:
                    from colony_sidecar.vector.multimodal_provider import make_multimodal_provider
                    from colony_sidecar.vector.image_store import make_image_store

                    mm_config = EmbeddingConfig(
                        provider=embed_provider,
                        model_id=embed_model,
                        dimensions=int(embed_dims) if embed_dims else 1024,
                        base_url=os.environ.get("COLONY_EMBED_BASE_URL"),
                        api_key=os.environ.get("COLONY_EMBED_API_KEY"),
                    )
                    mm_provider = make_multimodal_provider(mm_config)
                    img_store = make_image_store(
                        mode=os.environ.get("COLONY_IMAGE_STORAGE", "local"),
                        state_dir=os.environ.get("COLONY_STATE_DIR", "."),
                    )
                    pipeline = EmbeddingPipeline(
                        provider=make_provider(embed_config),
                        multimodal_provider=mm_provider,
                        image_store=img_store,
                    )
                    logger.info("Multimodal enabled (model=%s, storage=%s)", embed_model, os.environ.get("COLONY_IMAGE_STORAGE", "local"))
                except Exception as exc:
                    logger.warning("Multimodal init failed, falling back to text-only: %s", exc)

            await pipeline.warmup()
            set_embedder(pipeline)
            logger.info("EmbeddingPipeline initialized (provider=%s model=%s)", embed_provider, embed_model)

            # Wire embedding pipeline into ColonyGraph for vector-backed recall
            try:
                graph.set_embed_fn(pipeline.embed)
                from colony_sidecar.vector.store import VectorStore
                vector_db_path = os.path.join(state_dir, "lancedb")
                vs = VectorStore(data_dir=vector_db_path)
                embed_dims = int(os.environ.get("COLONY_EMBED_DIMS", pipeline.dimensions or 384))
                await vs.connect(dimensions=embed_dims)
                await vs.ensure_collections(dimensions=embed_dims)
                graph.set_vector_store(vs)
                logger.info("ColonyGraph wired to vector store (path=%s)", vector_db_path)

                if graph._embed_fn and graph._vector_store:
                    logger.info("ColonyGraph fully operational (Neo4j + embeddings + vector store)")
                else:
                    logger.warning("ColonyGraph partially wired — memory may be degraded")
            except Exception as vexc:
                logger.warning("Vector store wiring failed (recall will use keyword fallback): %s", vexc)

            # Pass LLM config to pipeline for auto-captioning
            llm_config_path = Path(os.environ.get("COLONY_STATE_DIR", ".")) / ".colony-llm-config.json"
            if llm_config_path.exists() and hasattr(pipeline, "set_llm_config"):
                try:
                    llm_cfg = _json.loads(llm_config_path.read_text())
                    pipeline.set_llm_config(llm_cfg)
                    logger.info("LLM config passed to EmbeddingPipeline for auto-captioning")
                except Exception as exc:
                    logger.debug("Could not pass LLM config to pipeline: %s", exc)

            # Health check + model mismatch detection
            try:
                hc = await pipeline.health_check()
                if hc.get("status") != "ok":
                    logger.warning("Embedder health check failed: %s", hc.get("error", "unknown"))
                else:
                    logger.info("Embedder health check passed (latency=%.1fms)", hc.get("latency_ms", 0))
            except Exception as exc:
                logger.warning("Embedder health check exception: %s", exc)
    except Exception as exc:
        logger.warning("EmbeddingPipeline init failed: %s", exc)

    # --- 6b. Reranker pipeline ---
    reranker_provider_name = os.environ.get("COLONY_RERANKER_PROVIDER", "")
    if reranker_model and reranker_model.lower() not in ("none", "", "null"):
        try:
            from colony_sidecar.vector.reranker import (
                OpenAIAPIRerankerProvider,
                NativeMLXRerankerProvider,
                MLXRerankerProvider,
                CPURerankerProvider,
                CUDARerankerProvider,
            )
            reranker_base_url = os.environ.get("COLONY_RERANKER_BASE_URL", "")
            reranker_api_key = os.environ.get("COLONY_RERANKER_API_KEY", "")
            if reranker_provider_name == "openai_api" or reranker_base_url:
                # Remote reranker over an OpenAI/Jina-compatible /v1/rerank
                # endpoint, mirroring the embedder's openai_api path so the
                # model stays off-box instead of loading in-process.
                # COLONY_RERANKER_PROMPT_STYLE=qwen3 applies the Qwen3-Reranker
                # instruction template, without which its scores are noise.
                reranker_provider = OpenAIAPIRerankerProvider(reranker_model)
                reranker_provider.configure(
                    reranker_base_url,
                    reranker_api_key,
                    os.environ.get("COLONY_RERANKER_PROMPT_STYLE", ""),
                )
            else:
                from colony_sidecar.vector.scanner import scan
                hw = scan()
                if hw.gpu_type == "mlx":
                    # Prefer native MLX when the package is available
                    try:
                        import mlx_lm  # noqa: F401
                        reranker_provider = NativeMLXRerankerProvider(reranker_model)
                    except ImportError:
                        reranker_provider = MLXRerankerProvider(reranker_model)
                elif hw.gpu_type == "cuda":
                    reranker_provider = CUDARerankerProvider(reranker_model)
                else:
                    reranker_provider = CPURerankerProvider(reranker_model)
            await reranker_provider.warmup()
            set_reranker(reranker_provider)
            logger.info(
                "Reranker initialized (provider=%s model=%s)",
                reranker_provider_name or "local", reranker_model,
            )
            # Wire the reranker into ColonyGraph recall (mirrors the
            # set_embed_fn wiring above). Registration alone changes
            # nothing: use is gated by COLONY_RECALL_RERANK (default off).
            if graph is not None and hasattr(graph, "set_rerank_fn"):
                from colony_sidecar.intelligence.graph.recall import provider_calibration_metadata
                def recall_calibration_metadata():
                    return provider_calibration_metadata(reranker_provider)
                graph.set_rerank_fn(
                    reranker_provider.rerank,
                    calibration_metadata=recall_calibration_metadata)
                logger.info(
                    "ColonyGraph wired to reranker for recall "
                    "(gated by COLONY_RECALL_RERANK)")
        except Exception as exc:
            logger.warning("Reranker init failed: %s", exc)
    else:
        logger.info("No reranker configured for this tier")

    # --- 7. Goals engine ---
    goals_engine = None
    try:
        from colony_sidecar.goals.engine import GoalEngine
        from colony_sidecar.goals.store import GoalStore
        goals_db = os.path.join(state_dir, "colony-goals.db")
        goals_store = GoalStore(db_path=goals_db)
        goals_engine = GoalEngine(store=goals_store)
        set_goals_engine(goals_engine)
        logger.info("GoalEngine initialized (db=%s)", goals_db)
    except Exception as exc:
        logger.warning("GoalEngine init failed: %s", exc)

    # --- 7b. Commitment Store ---
    try:
        from colony_sidecar.commitments.store import CommitmentStore

        commitments_db = state_dir / "colony-commitments.db"
        commitment_store = CommitmentStore(db_path=commitments_db)
        commitment_readiness = (
            commitment_store.resolution_recovery_readiness()
        )
        if commitment_readiness.get("ready") is not True:
            raise RuntimeError(
                "CommitmentStore resolution recovery is unavailable"
            )

        # Resolving a workspace concern raised from a commitment settles the
        # commitment itself — without this, the ingest loop re-raises the
        # concern from the still-open commitment and the resolve is cosmetic.
        from colony_sidecar.self_model.settlement import register_settler

        def _settle_commitment(source_id, *, outcome="done", note="",
                               resolved_by="owner", operation_id=None,
                               _cs=commitment_store):
            row = _cs.resolve(source_id, outcome=outcome, note=note,
                              resolved_by=resolved_by,
                              operation_id=operation_id)
            if not row:
                return None
            operation = (
                _cs.get_resolution_operation(source_id)
                if operation_id is not None else None
            )
            resolution = operation or (
                (row.get("metadata") or {}).get("resolution") or {}
            )
            return {
                "kind": "commitment",
                "status": row["status"],
                "operation_id": resolution.get("operation_id"),
                "outcome": resolution.get("outcome"),
                "note_digest": resolution.get("note_digest"),
                "resolved_by": (
                    resolution.get("resolved_by")
                    if operation is not None else resolution.get("by")
                ),
            }

        register_settler("commitment", _settle_commitment, retry_safe=True)
        set_commitment_store(commitment_store)
        logger.info(
            "CommitmentStore initialized (db=%s, capability=%s)",
            commitments_db, commitment_readiness["capability"],
        )
    except Exception as exc:
        set_commitment_store(None)
        logger.error("CommitmentStore initialization failed")
        raise RuntimeError("CommitmentStore initialization failed") from exc

    # --- 7c. Theory of Mind ---
    try:
        from colony_sidecar.tom.affect import AffectStore
        from colony_sidecar.tom.facts import SharedFactsStore

        affect_db = state_dir / "colony-affect.db"
        affect_store = AffectStore(db_path=affect_db)
        set_affect_store(affect_store)
        logger.info("AffectStore initialized (db=%s)", affect_db)

        facts_db = state_dir / "colony-facts.db"
        facts_store = SharedFactsStore(db_path=facts_db)
        set_facts_store(facts_store)
        logger.info("SharedFactsStore initialized (db=%s)", facts_db)

        try:
            _p8_wiring = _attach_p8_runtime(
                state_dir=state_dir, facts_store=facts_store, graph=graph)
            if _p8_wiring is not None:
                logger.info(
                    "P8 visibility/arcs/recipient audit attached (shadow only)")
        except Exception:
            # P8 is an advisory shadow integration.  Its persistence must not
            # take down the canonical SharedFactsStore or the rest of ToM.
            _p8_wiring = None
            logger.warning(
                "P8 shadow attachment failed; continuing with P8 off",
                exc_info=True,
            )

        # Second-order theory of mind (tom2): refs-not-content inference
        # store + daily asymmetry engine. Inert unless COLONY_TOM2 is set
        # (default off; shadow = counts only).
        from colony_sidecar.tom.tom2 import Tom2Store
        from colony_sidecar.tom.asymmetry import AsymmetryEngine, tom2_mode
        from colony_sidecar.api.routers.host import set_tom2_store, set_tom2_engine
        tom2_db = state_dir / "colony-tom2.db"
        tom2_store = Tom2Store(db_path=str(tom2_db))
        set_tom2_store(tom2_store)
        set_tom2_engine(AsymmetryEngine(facts_store, tom2_store))
        logger.info("Tom2Store + AsymmetryEngine initialized (db=%s, mode=%s)",
                    tom2_db, tom2_mode())

        # Level-2 exposure ledger (L2.3): refs-only record of any future
        # level-2 rendering + its budgets. Empty and inert until the leveled
        # rendering path is wired; the owner endpoint reads it regardless.
        from colony_sidecar.tom.exposure import Tom2ExposureStore
        from colony_sidecar.api.routers.host import set_tom2_exposure_store
        tom2_exposure_db = state_dir / "colony-tom2-exposure.db"
        set_tom2_exposure_store(Tom2ExposureStore(db_path=str(tom2_exposure_db)))
        logger.info("Tom2ExposureStore initialized (db=%s)", tom2_exposure_db)

        # Conversation presence registry (L1.1): passive census of WHO was
        # seen in WHICH conversation, fed from the turns/sync attribution
        # chokepoint. Read by the environment-risk classifier; recording is
        # gated by COLONY_CONV_PRESENCE (default on).
        from colony_sidecar.channels.presence import ConversationPresenceStore
        from colony_sidecar.api.routers.host import set_presence_store
        presence_db = state_dir / "colony-presence.db"
        presence_store = ConversationPresenceStore(db_path=str(presence_db))
        set_presence_store(presence_store)
        logger.info("ConversationPresenceStore initialized (db=%s)", presence_db)

        from colony_sidecar.gate.context_provenance import (
            ContextProvenanceStore, ProvenanceCrossContextGuard)
        provenance_db = state_dir / "colony-context-provenance.db"
        provenance_store = ContextProvenanceStore(db_path=str(provenance_db))
        set_context_provenance_store(provenance_store)
        logger.info("ContextProvenanceStore initialized (db=%s)", provenance_db)

        # Outbound response gate (opt-in; shadow by default). Applicability is
        # resolved by the static, deployment-neutral surface policy: guarded
        # text/artifacts and excluded real-time speech. Gateway labels grant
        # no bypass authority.
        from colony_sidecar.gate.response_guard import ResponseGuard, GuardMode
        from colony_sidecar.gate.surface_policy import (
            POLICY_DIGEST as _guard_policy_digest,
            POLICY_ID as _guard_policy_id,
        )
        from colony_sidecar.world_model.extraction.conversation_extractor import (
            ConversationExtractor)
        _guard_mode = _resolve_guard_mode(os.environ.get("COLONY_GUARD_MODE"))
        from colony_sidecar.gate.guard_audit import GuardAuditStore
        guard_audit_db = state_dir / "colony-guard-audit.db"
        guard_audit_store = GuardAuditStore(db_path=str(guard_audit_db))
        # Injection-taint registry (L3.1) + tom2 epistemic egress net (L3.2).
        # The check is INERT (zero findings) until the leveled tom2 wiring
        # registers a taint, which is why it may sit on the default enforce
        # allowlist from day one.
        from colony_sidecar.gate.taint import TaintRegistry
        from colony_sidecar.gate.layers.tom2_epistemic import Tom2EpistemicGuard
        from colony_sidecar.api.routers.host import set_taint_registry
        taint_db = state_dir / "colony-tom2-taint.db"
        taint_registry = TaintRegistry(db_path=str(taint_db))
        set_taint_registry(taint_registry)
        _guard = ResponseGuard(
            cross_context=ProvenanceCrossContextGuard(provenance_store, extractor=ConversationExtractor()),
            default_mode=_guard_mode, audit_store=guard_audit_store,
            tom2_epistemic=Tom2EpistemicGuard(taint_registry, facts_store=facts_store))
        set_response_guard(_guard)
        logger.info(
            "ResponseGuard initialized (mode=%s, surface_policy=%s:%s, "
            "audit=%s, taints=%s)",
            _guard_mode.value, _guard_policy_id, _guard_policy_digest,
            guard_audit_db, taint_db,
        )
        # Verdict rows prove evaluation, not that an egress mediator actually
        # withheld or revised the exact bytes.  Keep Tom2 capped at level 1
        # until a digest-bound applied-output receipt exists.
        from colony_sidecar.tom.levels import set_evidence_probe
        set_evidence_probe(None)

        from colony_sidecar.tom.engagement import EngagementStore
        engagement_db = state_dir / "colony-engagement.db"
        engagement_store = EngagementStore(db_path=engagement_db)
        set_engagement_store(engagement_store)
        logger.info("EngagementStore initialized (db=%s)", engagement_db)

        from colony_sidecar.contacts.comms import CommsLog
        comms_log = CommsLog(db_path=state_dir / "colony-comms.db")
        set_comms_log(comms_log)
        logger.info("CommsLog initialized")

        # Owner preference learner — captures the owner's *explicit* directives
        # about how to communicate ("be concise", "use bullets", "no emoji") at
        # high confidence; complements the inferred per-contact EngagementStore.
        from colony_sidecar.intelligence.components.preference_learner import PreferenceLearner
        from colony_sidecar.self_model.perspective import SelfPerspective
        from colony_sidecar.turns import get_turn_idempotency_ledger
        from colony_sidecar.identity import get_owner_contact_id
        preference_learner = PreferenceLearner(db_path=str(state_dir / "colony-preferences.db"),
            perspective=SelfPerspective(get_turn_idempotency_ledger(state_dir), owner_id=get_owner_contact_id()))
        set_preference_learner(preference_learner)
        logger.info("PreferenceLearner initialized (db=%s)", state_dir / "colony-preferences.db")
    except Exception as exc:
        try:
            from colony_sidecar.tom.levels import set_evidence_probe
            set_evidence_probe(None)
        except Exception:
            logger.debug("Tom2 evidence probe init cleanup failed", exc_info=True)
        logger.warning("Theory of Mind init failed: %s", exc)

    # --- Directive / boundary memory (safety foundation) ---
    # Durable store of the owner's standing directives (MUST NOT / MUST) with an
    # enforcement guard consulted before autonomous actions. Boundaries must be
    # available before any action-taking, so this is wired unconditionally.
    try:
        from colony_sidecar.directives import DirectiveManager, DirectiveStore
        from colony_sidecar.api.routers.host import set_directive_manager
        _directive_store = DirectiveStore(db_path=str(state_dir / "colony-directives.db"))
        _directive_manager = DirectiveManager(_directive_store)
        set_directive_manager(_directive_manager)
        if locals().get("tool_executor") is not None:
            tool_executor.configure_execution_policy(
                directive_manager=_directive_manager,
                boundary_required=True,
            )
        logger.info(
            "DirectiveManager initialized (db=%s, active=%d)",
            state_dir / "colony-directives.db", _directive_store.count_active(),
        )
    except Exception as exc:
        if locals().get("tool_executor") is not None:
            # A configured server may keep public information tools usable,
            # but private reads/mutations cannot silently lose their boundary.
            tool_executor.configure_execution_policy(
                directive_manager=None,
                boundary_required=True,
            )
        logger.warning("DirectiveManager init failed (boundaries disabled): %s", exc)

    # --- Proposal store (self-directed thinking + research -> proposals) ---
    try:
        from colony_sidecar.proposals import ProposalStore
        from colony_sidecar.api.routers.host import set_proposal_store
        _proposal_store = ProposalStore(db_path=str(state_dir / "colony-proposals.db"))
        set_proposal_store(_proposal_store)
        logger.info("ProposalStore initialized (db=%s)", state_dir / "colony-proposals.db")
    except Exception as exc:
        logger.warning("ProposalStore init failed: %s", exc)

    # --- Type feedback store (outcome-driven priority decay/boost) ---
    try:
        from colony_sidecar.feedback import TypeFeedbackStore
        from colony_sidecar.api.routers.host import set_feedback_store
        set_feedback_store(TypeFeedbackStore(db_path=str(state_dir / "colony-feedback.db")))
        logger.info("TypeFeedbackStore initialized (db=%s)", state_dir / "colony-feedback.db")
    except Exception as exc:
        logger.warning("TypeFeedbackStore init failed: %s", exc)

    # --- Self-model / trust engine + action journal (item 4, Amendment 1) ---
    # Wired before directed action so approval tiering can consult trust.
    _sm_for_directed = None
    try:
        from colony_sidecar.self_model import (
            ActionJournal, CompetenceStore, SelfModel, TrustEngine,
            self_model_enabled,
        )
        from colony_sidecar.api.routers.host import (
            set_self_model, _feedback_store as _fb_for_trust,
        )
        if self_model_enabled():
            from colony_sidecar.autonomy.registry import SubsystemRegistry as _Reg
            _competence = CompetenceStore(
                db_path=str(state_dir / "colony-self-model.db"))
            _journal = ActionJournal(
                db_path=str(state_dir / "colony-action-journal.db"))
            _trust = TrustEngine(
                _competence, db_path=str(state_dir / "colony-self-model.db"),
                feedback_store=_fb_for_trust, journal=_journal)
            _sm_for_directed = SelfModel(_competence, registry=_Reg(),
                                         trust=_trust, journal=_journal)
            _sm_for_directed.perspective = getattr(locals().get('preference_learner'), 'perspective', None)
            set_self_model(_sm_for_directed)
            if _adaptive_params is not None:
                _adaptive_params.set_journal(_journal)
            logger.info(
                "SelfModel/TrustEngine initialized (db=%s, journal=%s, "
                "autograduate=%s)",
                state_dir / "colony-self-model.db",
                state_dir / "colony-action-journal.db",
                os.environ.get("COLONY_TRUST_AUTOGRADUATE", "true"))
        else:
            logger.info("SelfModel disabled (COLONY_SELF_MODEL_ENABLED=false)")
    except Exception as exc:
        logger.warning("SelfModel init failed: %s", exc)

    # --- P4 controlled learning: one evidence and authority graph ---
    _controlled_learning = {
        "corrections": None,
        "benchmark": None,
        "experiments": None,
        "approval_authority": None,
    }
    try:
        _controlled_learning = _initialize_controlled_learning(
            state_dir=state_dir,
            adaptive_params=_adaptive_params,
            journal=locals().get("_journal"),
        )
        if _controlled_learning["benchmark"] is not None:
            logger.info(
                "Selfhood benchmark ready (db=%s, corrections=%s)",
                state_dir / "colony-benchmark.db",
                state_dir / "colony-learning-feedback.db",
            )
        else:
            logger.info(
                "Selfhood benchmark disabled (COLONY_BENCHMARK_ENABLED=false)")
        if _controlled_learning["experiments"] is not None:
            logger.info(
                "Controlled experiment framework ready (db=%s, shared_authority=%s)",
                state_dir / "colony-experiments.db",
                state_dir / "approval_authority.db",
            )
        else:
            logger.info(
                "Experiment framework disabled "
                "(COLONY_EXPERIMENTS_ENABLED=false)")
    except Exception as exc:
        logger.error("Controlled learning init failed closed: %s", exc)

    # --- Toolsmith (Mind M1): self-built, sandbox-verified tools ---
    try:
        from colony_sidecar.toolsmith import (
            Toolsmith, ToolRegistry, toolsmith_enabled,
        )
        from colony_sidecar.api.routers.host import set_toolsmith
        if toolsmith_enabled():
            _tool_registry = ToolRegistry(
                db_path=str(state_dir / "colony-toolsmith.db"),
                library_root=str(state_dir / "toolsmith_library"))
            _toolsmith = Toolsmith(_tool_registry)
            set_toolsmith(_toolsmith)
            # advertise graduated tools to the reasoning loop
            try:
                from colony_sidecar.api.routers.host import _tool_executor
                if _tool_executor is not None:
                    _tool_executor.set_dynamic_provider(
                        _toolsmith.build_dynamic_provider())
            except Exception as texc:
                logger.warning("toolsmith dynamic provider wiring: %s", texc)
            logger.info("Toolsmith ready (mode=%s, db=%s)",
                        os.environ.get("COLONY_TOOLSMITH", "off"),
                        state_dir / "colony-toolsmith.db")
        else:
            logger.info("Toolsmith disabled (COLONY_TOOLSMITH=off)")
    except Exception as exc:
        logger.warning("Toolsmith init failed: %s", exc)

    # --- Expectation engine (Mind M3a): predictions + surprise + calibration ---
    try:
        from colony_sidecar.self_model.expectations import (
            ExpectationEngine, ExpectationStore, expectations_enabled,
            expectations_mode,
        )
        from colony_sidecar.api.routers.host import set_expectations
        if expectations_enabled():
            _exp_store = ExpectationStore(
                db_path=str(state_dir / "colony-expectations.db"))
            _exp_journal = None
            try:
                from colony_sidecar.api.routers.host import _self_model as _sm_e
                _exp_journal = getattr(_sm_e, "journal", None)
            except Exception:
                _exp_journal = None
            # workspace wired later; set_expectations stores the engine and the
            # autonomy phase links the workspace ref at runtime.
            _expectations = ExpectationEngine(_exp_store, journal=_exp_journal)
            set_expectations(_expectations)
            # World-model prediction classes (relationship-still-active,
            # property-unchanged) — registered here, guarded on the engine;
            # the resolvers fetch the world store lazily so boot order and
            # a missing world model are both safe (they resolve to None).
            try:
                from colony_sidecar.world_model.expectation_resolvers import (
                    register_world_resolvers,
                )
                register_world_resolvers(_expectations)
            except Exception as rexc:
                logger.warning("World expectation resolvers not registered: "
                               "%s", rexc)
            logger.info("Expectation engine ready (mode=%s, db=%s)",
                        expectations_mode(),
                        state_dir / "colony-expectations.db")
        else:
            logger.info("Expectation engine disabled (COLONY_EXPECTATIONS=off)")
    except Exception as exc:
        logger.warning("Expectation engine init failed: %s", exc)

    # --- Cognitive workspace (Mind M2): continuity of thought ---
    try:
        from colony_sidecar.self_model.workspace import (
            ConcernStore, WorkspaceEngine, workspace_enabled, workspace_mode,
        )
        from colony_sidecar.self_model.thinker import build_thinker
        from colony_sidecar.self_model.event_concerns import (
            ConversationTurnConcernReducer,
            EventConcernReducer,
            ExternalEventConcernReducer,
            event_concerns_enabled,
            event_concern_mode,
            external_event_concerns_enabled,
            external_event_concern_mode,
            turn_concerns_enabled,
            turn_concern_channels,
            turn_concern_mode,
        )
        from colony_sidecar.api.routers.host import set_workspace
        if workspace_enabled():
            _concern_store = ConcernStore(
                db_path=str(state_dir / "colony-workspace.db"))
            _thinker = (build_thinker(llm_router, graph=graph)
                        if llm_router is not None else None)
            _ws_journal = None
            try:
                from colony_sidecar.api.routers.host import _self_model as _sm_ws
                _ws_journal = getattr(_sm_ws, "journal", None)
            except Exception:
                _ws_journal = None
            _workspace = WorkspaceEngine(
                _concern_store, thinker=_thinker, journal=_ws_journal)
            if event_concerns_enabled():
                _workspace.event_reducer = EventConcernReducer(_concern_store)
                logger.info(
                    "Durable event-to-concern reducer ready (mode=%s, bootstrap=%s)",
                    event_concern_mode(),
                    os.environ.get("COLONY_EVENT_CONCERNS_BOOTSTRAP", "tail"),
                )
            if external_event_concerns_enabled():
                _workspace.external_event_reducer = ExternalEventConcernReducer(
                    _concern_store,
                )
                logger.info(
                    "External event-to-concern reducer ready "
                    "(mode=%s, bootstrap=replay)",
                    external_event_concern_mode(),
                )
            if turn_concerns_enabled():
                _workspace.turn_event_reducer = ConversationTurnConcernReducer(
                    _concern_store,
                )
                logger.info(
                    "Conversation turn-to-concern reducer ready "
                    "(mode=%s, bootstrap=%s, channels=%s)",
                    turn_concern_mode(),
                    os.environ.get("COLONY_TURN_CONCERNS_BOOTSTRAP", "tail"),
                    ",".join(turn_concern_channels()) or "<none>",
                )
            set_workspace(_workspace)
            logger.info("Cognitive workspace ready (mode=%s, db=%s)",
                        workspace_mode(), state_dir / "colony-workspace.db")
        else:
            logger.info("Cognitive workspace disabled (COLONY_WORKSPACE=off)")
            if external_event_concerns_enabled():
                logger.warning(
                    "External event concerns requested but workspace is disabled"
                )
            if turn_concerns_enabled():
                logger.warning(
                    "Conversation turn concerns requested but workspace is disabled"
                )
    except Exception as exc:
        logger.warning("Workspace init failed: %s", exc)

    # --- Skills memory (procedure memory, item 3) ---
    _skills_mem_store = None
    try:
        from colony_sidecar.skills_memory import SkillStore, skills_distill_mode
        from colony_sidecar.api.routers.host import set_skill_store
        _skills_mem_store = SkillStore(
            db_path=str(state_dir / "colony-skills.db"))
        set_skill_store(_skills_mem_store)
        logger.info("SkillStore initialized (db=%s, %d skill(s), distill=%s)",
                    state_dir / "colony-skills.db",
                    _skills_mem_store.count(), skills_distill_mode())
    except Exception as exc:
        logger.warning("SkillStore init failed: %s", exc)

    # --- Mining: escalation miner + verbatim turn capture (corpus source) ---
    try:
        from colony_sidecar.mining import EscalationMiner, MiningStore, mining_mode
        from colony_sidecar.api.routers.mining import set_mining

        if mining_mode() != "off":
            _mining_store_obj = MiningStore(
                db_path=str(state_dir / "colony-mining.db"))

            def _mining_router_getter():
                try:
                    from colony_sidecar.api.routers.host import _reasoning_loop
                    return getattr(_reasoning_loop, "_model", None)
                except Exception:
                    return None

            _mining_engine_obj = EscalationMiner(
                _mining_store_obj,
                skill_store=_skills_mem_store,
                router_getter=_mining_router_getter,
            )
            set_mining(_mining_store_obj, _mining_engine_obj, state_dir)
            logger.info("EscalationMiner initialized (db=%s, mode=%s)",
                        state_dir / "colony-mining.db", mining_mode())
        else:
            logger.info("Mining disabled (COLONY_ESCALATION_MINING=off)")
    except Exception as exc:
        logger.warning("Mining init failed: %s", exc)

    # --- Read-only repo mirrors + directed action (option A) ---
    try:
        from colony_sidecar.repos import RepoMirrorManager
        from colony_sidecar.directed import (
            DirectedActionService, ScopedTaskStore, directed_mode,
        )
        from colony_sidecar.api.routers.host import (
            set_repo_mirrors, set_directed_service,
            get_directive_manager as _get_dm2,
        )
        _mirrors_mgr = RepoMirrorManager(
            mirror_dir=str(state_dir / "repo-mirrors"),
            directive_manager=_get_dm2(),
        )
        set_repo_mirrors(_mirrors_mgr)
        _n_repos = len(_mirrors_mgr.configured())
        if _n_repos:
            # Clone/pull in the background so boot is not blocked by network.
            async def _sync_mirrors():
                try:
                    loop_ = asyncio.get_event_loop()
                    results = await loop_.run_in_executor(None, _mirrors_mgr.refresh_all)
                    logger.info(
                        "Repo mirrors synced: %s",
                        {k: v.get("action") or v.get("reason") for k, v in results.items()},
                    )
                    from colony_sidecar.api.routers.host import _world_store as _ws
                    n = await _mirrors_mgr.register_entities(_ws)
                    if n:
                        logger.info("Registered %d repo(s) as Project entities", n)
                except Exception:
                    logger.debug("mirror sync failed", exc_info=True)
            asyncio.create_task(_sync_mirrors())
        logger.info("RepoMirrorManager initialized (%d repo(s) configured)", _n_repos)

        async def _directed_deliver(payload: dict) -> bool:
            # Late-bound: route through the autonomy loop's guarded delivery
            # (boundary + sanitize + rate + shadow), same as every reach-out.
            try:
                from colony_sidecar.api.routers.host import _autonomy_loop, _delivery_bridge
                if _autonomy_loop is not None and _delivery_bridge is not None:
                    return await _autonomy_loop._route_reachout_delivery(
                        payload, _delivery_bridge)
            except Exception:
                logger.debug("directed deliver failed", exc_info=True)
            return False

        from colony_sidecar.api.routers.host import _feedback_store as _fb_store
        _directed_svc = DirectedActionService(
            store=ScopedTaskStore(db_path=str(state_dir / "colony-directed.db")),
            directive_manager=_get_dm2(),
            mirrors=_mirrors_mgr,
            feedback_store=_fb_store,
            delivery_router=_directed_deliver,
            self_model=_sm_for_directed,
        )
        set_directed_service(_directed_svc)
        logger.info("DirectedActionService initialized (mode=%s)", directed_mode())

        # The once-per-boundary critical flag rides the same guarded delivery.
        _dm_for_flags = _get_dm2()
        if _dm_for_flags is not None:
            _dm_for_flags.set_delivery_router(_directed_deliver)
    except Exception as exc:
        logger.warning("Directed-action init failed: %s", exc)

    # --- Pattern Extraction + Surprise ---
    try:
        from colony_sidecar.patterns.store import PatternStore
        from colony_sidecar.surprise.store import SurpriseStore

        patterns_db = state_dir / "colony-patterns.db"
        pattern_store = PatternStore(db_path=patterns_db)
        set_pattern_store(pattern_store)
        logger.info("PatternStore initialized (db=%s)", patterns_db)

        surprise_db = state_dir / "colony-surprise.db"
        surprise_store = SurpriseStore(db_path=surprise_db)
        set_surprise_store(surprise_store)
        logger.info("SurpriseStore initialized (db=%s)", surprise_db)

        # Close the surprise loop: the condition worker's
        # surprise.accumulation event now lands as a workspace concern
        # (the consumer checks workspace enablement at event time, so this
        # registration is unconditional and a clean no-op when it is off).
        from colony_sidecar.surprise.accumulation import (
            register as register_surprise_consumer,
        )
        register_surprise_consumer()
    except Exception as exc:
        logger.warning("Pattern/Surprise init failed: %s", exc)

    # --- ToM LLM Extractor ---
    try:
        if llm_router is not None:
            from colony_sidecar.tom.extractor import TomExtractor
            tom_extractor = TomExtractor(llm_router)
            set_tom_extractor(tom_extractor)
            logger.info("ToM LLM Extractor initialized (router=%s)", type(llm_router).__name__)
        else:
            logger.info("ToM LLM Extractor skipped — no LLM router")
    except Exception as exc:
        logger.warning("ToM Extractor init failed: %s", exc)

    # --- 7e. Channel Registration Store ---
    channel_store = None
    try:
        from colony_sidecar.channels.store import ChannelStore
        from colony_sidecar.channels.router import set_channel_store
        from colony_sidecar.channels.phone_gateways import set_channel_store_ref

        channels_db = os.path.join(state_dir, "colony-channels.db")
        channel_store = ChannelStore(db_path=channels_db)
        channel_store.connect()
        set_channel_store(channel_store)
        set_channel_store_ref(channel_store)
        from colony_sidecar.api.routers.host import set_channel_store as _host_set_channel_store
        _host_set_channel_store(channel_store)   # turn traffic auto-registers + touches channels
        logger.info("ChannelStore initialized (db=%s)", channels_db)
    except Exception as exc:
        logger.warning("ChannelStore init failed: %s", exc)

    # --- 8. Contacts ---
    contacts_store = None
    try:
        from colony_sidecar.contacts.config import ContactsConfig
        from colony_sidecar.contacts.store import SQLiteContactStore
        contacts_config = ContactsConfig.from_env()
        contacts_store = SQLiteContactStore(config=contacts_config, graph=graph)
        await contacts_store.connect()
        set_contacts_store(contacts_store)
        logger.info("ContactsStore initialized (path=%s)", contacts_config.sqlite_path)
    except Exception as exc:
        logger.warning("ContactsStore init failed: %s", exc)

    # --- 8b. Contact-World Model Bridge ---
    if contacts_store is not None and graph is not None:
        try:
            from colony_sidecar.contacts.world_bridge import WorldModelContactBridge
            bridge = WorldModelContactBridge(graph=graph, store=contacts_store)
            # Backfill all substantive Person nodes on startup
            backfill_stats = await bridge.backfill_all_people()
            logger.info(
                "WorldModelContactBridge initialized — backfill created=%d linked=%d skipped=%d",
                backfill_stats["created"], backfill_stats["linked"], backfill_stats["skipped"],
            )
            # Prune shadow contacts whose Person node no longer exists
            pruned = await bridge.prune_orphaned_shadows()
            if pruned:
                logger.info("Pruned %d orphaned shadow contacts", pruned)
        except Exception as exc:
            logger.warning("WorldModelContactBridge init failed: %s", exc)
    else:
        logger.info("WorldModelContactBridge skipped — contacts_store or graph unavailable")

    # --- 8d. Relationship profiler (standing + psyche + approach briefs) ---
    if contacts_store is not None:
        try:
            from colony_sidecar.intelligence.relationships.profiler import (
                RelationshipProfiler,
            )
            import colony_sidecar.api.routers.host as _host_mod
            _rel_profiler = RelationshipProfiler(
                contacts_store=contacts_store,
                comms_log=_host_mod._comms_log,
                affect_store=_host_mod._affect_store,
                facts_store=_host_mod._facts_store,
                engagement_store=_host_mod._engagement_store,
                p8_runtime=_p8_wiring,
                db_path=str(state_dir / "colony-relationships.db"),
            )
            _host_mod.set_relationship_profiler(_rel_profiler)
            logger.info("RelationshipProfiler initialized (db=%s)",
                        state_dir / "colony-relationships.db")
        except Exception as exc:
            logger.warning("RelationshipProfiler init failed: %s", exc)

    # --- 9. Briefings ---
    try:
        from colony_sidecar.briefings.engine import BriefingEngine
        briefings = BriefingEngine()
        set_briefings_engine(briefings)
        logger.info("BriefingEngine initialized")
    except Exception as exc:
        logger.warning("BriefingEngine init failed: %s", exc)

    # --- 10. World model ---
    world_store = None
    try:
        from colony_sidecar.world_model.store import WorldModelStore
        from colony_sidecar.world_model.config import WorldModelConfig
        _wm_backend = os.environ.get("WORLD_MODEL_BACKEND", "sqlite")
        world_store = WorldModelStore(WorldModelConfig(backend=_wm_backend))
        await world_store.connect()
        set_world_store(world_store)
        logger.info("WorldModelStore initialized and connected (backend=%s)", _wm_backend)

        # World-model population from conversation (shadow-first). Boundary-checked
        # via the directive manager. Mode from COLONY_WORLD_POPULATE_MODE
        # (off|shadow|live, default shadow).
        try:
            from colony_sidecar.world_model.populator import WorldModelPopulator, populate_mode
            from colony_sidecar.api.routers.host import (
                get_directive_manager as _get_dm, set_world_populator,
            )
            _populator = WorldModelPopulator(world_store, directive_manager=_get_dm())
            set_world_populator(_populator)
            logger.info("WorldModelPopulator initialized (mode=%s)", populate_mode())
        except Exception as pexc:
            logger.warning("WorldModelPopulator init failed: %s", pexc)

        # LLM-assisted world-model extraction (batch, journaled; daily phase).
        try:
            from colony_sidecar.world_model.llm_extract import (
                WorldLLMExtractor, llm_extract_mode,
            )
            from colony_sidecar.api.routers.host import (
                get_directive_manager as _get_dm3, set_world_llm_extractor,
            )
            _wle = WorldLLMExtractor(
                world_store, graph=graph, directive_manager=_get_dm3(),
                journal=getattr(_sm_for_directed, "journal", None),
                self_model=_sm_for_directed)
            set_world_llm_extractor(_wle)
            logger.info("WorldLLMExtractor initialized (mode=%s)",
                        llm_extract_mode())
        except Exception as wexc:
            logger.warning("WorldLLMExtractor init failed: %s", wexc)

        # Belief maintenance (item 7): contradiction detection, resolution,
        # stale decay + the inline property-supersession audit hook.
        try:
            from colony_sidecar.beliefs import BeliefEngine, BeliefStore, beliefs_mode
            from colony_sidecar.api.routers.host import set_belief_engine
            from colony_sidecar.world_model.store import set_property_audit_hook
            _belief_store = BeliefStore(
                db_path=str(state_dir / "colony-beliefs.db"))
            _belief_eng = BeliefEngine(
                _belief_store, world_store=world_store, graph=graph,
                initiative_store=None,  # attached below once wired
                journal=getattr(_sm_for_directed, "journal", None),
                self_model=_sm_for_directed)
            set_belief_engine(_belief_eng)
            set_property_audit_hook(_belief_eng.note_property_update)
            logger.info("BeliefEngine initialized (db=%s, mode=%s)",
                        state_dir / "colony-beliefs.db", beliefs_mode())
        except Exception as bexc:
            logger.warning("BeliefEngine init failed: %s", bexc)

        # Wire extraction pipeline
        try:
            from colony_sidecar.world_model.extraction.pipeline import ExtractionPipeline
            from colony_sidecar.world_model.extraction.formats import (
                TextExtractor, JSONExtractor, CSVExtractor,
                PDFExtractor, HTMLExtractor,
            )
            extractors = [TextExtractor(), JSONExtractor(), CSVExtractor()]
            if PDFExtractor:
                extractors.append(PDFExtractor())
            if HTMLExtractor:
                extractors.append(HTMLExtractor())
            llm_extract_fn = None
            if llm_router is not None:
                try:
                    from colony_sidecar.world_model.extraction.llm_extractor import (
                        build_llm_extract_fn,
                    )
                    llm_extract_fn = build_llm_extract_fn(llm_router)
                except Exception as llm_exc:
                    logger.warning("LLM extraction fallback disabled: %s", llm_exc)

            pipeline = ExtractionPipeline(
                extractors=extractors,
                llm_extract_fn=llm_extract_fn,
            )
            set_extraction_pipeline(pipeline)
            logger.info(
                "Extraction pipeline initialized (%d format extractors, llm_fallback=%s)",
                len(extractors),
                "on" if llm_extract_fn is not None else "off",
            )
        except Exception as eexc:
            logger.warning("Extraction pipeline init skipped: %s", eexc)
    except Exception as exc:
        logger.warning("WorldModelStore init failed: %s", exc)
        # Try without connect() — some operations work without it
        try:
            world_store = WorldModelStore(WorldModelConfig(backend=_wm_backend))
            set_world_store(world_store)
            logger.info("WorldModelStore initialized (without connect)")
        except Exception as exc2:
            logger.error("WorldModelStore fallback init also failed: %s", exc2)

    # --- 11. Cognition (CognitionPipeline) ---
    cognition_pipeline = None
    try:
        from colony_sidecar.intelligence.cognition.registry import CognitionPipeline
        from colony_sidecar.events.bus import EventBus

        if graph is not None:
            # Create EventBus for real-time metrics
            event_bus = EventBus()

            cognition_pipeline = CognitionPipeline(
                graph=graph,
                event_bus=event_bus,
                params=_adaptive_params,
            )
            _wire_controlled_learning_pipeline(
                cognition_pipeline, _controlled_learning)
            set_metalearner(cognition_pipeline.meta_learner)
            logger.info(
                "CognitionPipeline initialized (controlled proposals=%s, "
                "durable corrections=%s)",
                _controlled_learning.get("experiments") is not None,
                _controlled_learning.get("corrections") is not None,
            )
        else:
            logger.warning("CognitionPipeline skipped — ColonyGraph not available")
    except Exception as exc:
        logger.warning("CognitionPipeline init failed: %s", exc, exc_info=True)

    # --- 12. Research pipeline ---
    try:
        from colony_sidecar.research.search.orchestrator import SearchOrchestrator

        # Wire search orchestrator
        search_orchestrator = SearchOrchestrator()
        search_provider = os.environ.get("COLONY_SEARCH_PROVIDER", "")
        if search_provider == "tavily" and os.environ.get("TAVILY_API_KEY"):
            from colony_sidecar.research.search.tavily import TavilyProvider
            search_orchestrator.add_provider(TavilyProvider(os.environ["TAVILY_API_KEY"]))
            logger.info("Search provider: Tavily")
        elif search_provider == "serpapi" and os.environ.get("SERPAPI_KEY"):
            from colony_sidecar.research.search.serpapi import SerpAPIProvider
            search_orchestrator.add_provider(SerpAPIProvider(os.environ["SERPAPI_KEY"]))
            logger.info("Search provider: SerpAPI")
        elif search_provider == "brave" and os.environ.get("BRAVE_API_KEY"):
            from colony_sidecar.research.search.brave import BraveSearchProvider
            search_orchestrator.add_provider(BraveSearchProvider(os.environ["BRAVE_API_KEY"]))
            logger.info("Search provider: Brave")
        else:
            # Zero-config fallback so web_search works out of the box.
            from colony_sidecar.research.search.duckduckgo import DuckDuckGoProvider
            search_orchestrator.add_provider(DuckDuckGoProvider())
            logger.info("Search provider: DuckDuckGo (default fallback)")

        set_search_orchestrator(search_orchestrator)

        # Register native tools with the ToolExecutor
        try:
            import colony_sidecar.api.routers.host as _host_router
            te = _host_router._tool_executor
        except Exception:
            te = None
        if te is not None:
            sandbox_dir = os.environ.get("COLONY_SANDBOX_DIR", str(state_dir / "sandbox"))
            # Ensure the sandbox directory exists so file_ops don't fail on first call.
            Path(sandbox_dir).mkdir(parents=True, exist_ok=True)
            te.register_native_tools(
                search_orchestrator=search_orchestrator,
                sandbox_dir=sandbox_dir,
            )
            logger.info(
                "Native tools registered (calculate, web_search, file_ops; sandbox=%s)",
                sandbox_dir,
            )

        research = _build_research_pipeline(
            graph=graph, p8_runtime=_p8_wiring)
        set_research_pipeline(research)
        logger.info("ResearchPipeline initialized")
    except Exception as exc:
        logger.warning("ResearchPipeline init failed: %s", exc, exc_info=True)

    # --- 13. Delivery bridge ---
    try:
        from colony_sidecar.delivery.bridge import ProactiveDeliveryBridge
        from colony_sidecar.delivery.channels import ChannelRegistry
        channel_registry = ChannelRegistry.load(contacts_store=contacts_store, channel_store=channel_store)
        delivery = ProactiveDeliveryBridge(channel_registry=channel_registry)
        set_delivery_bridge(delivery)
        logger.info("Delivery bridge initialized")

        # Adaptive daily cap (Amendment 1.6): the trust engine can EARN the
        # per-recipient cap upward with a proven delivery track record.
        try:
            _trust_for_cap = getattr(_sm_for_directed, "trust", None)
            if (_trust_for_cap is not None
                    and getattr(delivery, "_rate_limiter", None) is not None):
                delivery._rate_limiter._cap_provider = _trust_for_cap.delivery_cap
                logger.info("Delivery rate cap wired to trust engine "
                            "(adaptive, max=%s)",
                            os.environ.get("COLONY_TRUST_DELIVERY_CAP_MAX", "6"))
        except Exception:
            logger.debug("adaptive cap wiring failed", exc_info=True)

        # --- 13b. Briefings: full wiring (delivery + persistence + schedule) ---
        # The bare engine from section 9 can compose briefings but has no gateway, no
        # persistent store, and no scheduler -- proactive output never reaches anyone.
        # Rebuild it here, now that the delivery bridge exists: the bridge-backed
        # gateway auto-registers when a home channel is configured, the store persists
        # under COLONY_STATE_DIR, and the scheduler fires daily/weekly briefings and
        # drains pending deliveries. All deployment specifics come from env.
        try:
            from pathlib import Path as _P

            from colony_sidecar.briefings.config import BriefingConfig
            from colony_sidecar.briefings.engine import BriefingEngine
            from colony_sidecar.briefings.scheduler import BriefingScheduler
            from colony_sidecar.briefings.store import BriefingStore

            b_cfg = BriefingConfig()
            b_cfg.daily.time = os.environ.get("COLONY_BRIEFING_DAILY_TIME", b_cfg.daily.time)
            b_cfg.daily.timezone = os.environ.get("COLONY_BRIEFING_TZ", b_cfg.daily.timezone)
            b_cfg.weekly.timezone = b_cfg.daily.timezone
            b_cfg.delivery_gateway = os.environ.get("COLONY_BRIEFING_GATEWAY", "whatsapp")
            b_cfg.lm_enhancement_enabled = (
                os.environ.get("COLONY_BRIEFING_LM_ENHANCE", "0") not in ("0", "false", "no")
            )
            _b_state_dir = os.environ.get("COLONY_STATE_DIR", ".")
            b_store = BriefingStore(db_path=str(_P(_b_state_dir) / "briefings.db"))
            # Real aggregators where the backing subsystem exists — without
            # them the composer silently falls back to stubs and every data
            # section of every briefing is empty. Calendar/anomaly/mind/
            # synthesis still lack concrete aggregators (see docs/KNOWN-GAPS.md).
            _aggs = {}
            try:
                if graph is not None:
                    from colony_sidecar.briefings.aggregators import RelationshipAggregator
                    _aggs["relationship_aggregator"] = RelationshipAggregator(
                        scorer=None, graph=graph)
            except Exception:
                logger.debug("relationship aggregator wiring failed", exc_info=True)
            try:
                if goals_engine is not None:
                    from colony_sidecar.briefings.aggregators import GoalEngineAggregator
                    _aggs["goal_aggregator"] = GoalEngineAggregator(goals_engine)
            except Exception:
                logger.debug("goal aggregator wiring failed", exc_info=True)
            try:
                # Both resolve their subsystems lazily off host globals at
                # call time (the anomaly detector doesn't even exist until
                # the autonomy registry builds it, well after this point).
                from colony_sidecar.briefings.aggregators import (
                    AnomalyDetectorAggregator, DiscovererSynthesisAggregator)
                _aggs["anomaly_aggregator"] = AnomalyDetectorAggregator()
                _aggs["synthesis_aggregator"] = DiscovererSynthesisAggregator()
            except Exception:
                logger.debug("anomaly/synthesis aggregator wiring failed", exc_info=True)
            try:
                # resolves the enabled calendar connector instance(s) — base
                # or per-account — at call time; harmless when none enabled
                from colony_sidecar.briefings.aggregators import ConnectorCalendarAggregator
                _aggs["calendar_aggregator"] = ConnectorCalendarAggregator()
            except Exception:
                logger.debug("calendar aggregator wiring failed", exc_info=True)
            briefings = BriefingEngine(config=b_cfg, store=b_store,
                                       delivery_bridge=delivery, **_aggs)
            set_briefings_engine(briefings)
            if os.environ.get("COLONY_BRIEFINGS_SCHEDULE", "1") not in ("0", "false", "no"):
                b_sched = BriefingScheduler(config=b_cfg, engine=briefings, store=b_store)
                briefings.attach_scheduler(b_sched)
                b_sched.start()
            logger.info(
                "BriefingEngine rewired: gateway=%s daily=%s %s scheduler=on",
                b_cfg.delivery_gateway, b_cfg.daily.time, b_cfg.daily.timezone,
            )
        except Exception as exc:
            logger.warning("Briefing delivery wiring failed: %s", exc)
    except Exception as exc:
        logger.warning("ProactiveDeliveryBridge init failed: %s", exc)

    # --- 14. Synthesis (ConnectionDiscoverer) ---
    try:
        from colony_sidecar.intelligence.synthesis.connection_discoverer import ConnectionDiscoverer
        if graph is not None:
            discoverer = ConnectionDiscoverer(graph_client=graph)
            set_connection_discoverer(discoverer)
            logger.info("ConnectionDiscoverer initialized")
        else:
            logger.warning("ConnectionDiscoverer skipped — ColonyGraph not available")
    except Exception as exc:
        logger.warning("ConnectionDiscoverer init failed: %s", exc)

    # Insight overlay store (tracks dismissed-insight IDs).
    try:
        from colony_sidecar.intelligence.synthesis.insight_store import InsightStore
        insight_store = InsightStore(state_dir / "insights.db")
        set_insight_store(insight_store)
        logger.info("InsightStore initialized")
    except Exception as exc:
        logger.warning("InsightStore init failed: %s", exc)

    # --- 15. Continuous learner ---
    try:
        from colony_sidecar.intelligence.learning.continuous_learner import ContinuousLearner
        learner = ContinuousLearner()
        set_learner(learner)
        logger.info("ContinuousLearner initialized")
    except Exception as exc:
        logger.warning("ContinuousLearner init failed: %s", exc)

    # --- 16. Skills registry + executor ---
    skills_registry = None
    try:
        from colony_sidecar.skills.registry import SkillRegistry
        skills_registry = SkillRegistry()
        set_skills_registry(skills_registry)
        logger.info("SkillRegistry initialized (%d skills)", len(skills_registry.list_skills()))

        try:
            from colony_sidecar.skills.executor import SkillExecutor
            from colony_sidecar.skills.security.guards import CapabilityGuard
            from colony_sidecar.skills.security.scanner import ASTScanner
            skill_executor = SkillExecutor(
                registry=skills_registry,
                guard=CapabilityGuard(),
                scanner=ASTScanner(),
            )
            set_skill_executor(skill_executor)
            logger.info("SkillExecutor initialized")
        except Exception as sexc:
            logger.warning("SkillExecutor init failed: %s", sexc)
    except Exception as exc:
        logger.warning("SkillRegistry init failed: %s", exc)

    # --- 17. Chain / Identity ---
    try:
        from colony_sidecar.chain.identity import (
            get_or_create_colony_id,
            load_genesis_manifest,
            resolve_genesis_manifest_path,
        )
        colony_id = get_or_create_colony_id(state_dir)

        # Load the Genesis manifest: this colony's own if it claimed Genesis,
        # otherwise the bundled network trust anchor.
        genesis_path = resolve_genesis_manifest_path(state_dir)
        if genesis_path is None:
            logger.warning("No Genesis manifest found; trust anchor stays unverified")
        else:
            load_genesis_manifest(genesis_path)

        from colony_sidecar.chain.manager import ChainManager
        chain = ChainManager(
            db_path=state_dir / "chain.db",
            colony_id=colony_id,
        )
        set_chain_manager(chain)
        logger.info("ChainManager initialized (colony_id=%s)", colony_id)

        # Wire local key manager
        try:
            from colony_sidecar.chain.local_keys import LocalKeyManager
            keys_dir = state_dir / "colony-keys"
            key_passphrase = os.environ.get("COLONY_KEY_PASSPHRASE", "")
            passphrase = key_passphrase.encode() if key_passphrase else None

            if (keys_dir / "private.pem").exists():
                key_mgr = LocalKeyManager(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase)
                logger.info("LocalKeyManager loaded (public_key=%s...)", key_mgr.public_key_hex()[:16])
            else:
                key_mgr = LocalKeyManager.generate(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase)
                logger.info("LocalKeyManager generated new keypair for colony %s", colony_id)

            chain._key_manager = key_mgr  # Attach to chain for access
        except Exception as kexc:
            logger.warning("LocalKeyManager init skipped: %s", kexc)

        # Initialize node identity
        try:
            from colony_sidecar.chain.node import get_or_create_node_id, ensure_node_keypair, create_node_certificate
            node_id = get_or_create_node_id(state_dir)
            node_km = ensure_node_keypair(state_dir)
            logger.info("Node identity: %s (public_key=%s...)", node_id, node_km.public_key_hex()[:16])

            # Create node certificate if missing
            cert_path = Path(state_dir) / "node-cert.json"
            if not cert_path.exists():
                create_node_certificate(state_dir, colony_key_manager=key_mgr)
                logger.info("Node certificate created and signed by Colony key")
            else:
                logger.info("Node certificate exists")
        except Exception as nexc:
            logger.warning("Node identity init skipped: %s", nexc)

        # The LOCAL identity anchor (colony_id, node keypair, signed node
        # cert) is supported. The REMOTE multi-agent surface (agent connect,
        # cert-chain verification, block/consensus) is EXPERIMENTAL: no
        # consensus loop runs and the remote handshake is not production
        # verified. See docs/MULTI_AGENT.md.
        logger.info("Chain: local identity anchor ready; remote multi-agent "
                    "+ consensus are EXPERIMENTAL (no consensus loop started)")
    except Exception as exc:
        logger.warning("ChainManager init failed: %s", exc)

    # --- 18. Secrets ---
    try:
        from colony_sidecar.secrets.manager import SecretsManager
        secrets = SecretsManager()
        set_secrets_manager(secrets)
        logger.info("SecretsManager initialized")
    except Exception as exc:
        logger.warning("SecretsManager init failed: %s", exc)

    # --- 19. Session store ---
    try:
        from colony_sidecar.sessions.store import InMemorySessionStore
        session_store = InMemorySessionStore()
        set_session_store(session_store)
        logger.info("InMemorySessionStore initialized")

        # Re-wire ResponseGate now that session store is available
        if _gate_ref is not None:
            from colony_sidecar.gate import ResponseGate
            new_gate = ResponseGate(_gate_config, session_store=session_store, audit_log=_gate_audit)
            set_response_gate(new_gate)
            logger.info("ResponseGate re-wired with SessionStore")
    except Exception as exc:
        logger.warning("SessionStore init failed: %s", exc)

    # --- 20. Task queue ---
    task_queue = None
    try:
        from colony_sidecar.task_queue.queue_manager import TaskQueueManager
        task_queue = await TaskQueueManager.initialize(
            db_path=state_dir / "task_queue.db",
        )
        # The queue facade becomes process-visible below, before the project
        # verifier and ProjectStore are constructed.  Keep persisted
        # cognition WorkOrders fail-closed throughout that startup window;
        # successful project wiring replaces this with the digest-bound
        # callback later.
        _install_cognition_work_order_startup_fence(task_queue)
        task_queue.queue.set_execution_ready(False, "scheduler_starting")
        set_task_queue(task_queue)
        logger.info("TaskQueueManager initialized")
    except Exception as exc:
        logger.warning("TaskQueueManager init failed: %s", exc)

    # The embedded worker starts only after the central governor is installed.
    worker_task = None
    queue_scheduler = None
    queue_scheduler_task = None
    queue_execution_ready = False
    agent_bridge_service = None
    agent_bridge_task = None

    # --- 20c. Multi-Agent System (v0.7.0) ---
    try:
        from colony_sidecar.agents.store import AgentStore, InviteStore
        from colony_sidecar.initiatives.store import InitiativeStore
        from colony_sidecar.initiatives.assignment import AssignmentEngine
        from colony_sidecar.agents.websocket import WebSocketManager

        agent_store = AgentStore(state_dir=state_dir)
        invite_store = InviteStore(state_dir=state_dir)
        set_agent_store(agent_store)
        set_invite_store(invite_store)
        logger.info("AgentStore initialized (state_dir=%s)", state_dir)

        initiative_store = InitiativeStore(state_dir=state_dir)
        set_initiative_store(initiative_store)
        logger.info("InitiativeStore initialized (state_dir=%s)", state_dir)

        # Observation store (v0.16.0) — agent-reported domain snapshots
        try:
            from colony_sidecar.observations.store import ObservationStore
            from colony_sidecar.api.routers.observations import set_observation_store
            observation_store = ObservationStore(state_dir=state_dir)
            set_observation_store(observation_store)
            logger.info("ObservationStore initialized (state_dir=%s)", state_dir)
        except Exception as exc:
            logger.warning("ObservationStore init failed (non-fatal): %s", exc)

        assignment_engine = AssignmentEngine(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )
        set_assignment_engine(assignment_engine)
        logger.info("AssignmentEngine initialized")

        websocket_manager = WebSocketManager(
            agent_store=agent_store,
            initiative_store=initiative_store,
        )
        set_websocket_manager(websocket_manager)
        logger.info("WebSocketManager initialized")
    except Exception as exc:
        logger.warning("Multi-Agent System init failed: %s", exc)

    # --- 21. Autonomy loop ---
    autonomy_config = None
    registry = None
    scheduler = None
    autonomy_loop = None
    try:
        from colony_sidecar.autonomy.loop import AutonomyLoop
        from colony_sidecar.autonomy.config import AutonomyConfig
        from colony_sidecar.autonomy.registry import SubsystemRegistry
        from colony_sidecar.autonomy.scheduler import AutonomyScheduler
        autonomy_config = AutonomyConfig.from_env()

        # H4.2 annunciation: when the loop mode came from the preset (via
        # COLONY_PRESET_LOOP_COUPLING, default on) rather than an explicit
        # COLONY_AUTONOMY_MODE, say so loudly at startup and leave ONE
        # durable journal record the first time a deployment boots coupled —
        # a default flip an operator can always see and always roll back.
        if getattr(autonomy_config, "mode_source", "") == "preset":
            try:
                from colony_sidecar.util.autonomy_preset import preset_name
                _preset = preset_name() or "(unknown)"
            except Exception:
                _preset = "(unknown)"
            logger.warning(
                "Autonomy loop mode %s inherited from COLONY_AUTONOMY_PRESET=%s "
                "via preset-loop coupling (COLONY_PRESET_LOOP_COUPLING=on by "
                "default). Set COLONY_AUTONOMY_MODE explicitly to override, or "
                "COLONY_PRESET_LOOP_COUPLING=off to restore env-only resolution.",
                autonomy_config.mode.value, _preset)
            try:
                from colony_sidecar.api.routers.host import _self_model as _sm_j
                _cj = getattr(_sm_j, "journal", None)
                if _cj is not None and not _cj.recent(
                        limit=1, domain="preset_coupling"):
                    _cj.record(
                        "preset_coupling",
                        f"First coupled boot: loop mode {autonomy_config.mode.value} "
                        f"inherited from preset '{_preset}'",
                        reasoning="COLONY_AUTONOMY_MODE unset; "
                                  "COLONY_PRESET_LOOP_COUPLING on (default). "
                                  "Rollback: set COLONY_AUTONOMY_MODE=reactive "
                                  "or COLONY_PRESET_LOOP_COUPLING=off.",
                        reversibility="reversible", decision="noted",
                        ref="preset-loop-coupling")
            except Exception:
                pass  # annunciation must never block startup

        registry = SubsystemRegistry()

        # Wire scheduler BEFORE the loop so the loop gets a direct reference.
        scheduler = AutonomyScheduler(db_path=str(state_dir / "schedules.db"))
        set_scheduler(scheduler)
        logger.info("AutonomyScheduler initialized")

        autonomy_loop = AutonomyLoop(
            registry=registry,
            config=autonomy_config,
            scheduler=scheduler,
        )
        set_autonomy_loop(autonomy_loop)

        # Register default periodic tasks. Every registered task does REAL
        # work or reports skipped — a no-op lambda returning {"status":"ok"}
        # makes the loop count a subsystem as running when it never does
        # (signal ingest happens inline at the API; briefings are fired by
        # the BriefingScheduler wired in section 13b — neither needs a task
        # here, so neither gets a fake one).
        def _run_health_check():
            return _scheduler_health_check(autonomy_loop)

        scheduler.register("health_check", _run_health_check, interval_seconds=300, metadata={"description": "Subsystem health check (reports wired count)"})

        async def _run_memory_consolidate():
            from colony_sidecar.api.routers.host import _consolidator as c
            if c is None:
                return {"status": "skipped", "reason": "consolidator_not_wired"}
            result = await c.run()
            # ConsolidationResult exposes pairs_merged, not merged_count — the
            # old attr name silently reported merged:0 every run.
            return {"status": "ok", "merged": getattr(result, "pairs_merged", 0)}

        scheduler.register("memory_consolidate", _run_memory_consolidate, interval_seconds=3600, metadata={"description": "Deduplicate and merge near-duplicate memories"})

        async def _run_cpi_track():
            from colony_sidecar.api.routers.host import _metalearner as ml
            if ml is None:
                return {"status": "skipped", "reason": "metalearner_not_wired"}
            cpi = await ml.evaluate()
            return {"status": "ok", "overall": round(float(getattr(cpi, "overall", 0.0)), 4)}

        scheduler.register("cpi_track", _run_cpi_track, interval_seconds=86400, metadata={"description": "Calculate Cognitive Performance Index"})

        async def _run_world_model_prune():
            from colony_sidecar.api.routers.host import _world_store as ws
            if ws is None:
                return {"status": "skipped", "reason": "world_model_not_wired"}
            return await ws.prune()

        scheduler.register("world_model_prune", _run_world_model_prune, interval_seconds=86400, metadata={"description": "Remove stale low-confidence world model entities (config TTL)"})

        async def _run_mining_prune():
            from colony_sidecar.api.routers.mining import get_mining_store
            store = get_mining_store()
            if store is None:
                return {"status": "skipped", "reason": "mining_not_wired"}
            try:
                retention_days = int(os.environ.get(
                    "COLONY_MINING_RETENTION_DAYS", "0"))
                max_turns = int(os.environ.get(
                    "COLONY_MINING_MAX_TURNS", "0"))
            except ValueError:
                retention_days = max_turns = 0
            if retention_days <= 0 and max_turns <= 0:
                # Default posture: the verbatim turn bank is unbounded
                # unless a deployment opts into retention.
                return {"status": "skipped", "reason": "retention_unbounded"}
            return {"status": "ok", **store.prune_turns(
                retention_days=retention_days, max_turns=max_turns)}

        scheduler.register("mining_prune", _run_mining_prune, interval_seconds=86400, metadata={"description": "Prune banked mining turns per COLONY_MINING_RETENTION_DAYS / COLONY_MINING_MAX_TURNS (0 = keep everything)"})

        # Daily pattern extraction (U25): the pattern store fed compute_surprise
        # but only ever filled via the manual /patterns/extract endpoint, so
        # surprise scoring ran against an empty store. Flag-gated off by
        # default; registered only when on so the loop never counts a
        # deliberately-disabled subsystem as running.
        if os.environ.get("COLONY_PATTERNS_SCHEDULE",
                          "off").strip().lower() == "on":
            async def _run_pattern_extract():
                from colony_sidecar.api.routers.host import (
                    _pattern_store as ps, _world_store as ws,
                )
                if ws is None or ps is None:
                    return {"status": "skipped",
                            "reason": "stores_not_wired"}
                from colony_sidecar.patterns.extract import extract_patterns
                return {"status": "ok",
                        **extract_patterns(world_store=ws, pattern_store=ps)}

            scheduler.register("pattern_extract", _run_pattern_extract, interval_seconds=86400, metadata={"description": "Extract world-model patterns into the pattern store (COLONY_PATTERNS_SCHEDULE=on)"})

        async def _run_surprise_ttl():
            from colony_sidecar.api.routers.host import _surprise_store as ss
            if ss is None:
                return {"status": "skipped",
                        "reason": "surprise_store_not_wired"}
            try:
                ttl_days = float(os.environ.get(
                    "COLONY_SURPRISE_TTL_DAYS", "14"))
            except ValueError:
                ttl_days = 14.0
            if ttl_days <= 0:
                return {"status": "skipped", "reason": "ttl_disabled"}
            return {"status": "ok", "auto_resolved": ss.resolve_stale(ttl_days)}

        scheduler.register("surprise_ttl", _run_surprise_ttl, interval_seconds=86400, metadata={"description": "Auto-resolve surprises unaddressed past COLONY_SURPRISE_TTL_DAYS (default 14; 0 disables)"})

        async def _run_digest_flush():
            from colony_sidecar.api.routers.host import _delivery_bridge as bridge
            if bridge is None:
                return {"status": "skipped", "reason": "delivery_bridge_not_wired"}
            header = os.environ.get("COLONY_DIGEST_HEADER", "Daily digest")
            return await bridge.flush_digests_to_gateway(header=header)

        digest_interval = int(os.environ.get("COLONY_DIGEST_INTERVAL_SECONDS", "86400"))
        scheduler.register(
            "digest_flush",
            _run_digest_flush,
            interval_seconds=digest_interval,
            metadata={"description": "Bundle and deliver accumulated DIGEST-channel items"},
        )

        logger.info(
            "AutonomyLoop initialized (tick=%ds, scheduler=%d tasks)",
            autonomy_config.tick_interval_secs,
            len(scheduler.list_schedules()),
        )

        # Auto-start the autonomy loop as a background task
        asyncio.create_task(autonomy_loop.start())
        logger.info("AutonomyLoop auto-start scheduled")
    except Exception as exc:
        logger.warning("AutonomyLoop init failed: %s", exc)

    # Wire SubsystemRegistry into ToolExecutor so Colony-native tools
    # (memory_search, goals, relationships, etc.) are available to the
    # initiative executor's reasoning loop.
    if registry is not None and locals().get("tool_executor") is not None:
        te = locals()["tool_executor"]
        from colony_sidecar.tools.handlers import TOOL_HANDLERS as _colony_handlers
        for _tname, _thandler in _colony_handlers.items():
            if _tname not in te._handlers:
                te._handlers[_tname] = lambda args, h=_thandler, r=registry: h(args, r)
        logger.info(
            "Colony tool handlers wired into ToolExecutor (%d colony tools, %d total)",
            len(_colony_handlers),
            len(te._handlers),
        )

    # --- 22b. Project engine (goal persistence, cognition item 1) ---
    try:
        from colony_sidecar.projects import ProjectEngine, ProjectStore, projects_mode
        from colony_sidecar.work_orders import (
            QueueWorkOrderAdapter,
            load_receipt_verifier_from_env,
        )
        from colony_sidecar.api.routers.host import (
            set_project_engine,
            get_directive_manager as _get_dm_p,
            _directed_service as _dsvc_for_projects,
            _proposal_store as _pstore_for_projects,
            _feedback_store as _fb_for_projects,
        )

        async def _project_deliver(payload: dict) -> bool:
            try:
                from colony_sidecar.api.routers.host import (
                    _autonomy_loop, _delivery_bridge,
                )
                if _autonomy_loop is not None and _delivery_bridge is not None:
                    return await _autonomy_loop._route_reachout_delivery(
                        payload, _delivery_bridge)
            except Exception:
                logger.debug("project deliver failed", exc_info=True)
            return False

        # A failed verifier import/configuration must not leave a project
        # engine from an earlier lifespan reachable in this process.
        set_project_engine(None)
        _receipt_verifier = load_receipt_verifier_from_env()
        _project_store = ProjectStore(db_path=str(state_dir / "colony-projects.db"))
        _project_concern_store = locals().get("_concern_store")

        def _project_hold_reason(project) -> str:
            from colony_sidecar.self_model.event_concerns import (
                project_turn_concern_hold_reason,
            )
            return project_turn_concern_hold_reason(
                project, _project_concern_store,
            )

        _project_engine_obj = ProjectEngine(
            _project_store,
            directive_manager=_get_dm_p(),
            llm_router=llm_router,
            reasoning_loop=locals().get("reasoning_loop"),
            tool_executor=locals().get("tool_executor"),
            directed_service=_dsvc_for_projects,
            proposal_store=_pstore_for_projects,
            feedback_store=_fb_for_projects,
            self_model=_sm_for_directed,
            skill_store=_skills_mem_store,
            delivery_router=_project_deliver,
            initiative_store=locals().get("initiative_store"),
            work_order_adapter=(
                QueueWorkOrderAdapter(
                    task_queue,
                    project_store=_project_store,
                    receipt_verifier=_receipt_verifier,
                )
                if task_queue is not None else None
            ),
            project_hold_reason=_project_hold_reason,
        )
        if task_queue is not None:
            _install_cognition_work_order_runtime_fence(
                task_queue, _project_store, _project_concern_store,
            )
            await task_queue.queue.reconcile_runtime_claim_holds()
        set_project_engine(_project_engine_obj)
        if _receipt_verifier is not None:
            logger.info(
                "External WorkOrder receipt verifier initialized (%s)",
                getattr(_receipt_verifier, "identity", type(_receipt_verifier).__name__),
            )
        logger.info("ProjectEngine initialized (db=%s, mode=%s)",
                    state_dir / "colony-projects.db", projects_mode())
        # Late-attach the initiative store to the belief engine (it is wired
        # after the world-model section where the engine was created).
        try:
            from colony_sidecar.api.routers.host import _belief_engine as _be
            if _be is not None:
                _be._initiatives = locals().get("initiative_store")
        except Exception:
            pass
    except Exception as exc:
        logger.warning("ProjectEngine init failed: %s", exc)

    # --- 22b.1 Receipt-derived cognition evidence (migration-gated) ---
    _evidence_wiring = None
    try:
        _evidence_wiring = _attach_cognition_evidence(
            state_dir=state_dir,
            project_store=locals().get("_project_store"),
            self_model=_sm_for_directed,
            expectations=locals().get("_expectations"),
            scheduler=locals().get("scheduler"),
        )
        if _evidence_wiring is not None:
            logger.info(
                "Cognition evidence attached (mode=%s, db=%s, initial=%s)",
                _evidence_wiring["mode"],
                (
                    state_dir / "colony-cognition-evidence.db"
                    if _evidence_wiring["store"] is not None else "disabled"
                ),
                _evidence_wiring["initial_status"],
            )
    except Exception as exc:
        # A requested live pipeline suppresses the legacy direct competence
        # writer independently, so attachment failure loses no authority by
        # silently falling back to self-reported outcomes.
        logger.error("Cognition evidence attachment failed closed: %s", exc)

    # --- 22b.2 Typed cognition/goal spine (P3, migration-gated) ---
    try:
        _cognition_spine = _attach_cognition_spine(
            state_dir=state_dir,
            task_queue=task_queue,
            workspace=locals().get("_workspace"),
            concern_store=locals().get("_concern_store"),
            project_store=locals().get("_project_store"),
            project_engine=locals().get("_project_engine_obj"),
            directive_manager=(
                _get_dm_p() if "_get_dm_p" in locals() else None
            ),
            llm_router=llm_router,
            embedded_worker_enabled=_configured_embedded_worker_enabled(),
            proposal_store=locals().get("_proposal_store"),
        )
        if _cognition_spine is not None:
            from colony_sidecar.cognition.goal_spine import cognition_spine_mode
            logger.info(
                "Typed cognition spine attached (db=%s, mode=%s)",
                state_dir / "colony-cognition.db",
                cognition_spine_mode(),
            )
    except Exception as exc:
        # In live mode, P3's legacy-writer guards remain active and the
        # workspace loop refuses a missing spine.  The failure is therefore
        # fail-closed for autonomous goal creation without taking the whole
        # sidecar (and daily owner-directed work) offline.
        from colony_sidecar.cognition.goal_spine import cognition_spine_mode
        failed_mode = cognition_spine_mode()
        set_cognition_attachment_status({
            "configured_mode": failed_mode,
            "state": "failed",
            "reason": str(exc)[:500],
            "configured_handler_catalog": (
                ["thought"] if failed_mode in {"shadow", "live"} else []
            ),
            "effective_handler_catalog": [],
        })
        logger.error("Typed cognition spine attachment failed: %s", exc)

    # --- 22b.3 Evidence-derived situation spine (P6, migration-gated) ---
    _situation_wiring = None
    try:
        _situation_wiring = _attach_situation_spine(
            state_dir=state_dir,
            cognition_spine=locals().get("_cognition_spine"),
            scheduler=locals().get("scheduler"),
            task_queue=task_queue,
        )
        if _situation_wiring is not None:
            logger.info(
                "Situation spine attached (db=%s, mode=%s, initial=%s)",
                state_dir / "colony-situation.db",
                _situation_wiring["mode"],
                _situation_wiring["initial_status"],
            )
    except Exception as exc:
        # A configured live P6 that cannot attach must not silently leave P3's
        # capacity-only allow path active. Preserve capacity denials, then hold
        # everything else until a healthy pinned restart.
        try:
            from colony_sidecar.self_model.situation import situation_spine_mode
            cognition = locals().get("_cognition_spine")
            existing = getattr(cognition, "_situation", None)
            if situation_spine_mode() == "live" and callable(existing):
                cognition._situation = _capacity_plus_attachment_failure(
                    existing, "situation_attachment_failed_closed",
                )
        except Exception:
            logger.exception("Could not install P6 attachment failure gate")
        logger.error("Situation spine attachment failed closed: %s", exc)

    # --- 22b.4 Owner-ratified drive governance (P7, migration-gated) ---
    _drive_wiring = None
    try:
        _drive_wiring = _attach_drive_governance(
            state_dir=state_dir,
            cognition_spine=locals().get("_cognition_spine"),
            workspace=locals().get("_workspace"),
            project_store=locals().get("_project_store"),
            directive_manager=(
                _get_dm_p() if "_get_dm_p" in locals() else None
            ),
            approval_authority=_controlled_learning.get(
                "approval_authority"
            ),
        )
        if _drive_wiring is not None:
            logger.info(
                "Drive governance attached (db=%s, mode=%s, shared_authority=%s)",
                state_dir / "cognition-drive-governance.db",
                _drive_wiring["mode"],
                getattr(
                    _drive_wiring["approval_authority"], "path", None,
                ),
            )
    except Exception as exc:
        logger.error("Drive governance attachment failed closed: %s", exc)

    # --- 22c. Worker governor (server-side queue enforcement, item 5) ---
    try:
        from colony_sidecar.api.routers.host import (
            get_directive_manager as _get_dm_w,
            _feedback_store as _fb_for_workers,
        )
        from colony_sidecar.task_queue.governor import WorkerGovernor, workers_mode

        async def _worker_deliver(payload: dict) -> bool:
            try:
                from colony_sidecar.api.routers.host import (
                    _autonomy_loop, _delivery_bridge,
                )
                if _autonomy_loop is not None and _delivery_bridge is not None:
                    return await _autonomy_loop._route_reachout_delivery(
                        payload, _delivery_bridge)
            except Exception:
                logger.debug("worker deliver failed", exc_info=True)
            return False

        _worker_gov = WorkerGovernor(
            directive_manager=_get_dm_w(),
            feedback_store=_fb_for_workers,
            self_model=_sm_for_directed,
            delivery_router=_worker_deliver,
            proposal_store=locals().get("_proposal_store"),
            skill_store=_skills_mem_store,
            llm_router=llm_router,
            boundary_required=True,
        )
        set_worker_governor(_worker_gov)
        logger.info("WorkerGovernor initialized (mode=%s)", workers_mode())
    except Exception as exc:
        logger.warning("WorkerGovernor init failed: %s", exc)

    # Queue maintenance is independent from execution-worker enablement. It
    # expires deadlines and leases, reconciles holds, and drains durable
    # outcome evidence even during a health-only deployment cutover.
    if task_queue is not None:
        try:
            from colony_sidecar.task_queue.scheduler import Scheduler

            queue_scheduler = Scheduler(
                queue=task_queue.queue,
                tick_interval_secs=_queue_seconds_env(
                    "COLONY_QUEUE_SCHEDULER_TICK_SECS", 2.0,
                ),
                claim_timeout_secs=_queue_seconds_env(
                    "COLONY_QUEUE_CLAIM_TIMEOUT_SECS", 30.0,
                ),
                readiness_callback=task_queue.queue.set_execution_ready,
            )
            # A successful synchronous first tick is the execution-readiness
            # gate. Configuration/import/database failures leave the API and
            # read surfaces online but no worker may claim work.
            await queue_scheduler.tick_once()
            queue_scheduler_task = asyncio.create_task(
                queue_scheduler.run()
            )
            app.state.queue_scheduler = queue_scheduler
            app.state.queue_scheduler_task = queue_scheduler_task
            queue_readiness = task_queue.queue.execution_readiness()
            queue_execution_ready = bool(queue_readiness.get("ready"))
            if queue_execution_ready:
                logger.info("Task queue scheduler started")
            else:
                logger.error(
                    "Task queue maintenance started but execution remains "
                    "held: %s",
                    queue_readiness.get("reason"),
                )
        except Exception as exc:
            queue_execution_ready = False
            task_queue.queue.set_execution_ready(
                False, f"scheduler_unavailable:{exc}",
            )
            logger.error(
                "Task queue scheduler unavailable; execution workers disabled: %s",
                exc,
                exc_info=True,
            )

    # --- 22c.0 Agent Bridge (after claim authority + scheduler readiness) ---
    # The bridge can still forward initiatives when queue execution is held,
    # but it receives no queue handle and therefore cannot claim jobs.
    try:
        from colony_sidecar.services.agent_bridge import (
            create_from_env as _create_bridge,
        )
        agent_bridge_service = _create_bridge(
            initiative_store=locals().get("initiative_store"),
            autonomy_loop=autonomy_loop,
            task_queue=(task_queue if queue_execution_ready else None),
            observation_store=locals().get("observation_store"),
        )
        if agent_bridge_service is not None:
            set_agent_bridge(agent_bridge_service)
            agent_bridge_task = asyncio.create_task(
                agent_bridge_service.start()
            )
            logger.info("AgentBridgeService auto-start scheduled")
    except Exception as exc:
        logger.warning("AgentBridgeService init failed (non-fatal): %s", exc)

    # --- 22c.1 Embedded worker (after mandatory central authority wiring) ---
    if task_queue is not None and _embedded_worker_enabled():
        try:
            if not queue_execution_ready:
                raise RuntimeError("queue scheduler is not execution-ready")
            import asyncio as _asyncio
            from colony_sidecar.task_queue.worker import WorkerNode
            from colony_sidecar.task_queue.handlers.registry import build_default_handlers
            from colony_sidecar.chain.node import get_or_create_node_id
            import colony_sidecar.api.routers.host as _host_mod

            worker_node_id = get_or_create_node_id(state_dir)
            from colony_sidecar.cognition.goal_spine import cognition_spine_mode
            worker_profile = _cognition_worker_profile(
                configured_mode=cognition_spine_mode(),
                attached=locals().get("_cognition_spine") is not None,
            )
            if worker_profile == "held":
                raise RuntimeError(
                    "configured cognition owner is held because P3 attachment "
                    "is unavailable"
                )
            if worker_profile == "thought_only":
                handlers, worker_capabilities = _cognition_owner_spec(
                    router=_host_mod._llm_router,
                    node_id=worker_node_id,
                )
            else:
                handlers = build_default_handlers(
                    router=_host_mod._llm_router,
                    world_model_store=_host_mod._world_store,
                    contact_store=_host_mod._contacts_store,
                    response_gate=_host_mod._response_gate,
                    node_id=worker_node_id,
                )
                worker_capabilities = None
            worker = WorkerNode(
                node_id=worker_node_id,
                queue=task_queue.queue,
                handlers=handlers,
                capabilities=worker_capabilities,
            )
            worker_task = _asyncio.create_task(worker.start())
            app.state.worker = worker
            app.state.worker_task = worker_task
            if worker_profile == "thought_only":
                set_cognition_attachment_status({
                    "configured_mode": cognition_spine_mode(),
                    "state": "attached",
                    "reason": "cognition_thought_worker_started",
                    "configured_handler_catalog": ["thought"],
                    "effective_handler_catalog": ["thought"],
                })
            logger.info(
                "WorkerNode started after central governor "
                "(node=%s, handlers=%s, governance=%s)",
                worker_node_id,
                [jt.value for jt in handlers.keys()],
                task_queue.queue.governance_configuration(),
            )
        except Exception as exc:
            logger.warning(
                "WorkerNode init failed — queued jobs will not execute: %s",
                exc,
                exc_info=True,
            )

    # --- 22d. Exploration sandbox (gated isolated execution, item 6) ---
    try:
        from colony_sidecar.sandbox import SandboxManager, sandbox_mode
        from colony_sidecar.api.routers.host import (
            set_sandbox, get_directive_manager as _get_dm_sb,
        )
        _sandbox_mgr = SandboxManager(
            directive_manager=_get_dm_sb(),
            self_model=_sm_for_directed,
        )
        set_sandbox(_sandbox_mgr)
        logger.info("SandboxManager initialized (mode=%s, backend=%s)",
                    sandbox_mode(), _sandbox_mgr.backend_name())
    except Exception as exc:
        logger.warning("SandboxManager init failed: %s", exc)

    # --- 22e. Connector framework (read-only pull senses, item 2) ---
    try:
        from colony_sidecar.connectors import (
            ConnectorManager, connectors_mode,
        )
        from colony_sidecar.api.routers.host import (
            set_connector_manager, get_directive_manager as _get_dm_c,
            _world_populator as _pop_for_conn,
        )
        _conn_mgr = ConnectorManager(
            observation_store=locals().get("observation_store"),
            populator=_pop_for_conn,
            directive_manager=_get_dm_c(),
            self_model=_sm_for_directed,
        )
        n_conn = _conn_mgr.register_default_connectors()
        set_connector_manager(_conn_mgr)
        logger.info("ConnectorManager initialized (mode=%s, %d connector(s) enabled)",
                    connectors_mode(), n_conn)
    except Exception as exc:
        logger.warning("ConnectorManager init failed: %s", exc)

    # --- 23. Initiative Executor service (autonomous initiative processing) ---
    try:
        from colony_sidecar.services.initiative_executor import (
            create_from_env as _create_executor,
        )
        from colony_sidecar.api.routers.host import get_directive_manager as _get_dm
        _executor_svc = _create_executor(
            initiative_store=locals().get("initiative_store"),
            reasoning_loop=locals().get("reasoning_loop"),
            tool_executor=locals().get("tool_executor"),
            directive_manager=_get_dm(),
            skill_store=_skills_mem_store,
            self_model=_sm_for_directed,
        )
        if _executor_svc is not None:
            set_initiative_executor(_executor_svc)
            asyncio.create_task(_executor_svc.start())
            logger.info("InitiativeExecutorService auto-start scheduled")
    except Exception as exc:
        logger.warning("InitiativeExecutorService init failed (non-fatal): %s", exc)

    from colony_sidecar.telemetry import TelemetryStore
    telemetry = TelemetryStore()
    telemetry.load()  # restore last_*_at across restart (v0.21.0)
    telemetry.started_at = datetime.now(timezone.utc)
    app.state.telemetry = telemetry
    set_telemetry(telemetry)
    logger.info("TelemetryStore initialized")

    # Session report store (cross-session context bridge)
    from colony_sidecar.sessions.reports import SessionReportStore
    session_report_store = SessionReportStore()
    set_session_report_store(session_report_store)
    logger.info("SessionReportStore initialized")

    # Register conversation synthesis task (periodic memory scan for goals)
    try:
        if (
            autonomy_config is not None
            and getattr(autonomy_config, "conversation_synthesis_enabled", True)
            and registry is not None
            and scheduler is not None
        ):
            from colony_sidecar.autonomy.synthesis import ConversationSynthesisTask
            _synthesis_task = ConversationSynthesisTask(
                registry=registry,
                lookback_hours=getattr(autonomy_config, "conversation_synthesis_lookback_hours", 2.0),
                min_confidence=getattr(autonomy_config, "conversation_synthesis_min_confidence", 0.35),
                telemetry=telemetry,
            )
            synthesis_interval = int(getattr(autonomy_config, "conversation_synthesis_interval_secs", 1800.0))
            scheduler.register(
                "conversation_synthesis",
                _synthesis_task.run,
                interval_seconds=synthesis_interval,
                metadata={"description": "Scan conversation memories for implicit goals and commitments"},
            )
            logger.info(
                "Conversation synthesis registered (lookback=%.1fh, interval=%ds, min_conf=%.2f)",
                getattr(autonomy_config, "conversation_synthesis_lookback_hours", 2.0),
                synthesis_interval,
                getattr(autonomy_config, "conversation_synthesis_min_confidence", 0.35),
            )
    except Exception as exc:
        logger.warning("Conversation synthesis registration failed: %s", exc)

    logger.info("Sidecar capabilities: %s", supported_capabilities())

    # Dedicated, owner-bound governed action execution.  This ledger is
    # separate from the general API and task queue so a crash after an effect
    # starts can be recovered honestly as ambiguous without ever retrying the
    # mutation.  No live action is enabled merely by constructing the service;
    # the exact scoped keyring principal is still required at the HTTP edge.
    governed_action_service = None
    try:
        import colony_sidecar.api.routers.host as _host_actions
        from colony_sidecar.api.routers.governed_actions import (
            set_governed_action_service,
        )
        from colony_sidecar.governed_actions import (
            ColonySubsystemActionExecutor,
            GovernedActionLedger,
            GovernedActionService,
        )

        async def _autonomy_enable():
            await _host_actions.autonomy_start()

        async def _autonomy_disable():
            await _governed_autonomy_stop_signal(_host_actions._autonomy_loop)

        def _autonomy_running():
            loop = _host_actions._autonomy_loop
            return bool(loop is not None and loop.is_running)

        governed_action_service = GovernedActionService(
            GovernedActionLedger(
                state_dir / "governed-actions" / "ledger.db"
            ),
            ColonySubsystemActionExecutor(
                graph=_host_actions._graph,
                goals=_host_actions._goals_store,
                commitments=_host_actions._commitment_store,
                initiatives=_host_actions._initiative_store,
                projects=_host_actions._project_engine,
                feedback=_host_actions._feedback_store,
                autonomy_enable=_autonomy_enable,
                autonomy_disable=_autonomy_disable,
                autonomy_running=_autonomy_running,
            ),
        )
        set_governed_action_service(governed_action_service)
        logger.info("Governed action ledger initialized")
    except Exception as exc:
        logger.warning("Governed action ledger init failed: %s", exc)
    source_claim_task = None
    if os.environ.get("COLONY_SOURCE_CLAIMS", "on").strip().lower() in {"on", "1", "true"}:
        from colony_sidecar.turns import get_turn_idempotency_ledger
        from colony_sidecar.beliefs.source_projection import run_source_claim_worker
        from colony_sidecar.api.routers import host as source_claim_host
        source_claim_task = asyncio.create_task(run_source_claim_worker(
            get_turn_idempotency_ledger(state_dir), lambda: source_claim_host._llm_router))
    yield

    if source_claim_task is not None:
        source_claim_task.cancel()
        try:
            await source_claim_task
        except asyncio.CancelledError:
            pass

    # Shutdown — close connections
    set_external_event_intake(None)
    if _external_event_intake is not None:
        try:
            _external_event_intake.close()
        except Exception:
            logger.debug(
                "external cognition event intake shutdown failed",
                exc_info=True,
            )
    # Stop the queue-consuming bridge before its stores, governor, or queue.
    try:
        if agent_bridge_service is not None:
            await agent_bridge_service.stop()
        if agent_bridge_task is not None:
            try:
                await asyncio.wait_for(agent_bridge_task, timeout=5.0)
            except asyncio.TimeoutError:
                agent_bridge_task.cancel()
                try:
                    await agent_bridge_task
                except asyncio.CancelledError:
                    pass
    except Exception:
        logger.debug("Agent bridge shutdown error", exc_info=True)
    if graph is not None:
        try:
            await graph.close()
        except Exception:
            logger.debug("Graph close failed", exc_info=True)
    if world_store is not None:
        try:
            await world_store.close()
        except Exception:
            logger.debug("WorldStore close failed", exc_info=True)
    if skills_registry is not None:
        try:
            skills_registry.close()
        except Exception:
            logger.debug("SkillRegistry close failed", exc_info=True)
    set_llm_router(None)
    set_reasoning_loop(None)
    set_graph(None)
    set_response_gate(None)
    set_signal_collector(None)
    set_embedder(None)
    set_goals_engine(None)
    set_contacts_store(None)
    set_briefings_engine(None)
    set_world_store(None)
    set_metalearner(None)
    try:
        from colony_sidecar.api.routers.host import (
            set_adaptive_params as _set_adaptive_params,
            set_benchmark as _set_benchmark,
            set_experiments as _set_experiments,
            set_learning_feedback_store as _set_learning_feedback_store,
        )
        _set_experiments(None)
        _set_benchmark(None)
        _set_learning_feedback_store(None)
        _set_adaptive_params(None)
        if _adaptive_params is not None:
            _adaptive_params.close()
    except Exception:
        logger.debug("controlled learning shutdown failed", exc_info=True)
    set_research_pipeline(None)
    set_delivery_bridge(None)
    set_connection_discoverer(None)
    set_learner(None)
    set_skills_registry(None)
    set_commitment_store(None)
    set_affect_store(None)
    try:
        if _p8_wiring is not None:
            _p8_wiring.close()
    except Exception:
        logger.debug("P8 runtime shutdown failed", exc_info=True)
    set_p8_runtime(None)
    set_facts_store(None)
    try:
        from colony_sidecar.api.routers.host import (
            set_presence_store as _set_presence_store,
            _presence_store as _presence_ref,
        )
        if _presence_ref is not None:
            _presence_ref.close()
        _set_presence_store(None)
    except Exception:
        logger.debug("presence store shutdown failed", exc_info=True)
    set_context_provenance_store(None)
    set_response_guard(None)
    try:
        from colony_sidecar.tom.levels import set_evidence_probe
        set_evidence_probe(None)
    except Exception:
        logger.debug("Tom2 evidence probe shutdown failed", exc_info=True)
    set_pattern_store(None)
    set_surprise_store(None)
    set_tom_extractor(None)
    if channel_store is not None:
        try:
            channel_store.close()
        except Exception:
            logger.debug("ChannelStore close failed", exc_info=True)
    try:
        from colony_sidecar.channels.router import set_channel_store as _set_ch_store
        _set_ch_store(None)
    except Exception:
        pass
    set_chain_manager(None)
    set_secrets_manager(None)
    set_session_store(None)
    set_session_report_store(None)
    set_agent_bridge(None)
    set_initiative_executor(None)
    # Stop worker node (before queue so in-flight jobs can drain).
    try:
        worker = getattr(app.state, "worker", None)
        if worker is not None:
            await worker.stop(drain_timeout=10.0)
        worker_task = getattr(app.state, "worker_task", None)
        if worker_task is not None:
            worker_task.cancel()
    except Exception:
        logger.debug("Worker shutdown error", exc_info=True)
    # Stop queue maintenance only after the execution worker has drained, and
    # before closing the shared queue connection.
    try:
        if queue_scheduler is not None:
            await queue_scheduler.stop()
        if queue_scheduler_task is not None:
            try:
                await asyncio.wait_for(queue_scheduler_task, timeout=5.0)
            except asyncio.TimeoutError:
                queue_scheduler_task.cancel()
                try:
                    await queue_scheduler_task
                except asyncio.CancelledError:
                    pass
    except Exception:
        logger.debug("Task queue scheduler shutdown error", exc_info=True)
    # No stopped/repeated lifespan may retain a usable authority handle.
    set_worker_governor(None)
    # Stop task queue
    try:
        from colony_sidecar.api.routers.host import _task_queue
        if _task_queue is not None:
            await _task_queue.queue.stop()
    except Exception:
        logger.warning("Task queue shutdown failed")
    set_task_queue(None)
    # Stop autonomy loop if running
    try:
        from colony_sidecar.api.routers.host import _autonomy_loop
        if _autonomy_loop is not None and _autonomy_loop.is_running:
            await _autonomy_loop.stop()
    except Exception:
        logger.warning("Autonomy loop shutdown failed")
    set_autonomy_loop(None)
    # Evidence/P6 periodic reducers are owned by the autonomy scheduler. Close
    # them only after that loop is stopped so an in-flight tick cannot race a
    # closed SQLite handle. Clear HTTP handles before releasing connections.
    set_cognition_evidence(None, None, None, {
        "configured_mode": "off",
        "state": "off",
        "reason": "sidecar_stopped",
    })
    try:
        evidence_wiring = locals().get("_evidence_wiring")
        if evidence_wiring is not None and evidence_wiring.get("store") is not None:
            evidence_wiring["store"].close()
    except Exception:
        logger.debug("cognition evidence shutdown failed", exc_info=True)
    set_situation_spine(None, None)
    set_drive_governance(None, None, None)
    try:
        situation_wiring = locals().get("_situation_wiring")
        if situation_wiring is not None:
            original = situation_wiring.get("original_validator")
            cognition = locals().get("_cognition_spine")
            if original is not None and cognition is not None:
                cognition._situation = original
            situation_wiring["store"].close()
    except Exception:
        logger.debug("situation spine shutdown failed", exc_info=True)
    try:
        drive_wiring = locals().get("_drive_wiring")
        if drive_wiring is not None:
            drive_wiring["store"].close()
    except Exception:
        logger.debug("drive governance shutdown failed", exc_info=True)
    try:
        from colony_sidecar.api.routers.host import set_project_engine
        set_project_engine(None)
        project_store = locals().get("_project_store")
        if project_store is not None:
            project_store.close()
    except Exception:
        logger.debug("project store shutdown failed", exc_info=True)
    set_session_store(None)
    set_task_queue(None)
    # Multi-Agent cleanup
    set_agent_store(None)
    set_invite_store(None)
    set_initiative_store(None)
    try:
        from colony_sidecar.api.routers.governed_actions import (
            set_governed_action_service,
        )
        set_governed_action_service(None)
        if governed_action_service is not None:
            governed_action_service.close()
    except Exception:
        logger.debug("Governed action ledger shutdown failed", exc_info=True)
    set_assignment_engine(None)
    set_websocket_manager(None)
    try:
        from colony_sidecar.api.routers.observations import set_observation_store
        set_observation_store(None)
    except Exception:
        pass
    auth_telemetry = getattr(app.state, "auth_telemetry", None)
    if auth_telemetry is not None:
        try:
            auth_telemetry.close()
        except Exception:
            logger.debug("Auth telemetry shutdown failed", exc_info=True)
    logger.info("Sidecar shutdown complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="Colony Intelligence Sidecar",
        version="0.1.0",
        lifespan=lifespan,
    )

    # API authentication (skips health/docs; loopback dev mode if no auth set).
    # The scoped keyring runs alongside COLONY_API_KEY during migration.
    from colony_sidecar.api.auth_telemetry import AuthTelemetry
    from colony_sidecar.api.contact_grants import ContactGrantRegistry
    from colony_sidecar.api.middleware import ApiKeyMiddleware, BodySizeLimitMiddleware

    # Body-size cap runs before auth so oversized payloads are rejected with
    # 413 regardless of the auth state.
    try:
        max_body = int(os.environ.get("COLONY_MAX_BODY_BYTES", "") or 10 * 1024 * 1024)
    except ValueError:
        max_body = 10 * 1024 * 1024
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body)

    api_key = os.environ.get("COLONY_API_KEY")
    keyring_path = os.environ.get("COLONY_API_KEYRING_PATH")
    state_dir = _state_dir()
    telemetry_path = os.environ.get(
        "COLONY_AUTH_TELEMETRY_PATH",
        str(state_dir / "colony-auth-telemetry.db"),
    )
    contact_grants_path = os.environ.get(
        "COLONY_API_CONTACT_GRANTS_PATH",
        str(state_dir / "api-contact-grants.json"),
    )
    auth_telemetry = AuthTelemetry(telemetry_path)
    contact_grants = ContactGrantRegistry(contact_grants_path)
    # HTTP middleware uses request.state; WebSocket/admin status paths use
    # app.state because BaseHTTPMiddleware does not wrap WebSocket frames.
    app.state.auth_telemetry = auth_telemetry
    app.state.contact_grants = contact_grants
    app.add_middleware(
        ApiKeyMiddleware,
        api_key=api_key,
        keyring_path=keyring_path,
        auth_telemetry=auth_telemetry,
        contact_grants=contact_grants,
    )
    if api_key and keyring_path:
        logger.info("Legacy and scoped API authentication enabled (dual-accept)")
    elif keyring_path:
        logger.info("Scoped API authentication enabled")
    elif api_key:
        logger.info("Legacy API key authentication enabled")
    else:
        logger.warning("No API auth configured — API is open (loopback dev mode)")

    app.include_router(host_router)
    app.include_router(host_v2_router)
    from colony_sidecar.api.routers import executions as executions_router
    app.include_router(executions_router.router)

    # Exact PUT/GET action endpoint.  Middleware maps these methods to
    # actions:execute/actions:verify; the router independently rejects legacy,
    # unscoped, non-owner, and differently named principals.
    from colony_sidecar.api.routers import governed_actions as governed_actions_router
    app.include_router(governed_actions_router.router)

    # Channel registration router
    from colony_sidecar.channels.router import router as channels_router
    app.include_router(channels_router)

    # Task queue router (v0.13.0)
    from colony_sidecar.api.routers import task_queue as task_queue_router
    app.include_router(task_queue_router.router)

    # Observations router (v0.16.0) — agent-as-sensor ingestion
    from colony_sidecar.api.routers import observations as observations_router
    app.include_router(observations_router.router)
    from colony_sidecar.api.routers import mining as mining_router
    app.include_router(mining_router.router)

    # Context gate (v0.32.0) — budget-aware context preparation
    from colony_sidecar.api.routers import context_gate as context_gate_router
    app.include_router(context_gate_router.router)

    # MCP streamable HTTP endpoint
    try:
        from colony_sidecar.mcp.server import create_server
        mcp_server = create_server()
        # Mount MCP ASGI app at /mcp
        mcp_asgi = mcp_server.streamable_http_app()
        app.mount("/mcp", mcp_asgi)
        logger.info("MCP endpoint mounted at /mcp (streamable HTTP)")
    except ImportError:
        logger.debug("MCP SDK not installed — /mcp endpoint not available (install colonyai[mcp])")
    except Exception as exc:
        logger.warning("Could not mount MCP endpoint: %s", exc)

    return app


# Uvicorn entry point
app = create_app()
