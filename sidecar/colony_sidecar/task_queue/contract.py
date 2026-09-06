"""Authenticated, deploy-pinned worker protocol identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict

from colony_sidecar.task_queue.models import CANONICAL_JOB_ID_PATTERN
from colony_sidecar.task_queue.routing import (
    ACTION_PLANE_ROUTE,
    AGENT_SYNC_ROUTE,
    HERMES_RUN_ROUTE,
    WORK_ORDER_ROUTE,
    THOUGHT_ROUTE,
    configured_route_owners,
)


QUEUE_CONTRACT_SCHEMA = "ColonyQueueContractV1"
QUEUE_CONTRACT_VERSION = 1
AGENT_ACTION_ROUTING_SCHEMA = "AgentActionRoutingV1"
AGENT_ACTION_ROUTING_VERSION = 1
CLAIM_ATTEMPT_PROTOCOL = "ExactClaimAttemptV1"

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class QueueContractIdentityError(RuntimeError):
    """The running artifact has no valid deploy-pinned identity."""


def _release_identity() -> Dict[str, str]:
    commit = os.environ.get("COLONY_RELEASE_COMMIT", "").strip().lower()
    manifest_sha256 = os.environ.get(
        "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256", ""
    ).strip().lower()
    if not _COMMIT_RE.fullmatch(commit) or set(commit) == {"0"}:
        raise QueueContractIdentityError(
            "COLONY_RELEASE_COMMIT must be the exact 40-character lowercase "
            "Git commit deployed"
        )
    if (
        not _SHA256_RE.fullmatch(manifest_sha256)
        or set(manifest_sha256) == {"0"}
    ):
        raise QueueContractIdentityError(
            "COLONY_RELEASE_ARTIFACT_MANIFEST_SHA256 must be the exact "
            "64-character lowercase SHA-256 of the deployed artifact manifest"
        )
    return {
        "commit": commit,
        "artifact_manifest_sha256": manifest_sha256,
    }


def _deployment_posture() -> Dict[str, Any]:
    authority_mode = os.environ.get(
        "COLONY_WORKER_AUTHORITY_MODE", "shadow"
    ).strip().lower()
    if authority_mode not in {"shadow", "enforce"}:
        raise QueueContractIdentityError(
            "COLONY_WORKER_AUTHORITY_MODE must be shadow or enforce"
        )
    raw_claims = os.environ.get(
        "COLONY_AGENT_JOB_CLAIMS_ENABLED", "true"
    ).strip().lower()
    if raw_claims in {"1", "true", "yes", "on"}:
        claims_enabled = True
    elif raw_claims in {"0", "false", "no", "off"}:
        claims_enabled = False
    else:
        raise QueueContractIdentityError(
            "COLONY_AGENT_JOB_CLAIMS_ENABLED must be true or false"
        )
    raw_routes = os.environ.get(
        "COLONY_AGENT_WORKER_ROUTES", "agent_sync,hermes_run"
    )
    generic_routes = sorted({
        item.strip().lower()
        for item in raw_routes.split(",")
        if item.strip()
    })
    if (
        not generic_routes
        or set(generic_routes) - {"agent_sync", "hermes_run"}
    ):
        raise QueueContractIdentityError(
            "COLONY_AGENT_WORKER_ROUTES must select agent_sync and/or "
            "hermes_run"
        )
    raw_embedded = os.environ.get(
        "COLONY_EMBEDDED_WORKER_ENABLED", "true"
    ).strip().lower()
    if raw_embedded in {"1", "true", "yes", "on"}:
        embedded_enabled = True
    elif raw_embedded in {"0", "false", "no", "off"}:
        embedded_enabled = False
    else:
        raise QueueContractIdentityError(
            "COLONY_EMBEDDED_WORKER_ENABLED must be true or false"
        )
    return {
        "worker_authority_mode": authority_mode,
        "generic_agent_job_claims_enabled": claims_enabled,
        "generic_agent_worker_routes": generic_routes,
        "embedded_worker_enabled": embedded_enabled,
    }


def _work_control_contract() -> Dict[str, Any]:
    from colony_sidecar.task_queue.work_control import (
        WorkControlError,
        work_control_ack_timeout_secs,
        work_control_mode,
    )

    try:
        mode = work_control_mode()
        ack_timeout = work_control_ack_timeout_secs()
    except WorkControlError as exc:
        raise QueueContractIdentityError(exc.message) from exc
    return {
        "schema": "WorkControlV1",
        "version": 1,
        "mode": mode,
        "target_kind": "queue_job",
        "work_order_authority_digest_bound": True,
        "generic_job_authority_digest_bound": True,
        "cas_fields": ["revision", "state_digest"],
        "operations": ["steer", "interrupt", "cancel", "retry"],
        "caller_operation_id_required": True,
        "append_only_receipt_schema": "WorkControlReceiptEventV1",
        "worker_ack_authority_persisted": True,
        "slash_safe_operator_endpoints": {
            "inspect": "/v1/host/queue/work?target_id={target_id}",
            "operate": "/v1/host/queue/work/operations",
            "receipt": (
                "/v1/host/queue/work/operations/receipt?"
                "target_id={target_id}&operation_id={operation_id}"
            ),
        },
        "worker_ack_operations": ["steer", "interrupt", "running_cancel"],
        "worker_ack_timeout_seconds": ack_timeout,
        "effectful_started_retry_policy": "ambiguous_forbidden",
        "effect_reconciliation": {
            "schema": "WorkEffectReconciliationV1",
            "findings": ["applied", "not_applied"],
            "exact_attempt_required": True,
            "independent_verifier_required": True,
            "retry_enabled_only_by": "not_applied",
        },
        "steer_idempotency": {
            "durable_worker_outcome": True,
            "handler_operation_id_idempotency_required": True,
            "ack_retried_before_reapply": True,
        },
        "operator_read_scope": "work:read",
        "operator_control_scope": "work:control",
        "worker_scope": "workers:lifecycle",
        "legacy_bearer_control_allowed": False,
        "off_posture": (
            "control mutations, delivery loop, and advertised capabilities "
            "disabled; mode-invariant lifecycle safety corrections retained"
        ),
        "rollback_posture": (
            "off disables WorkControl but never restores unsafe active-cancel "
            "or started-effect automatic-retry behavior"
        ),
        "active_control_requires_explicit_handler_opt_in": True,
        "bundled_production_handler_opt_ins": [],
        "active_control_posture": (
            "inactive_until_host_adapter_registers_exact_handler_semantics"
        ),
        "colony_supplies_host_control_adapter": False,
        "host_adapter_prerequisite": (
            "A host adapter must register a cooperative interrupt-safe and/or "
            "durably idempotent steer handler before active control is claimed"
        ),
    }


def queue_contract_identity() -> Dict[str, Any]:
    """Return one canonical contract whose digest includes release identity."""

    owners = configured_route_owners()
    contract: Dict[str, Any] = {
        "schema": QUEUE_CONTRACT_SCHEMA,
        "version": QUEUE_CONTRACT_VERSION,
        "release": _release_identity(),
        "deployment_posture": _deployment_posture(),
        "claim_attempt": {
            "protocol": CLAIM_ATTEMPT_PROTOCOL,
            "field": "claim_attempt_id",
            "required": True,
            "server_minted": True,
            "start_required_before_execution": True,
            "lifecycle_operations": [
                "start", "heartbeat", "complete", "fail", "release",
            ],
            "worker_heartbeat_exact_map_required": True,
            "missing_attempt_accepted": False,
            "reclaim_mints_new_attempt": True,
            "stale_attempt_callbacks_rejected": True,
            "same_attempt_terminal_replay_idempotent": True,
            "fail_release_replay_stable_before_reclaim": True,
            "claim_expiry_field": "claim_expires_at",
            "claim_expiry_required": True,
            "completion_response_fields": [
                "transitioned", "job_status", "governor_outcome",
            ],
        },
        "agent_action_routing": {
            "schema": AGENT_ACTION_ROUTING_SCHEMA,
            "version": AGENT_ACTION_ROUTING_VERSION,
            "server_derived": True,
            "caller_route_selection_allowed": False,
            "route_tag": "agent_action_route",
            "exact_owner_tag": "agent_action_route_node",
            "routes": {
                "agent_sync": [AGENT_SYNC_ROUTE],
                "action_plane": [ACTION_PLANE_ROUTE],
                "work_order": [WORK_ORDER_ROUTE, ACTION_PLANE_ROUTE],
                "hermes_run": [HERMES_RUN_ROUTE],
            },
            "deployment_owners": owners,
            "exact_owner_enforced": {
                lane: owner is not None for lane, owner in owners.items()
            },
        },
        "work_order": {
            "schema": "WorkOrderV1",
            "version": 1,
            "execution_result_schema": "ExecutionResultV1",
            "execution_result_version": 1,
            "independent_success_attestation_required": True,
        },
        "work_control": _work_control_contract(),
        "action_plane_result": {
            "request_schema": "ActionReceiptAttestationV1",
            "request_version": 1,
            "response_schema": "ActionReceiptAttestationResultV1",
            "response_version": 1,
            "effect_completion_status_before_attestation": "neutral",
            "terminal_status_after_attestation": "completed",
            "attestation_endpoint": (
                "/v1/host/queue/attestations/jobs/{job_id}"
            ),
            "required_scope": "workers:attest",
            "inspection_scope": "workers:inspect",
            "inspection_authority_fields": [
                "action_digest", "action_result_contract",
                "claim_attempt_id", "verification_pending",
                "success_attestation_schema",
            ],
            "request_fields": [
                "schema", "version", "job_id", "action_digest",
                "claim_attempt_id", "effect_class", "terminal_outcome",
                "receipt_refs", "observed_at", "summary",
            ],
            "response_fields": [
                "schema", "version", "success", "job_id",
                "claim_attempt_id", "action_digest", "verifier_identity",
                "replayed", "job_status", "evidence_sha256",
                "receipt_refs_sha256",
            ],
            "action_digest_bound": True,
            "claim_attempt_bound": True,
            "effect_class_server_verified": True,
            "evidence_sha256_server_computed": True,
            "receipt_refs_bounded_and_scheme_qualified": True,
            "observed_at_bounded_to_completion": True,
            "executor_self_attestation_allowed": False,
            "legacy_bearer_attestation_allowed": False,
            "verifier_principal_may_have_worker_grants": False,
            "exact_replay_status": 200,
            "conflicting_replay_status": 409,
            "raw_receipts_persisted": False,
        },
        "approval_at_birth": {
            "effect_claim_revalidation_required": True,
            "caller_approval_tags_authoritative": False,
            "queue_row_committed_before_authority_side_effect": True,
            "canonical_job_id_pattern": CANONICAL_JOB_ID_PATTERN,
            "blocked_discovery": {
                "response": "list",
                "order": "canonical_job_id_ascending",
                "cursor_parameter": "after",
                "cursor_comparison": "job_id > after",
                "maximum_page_size": 200,
                "emits_only_canonical_ids": True,
                "legacy_row_policy": "exclude_and_report",
                "legacy_count_header": (
                    "X-Colony-Blocked-Legacy-Count"
                ),
                "legacy_count_is_task_type_filtered": True,
                "read_only": True,
            },
            "all_effects_require_canonical_authority": True,
            "graduated_mutation_auto_queue_enabled": False,
            "outbound_graduated_auto_queue_enabled": False,
            "outbound_authority": [
                "durable_owner_decision", "consumed_bounded_grant",
            ],
            "future_fast_path_prerequisite": (
                "server_issued_target_and_transport_attestation"
            ),
        },
        "provider_context_privacy": {
            "schema": "ContextProjectionAttestationV1",
            "version": 1,
            "readiness_endpoint": (
                "/v1/host/context/projection-readiness"
            ),
            "required_scope": "context:read",
            "guest_assemble_projection_policy": (
                "scoped_viewer_required"
            ),
            "viewer_person_id_must_match_turn_contact": True,
            "guest_scoped_projection_modes": ["shadow", "live"],
            "guest_projection_backends": ["p8", "canonical_sources"],
            "guest_legacy_global_allowed": False,
            "p8_absent_scoped_guest_status": 200,
            "exact_owner_legacy_context_carveout": True,
            "temporary_legacy_bearer_carveout": True,
            "guest_temporal_endpoint_allowed": False,
            "guest_reply_thread_projection_enabled": False,
            "general_plugin_governance_ready": False,
            "general_plugin_follow_on_slice": (
                "hermes-session-governance-v1"
            ),
        },
        "typed_job_routing": {
            "thought": {
                "schema": "ThoughtJobV1",
                "version": 1,
                "job_type": "thought",
                "required_capabilities": [
                    "cognition_scoped", THOUGHT_ROUTE,
                ],
                "route_tag": "thought_route",
                "exact_owner_tag": "thought_route_node",
                "deployment_owner": owners["thought"],
                "exact_owner_enforced": owners["thought"] is not None,
                "production_handler": "ThoughtOnlyInferenceHandler:v1",
                "result_schema": "ThoughtOutputV1",
                "result_version": 1,
            },
        },
    }
    canonical = json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    contract["contract_sha256"] = hashlib.sha256(canonical).hexdigest()
    return contract
