"""Server-owned routing contracts for the shared ``agent_action`` job type."""

from __future__ import annotations

import os
from typing import Any


AGENT_SYNC_ROUTE = "agent_sync:v1"
ACTION_PLANE_ROUTE = "action_plane:v1"
WORK_ORDER_ROUTE = "work_order:v1"
HERMES_RUN_ROUTE = "hermes_run:v1"
THOUGHT_ROUTE = "thought_engine:v1"

AGENT_ACTION_ROUTE_CAPABILITIES = frozenset({
    AGENT_SYNC_ROUTE,
    ACTION_PLANE_ROUTE,
    WORK_ORDER_ROUTE,
    HERMES_RUN_ROUTE,
})

_ROUTE_OWNER_ENV = {
    AGENT_SYNC_ROUTE: "COLONY_AGENT_SYNC_WORKER_NODE_ID",
    HERMES_RUN_ROUTE: "COLONY_HERMES_RUN_WORKER_NODE_ID",
    ACTION_PLANE_ROUTE: "COLONY_ACTION_PLANE_WORKER_NODE_ID",
    WORK_ORDER_ROUTE: "COLONY_ACTION_PLANE_WORKER_NODE_ID",
}


def generic_agent_job_claims_enabled() -> bool:
    """Resolve the global generic agent-action containment switch."""

    value = os.environ.get(
        "COLONY_AGENT_JOB_CLAIMS_ENABLED", "true"
    ).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    # An invalid containment value never silently enables generic claims.
    return False


def expected_agent_action_routes(job: Any) -> tuple[str, ...]:
    """Derive exact executor lanes from server-known schema/action policy."""

    payload = getattr(job, "payload", {}) or {}
    if payload.get("schema") == "WorkOrderV1":
        from colony_sidecar.work_orders import WorkOrderV1

        order = WorkOrderV1.from_payload(payload)
        if str(getattr(job, "job_id", "")) != order.work_order_id:
            raise ValueError(
                "WorkOrder queue job ID does not match authority digest"
            )
        # WorkOrders are receipt-aware Action Plane work. Requiring both
        # capabilities prevents a generic protocol-aware worker from racing
        # the deployment's authorized effect executor.
        return (WORK_ORDER_ROUTE, ACTION_PLANE_ROUTE)

    action_hint = str(payload.get("action_hint") or "").strip()
    if action_hint:
        from colony_sidecar.initiatives.action_registry import (
            RiskTier,
            get_action,
        )

        spec = get_action(action_hint)
        if spec is not None:
            canonical_risk = spec.risk.value
            declared_risk = str(payload.get("risk") or "").strip().lower()
            if declared_risk and declared_risk != canonical_risk:
                raise ValueError(
                    "agent_action risk does not match server registry"
                )
            if spec.risk is RiskTier.READ_ONLY:
                from colony_sidecar.task_queue.governor import (
                    job_declares_effect,
                )
                if job_declares_effect(job):
                    raise ValueError(
                        "read-only agent action declares effect capabilities"
                    )
                return (AGENT_SYNC_ROUTE,)
            return (ACTION_PLANE_ROUTE,)
        raise ValueError("agent_action action_hint is not server-registered")

    from colony_sidecar.task_queue.governor import job_declares_effect

    if job_declares_effect(job):
        raise ValueError(
            "effectful agent_action has no server-registered executor route"
        )
    if payload.get("schema") == "HermesRunV1":
        return (HERMES_RUN_ROUTE,)
    raise ValueError(
        "agent_action requires WorkOrderV1, HermesRunV1, or a registered action"
    )


def bind_agent_action_routes(
    job: Any,
    *,
    allow_legacy_migration: bool = False,
) -> tuple[str, ...]:
    """Add derived route requirements and reject caller-selected mismatch."""

    expected = expected_agent_action_routes(job)
    payload = dict(getattr(job, "payload", None) or {})
    action_hint = str(payload.get("action_hint") or "").strip()
    if action_hint and payload.get("schema") != "WorkOrderV1":
        from colony_sidecar.initiatives.action_registry import get_action

        spec = get_action(action_hint)
        if spec is not None:
            payload["risk"] = spec.risk.value
            job.payload = payload
    capabilities = list(getattr(job, "capabilities", None) or ())
    names = {
        str(getattr(capability, "name", capability))
        for capability in capabilities
    }
    supplied_routes = names & AGENT_ACTION_ROUTE_CAPABILITIES
    if (
        supplied_routes
        and supplied_routes != set(expected)
        and not (
            allow_legacy_migration
            and payload.get("schema") == "WorkOrderV1"
        )
    ):
        raise ValueError(
            "agent_action route capabilities do not match server policy"
        )

    from colony_sidecar.task_queue.models import JobCapabilityRequirement

    # Caller/legacy metadata can never weaken a route by marking it preferred,
    # adding a nonsensical numeric minimum, or duplicating it.  Replace every
    # route entry with one canonical required requirement.
    if payload.get("schema") == "WorkOrderV1":
        from colony_sidecar.work_orders import WorkOrderV1

        order = WorkOrderV1.from_payload(payload)
        # Compatibility fields consumed by the host are reconstructed from the
        # verified authority object. Unknown executable-looking extras and
        # caller-edited description/context never survive queue admission.
        payload = order.payload()
        job.payload = payload
        capability_names = (
            WORK_ORDER_ROUTE,
            ACTION_PLANE_ROUTE,
            *order.capability_allowlist,
        )
        # The embedded authority allowlist is the complete executor contract;
        # arbitrary/missing queue metadata must not broaden or weaken it.
        capabilities = []
    else:
        capability_names = expected
        capabilities = [
            capability for capability in capabilities
            if str(getattr(capability, "name", capability))
            not in AGENT_ACTION_ROUTE_CAPABILITIES
        ]
    capabilities.extend(
        JobCapabilityRequirement(
            name=name,
            minimum=None,
            preferred=False,
        )
        for name in dict.fromkeys(capability_names)
    )
    job.capabilities = capabilities
    tags = dict(getattr(job, "tags", None) or {})
    tags["agent_action_route"] = "+".join(expected)
    if payload.get("schema") == "WorkOrderV1":
        tags.update({
            "schema": order.schema,
            "project_id": order.project_id,
            "step_id": order.step_id,
            "risk_class": order.risk_class,
            "idempotency_key": order.idempotency_key,
            "work_order_digest": order.work_order_digest,
            "work_order_version": str(order.version),
            "executor_protocol": WORK_ORDER_ROUTE,
            "action_result_contract": "ExecutionResultV1",
        })
    elif expected == (ACTION_PLANE_ROUTE,):
        from colony_sidecar.initiatives.approval_authority import (
            build_action_binding,
        )

        binding = build_action_binding(
            job_id=str(getattr(job, "job_id", "")),
            job_type=str(
                getattr(getattr(job, "job_type", None), "value", "")
                or getattr(job, "job_type", "")
            ),
            payload=payload,
        )
        tags.update({
            "action_digest": binding.action_digest,
            "action_result_contract": "ActionReceiptAttestationV1",
        })
    owners = {
        os.environ.get(_ROUTE_OWNER_ENV[route], "").strip()
        for route in expected
        if os.environ.get(_ROUTE_OWNER_ENV[route], "").strip()
    }
    if len(owners) > 1:
        raise ValueError("agent_action route owners conflict")
    if owners:
        tags["agent_action_route_node"] = next(iter(owners))
    else:
        tags.pop("agent_action_route_node", None)
    job.tags = tags
    return expected


def worker_route_capabilities(*, sync: bool, text: bool) -> set[str]:
    """Build a generic non-effectful worker's explicit route set."""

    routes: set[str] = set()
    if sync:
        routes.add(AGENT_SYNC_ROUTE)
    if text:
        routes.add(HERMES_RUN_ROUTE)
    return routes


def bind_thought_route(job: Any) -> str:
    """Canonicalize the private ThoughtJobV1 worker route."""

    payload = getattr(job, "payload", {}) or {}
    from colony_sidecar.cognition.goal_spine import ThoughtJobV1

    try:
        thought = ThoughtJobV1.from_payload(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid ThoughtJobV1 payload: {exc}") from exc
    if str(getattr(job, "job_id", "")) != thought.thought_job_id:
        raise ValueError("thought queue job ID does not match authority digest")
    from colony_sidecar.task_queue.models import JobCapabilityRequirement

    job.capabilities = [
        JobCapabilityRequirement(name="cognition_scoped"),
        JobCapabilityRequirement(name=THOUGHT_ROUTE),
    ]
    tags = dict(getattr(job, "tags", None) or {})
    tags["thought_route"] = THOUGHT_ROUTE
    tags.update({
        "schema": thought.schema,
        "concern_id": thought.concern_id,
        "viewer_scope": thought.viewer_scope,
        "shareability": thought.shareability,
        "thought_job_digest": thought.thought_job_digest,
        "risk_class": "read_only",
    })
    owner = os.environ.get("COLONY_THOUGHT_WORKER_NODE_ID", "").strip()
    if owner:
        tags["thought_route_node"] = owner
    else:
        tags.pop("thought_route_node", None)
    job.tags = tags
    return THOUGHT_ROUTE


def configured_route_owners() -> dict[str, str | None]:
    """Return deployment-pinned owners by logical executor lane."""

    return {
        "agent_sync": os.environ.get(
            "COLONY_AGENT_SYNC_WORKER_NODE_ID", ""
        ).strip() or None,
        "action_plane": os.environ.get(
            "COLONY_ACTION_PLANE_WORKER_NODE_ID", ""
        ).strip() or None,
        "work_order": os.environ.get(
            "COLONY_ACTION_PLANE_WORKER_NODE_ID", ""
        ).strip() or None,
        "hermes_run": os.environ.get(
            "COLONY_HERMES_RUN_WORKER_NODE_ID", ""
        ).strip() or None,
        "thought": os.environ.get(
            "COLONY_THOUGHT_WORKER_NODE_ID", ""
        ).strip() or None,
    }
