"""Embedded handlers enforce effects and return truthful result contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apsimo.router.tiers import ModelTier
from apsimo.task_queue.handlers.inference import InferenceHandler
from apsimo.task_queue.handlers.monitoring import _validate_endpoint
from apsimo.task_queue.handlers.system_maintenance import (
    SystemMaintenanceHandler,
)
from apsimo.task_queue.models import Job, JobType
from apsimo.task_queue.worker import _prepare_embedded_report


def test_registry_has_real_handlers_and_preserves_historical_job_types():
    from apsimo.task_queue.handlers.registry import build_default_handlers
    assert set(build_default_handlers()) == {
        JobType.MONITORING, JobType.SYSTEM_MAINTENANCE, JobType.CUSTOM}
    for legacy in ("desktop", "browser"):
        assert Job(job_type=legacy).job_type == JobType(legacy)


@pytest.mark.parametrize("parameter", ["desktop_config", "browser_config"])
@pytest.mark.parametrize("enabled", [True, False])
def test_registry_rejects_unsupported_legacy_worker_configuration(parameter, enabled):
    from apsimo.task_queue.handlers.registry import build_default_handlers
    with pytest.raises(ValueError, match="native runtime"):
        build_default_handlers(**{parameter: SimpleNamespace(enabled=enabled)})


@pytest.mark.asyncio
async def test_maintenance_cannot_escape_configured_state_root(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    handler = SystemMaintenanceHandler(allowed_roots=[state])

    with pytest.raises(ValueError, match="must stay inside"):
        await handler.execute(Job(
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "disk_cleanup", "target_path": str(outside)},
        ))
    assert marker.read_text() == "keep"


@pytest.mark.asyncio
async def test_maintenance_success_has_attestable_contract(tmp_path):
    state = tmp_path / "state"
    target = state / "tmp"
    target.mkdir(parents=True)
    (target / "remove.txt").write_text("temporary")
    handler = SystemMaintenanceHandler(allowed_roots=[state])
    output = await handler.execute(Job(
        job_type=JobType.SYSTEM_MAINTENANCE,
        payload={"action": "disk_cleanup", "target_path": str(target)},
    ))

    assert output["status"] == "completed"
    assert output["action_plane"]["state"] == "completed"
    assert output["errors"] == []
    assert list(target.iterdir()) == []


def test_embedded_result_contract_never_attests_failures_or_skips():
    job = Job()
    failure, failure_attested = _prepare_embedded_report(
        job, {"success": False, "error": "no credential"},
    )
    skipped, skipped_attested = _prepare_embedded_report(
        job, {"status": "skipped", "gate_blocked": True},
    )
    success, success_attested = _prepare_embedded_report(job, {
        "status": "completed",
        "summary": "done",
        "action_plane": {"state": "completed"},
    })

    assert failure["status"] == "failed" and not failure_attested
    assert skipped["status"] == "skipped" and not skipped_attested
    assert success["status"] == "completed" and success_attested


@pytest.mark.asyncio
async def test_monitoring_rejects_private_targets_unless_explicitly_allowlisted(
    monkeypatch,
):
    monkeypatch.delenv("COLONY_MONITORING_HOST_ALLOWLIST", raising=False)
    with pytest.raises(ValueError, match="non-public"):
        await _validate_endpoint("http://127.0.0.1:7777/health")

    monkeypatch.setenv("COLONY_MONITORING_HOST_ALLOWLIST", "127.0.0.1")
    assert await _validate_endpoint(
        "http://127.0.0.1:7777/health"
    ) == "http://127.0.0.1:7777/health"


@pytest.mark.asyncio
async def test_monitoring_rejects_non_global_ip_and_unpinned_dns(monkeypatch):
    monkeypatch.delenv("COLONY_MONITORING_HOST_ALLOWLIST", raising=False)
    with pytest.raises(ValueError, match="non-public"):
        await _validate_endpoint("http://100.64.0.1/health")
    with pytest.raises(ValueError, match="explicitly configured"):
        await _validate_endpoint("https://status.example.test/health")

    monkeypatch.setenv(
        "COLONY_MONITORING_HOST_ALLOWLIST", "status.example.test",
    )
    assert await _validate_endpoint(
        "https://status.example.test/health"
    ) == "https://status.example.test/health"


class _InferenceRouter:
    def route(self, _prompt, _context):
        return ModelTier.SMALL, "test-model"

    async def complete(self, _messages, **_kwargs):
        return SimpleNamespace(
            usage={"total_tokens": 4, "completion_tokens": 2},
            tier_used=ModelTier.SMALL,
            model_id="test-model",
            cost_usd=0.0,
            latency_ms=1,
            request_id="request-1",
            content="private response",
        )


class _ExplodingGate:
    async def evaluate(self, _payload):
        raise RuntimeError("gate unavailable")


class _GateSessions:
    def register(self, *_args):
        return None


@pytest.mark.asyncio
async def test_inference_gate_failure_holds_external_but_not_internal():
    handler = InferenceHandler(
        router=_InferenceRouter(),
        response_gate=_ExplodingGate(),
        gate_session_store=_GateSessions(),
    )
    # Keep this unit test out of the optional world-model initialization path.
    handler._wm_connected = True
    handler._wm = None

    external = await handler.execute(Job(payload={
        "prompt": "tell the guest",
        "contact_id": "guest-1",
    }))
    internal = await handler.execute(Job(payload={
        "prompt": "internal analysis",
        "contact_id": "internal",
    }))

    assert external["status"] == "skipped"
    assert external["gate_reason"] == "recipient_gate_unavailable"
    assert internal["status"] == "completed"
    assert internal["result"] == "private response"
