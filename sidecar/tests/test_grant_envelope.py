"""Configurable approval-grant envelope regression tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from colony_sidecar.initiatives import approval_authority as authority


def _payload() -> dict:
    return {
        "action_hint": "coding_merge_pr",
        "risk": "destructive",
        "description": "merge",
        "context": {"PR": "17"},
    }


def _binding(job_id: str):
    return authority.build_action_binding(
        job_id=job_id,
        job_type="agent_action",
        payload=_payload(),
    )


def _presentation(job_id: str) -> dict:
    return authority.build_approval_presentation(
        job_id=job_id,
        job_type="agent_action",
        payload=_payload(),
    )


def _mint_grant(
    store: authority.ApprovalAuthorityStore,
    *,
    now: datetime,
    ttl_seconds: int,
    max_uses: int,
    job_id: str = "job-source",
) -> dict:
    binding = _binding(job_id)
    request = store.ensure_request(
        job_id=job_id,
        binding=binding,
        presentation=_presentation(job_id),
        now=now,
    )
    decided = store.decide(
        request["request_id"],
        decision="approve",
        decision_id=f"decision_{job_id}",
        expected_action_digest=binding.action_digest,
        decided_by="owner-approval-service",
        authority_evidence="scoped_principal:owner-approval-service:key-1",
        grant_scope=binding.scope,
        grant_ttl_seconds=ttl_seconds,
        grant_max_uses=max_uses,
        now=now,
    )
    return decided["grant"]


def test_default_grant_envelope_preserves_30_day_100_use_caps(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("COLONY_GRANT_MAX_TTL_SECONDS", raising=False)
    monkeypatch.delenv("COLONY_GRANT_MAX_USES", raising=False)

    envelope = authority.resolve_grant_envelope()

    assert envelope.max_ttl_seconds == 30 * 24 * 60 * 60
    assert envelope.max_uses == 100
    assert envelope.standing_dimensions == ()

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    at_cap = _mint_grant(
        authority.ApprovalAuthorityStore(tmp_path / "at-cap.db"),
        now=now,
        ttl_seconds=envelope.max_ttl_seconds,
        max_uses=envelope.max_uses,
    )
    assert at_cap["expires_at"] == (now + timedelta(days=30)).isoformat()
    assert at_cap["max_uses"] == 100

    with pytest.raises(authority.ApprovalAuthorityError) as ttl_error:
        _mint_grant(
            authority.ApprovalAuthorityStore(tmp_path / "over-ttl.db"),
            now=now,
            ttl_seconds=envelope.max_ttl_seconds + 1,
            max_uses=envelope.max_uses,
        )
    assert ttl_error.value.code == "invalid_grant_expiry"
    with pytest.raises(authority.ApprovalAuthorityError) as uses_error:
        _mint_grant(
            authority.ApprovalAuthorityStore(tmp_path / "over-uses.db"),
            now=now,
            ttl_seconds=envelope.max_ttl_seconds,
            max_uses=envelope.max_uses + 1,
        )
    assert uses_error.value.code == "invalid_grant_uses"

    from pydantic import ValidationError
    from colony_sidecar.api.routers.task_queue import BoundedGrantRequest

    with pytest.raises(ValidationError):
        BoundedGrantRequest(expires_in_seconds=envelope.max_ttl_seconds + 1)
    with pytest.raises(ValidationError):
        BoundedGrantRequest(max_uses=envelope.max_uses + 1)


def test_configured_finite_grant_envelope_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_GRANT_MAX_TTL_SECONDS", str(60 * 24 * 60 * 60))
    monkeypatch.setenv("COLONY_GRANT_MAX_USES", "250")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = authority.ApprovalAuthorityStore(tmp_path / "authority.db")

    grant = _mint_grant(
        store,
        now=now,
        ttl_seconds=45 * 24 * 60 * 60,
        max_uses=200,
    )

    assert datetime.fromisoformat(grant["expires_at"]) == now + timedelta(days=45)
    assert grant["max_uses"] == 200

    with pytest.raises(authority.ApprovalAuthorityError) as ttl_error:
        _mint_grant(
            authority.ApprovalAuthorityStore(tmp_path / "over-ttl.db"),
            job_id="job-over-ttl",
            now=now,
            ttl_seconds=60 * 24 * 60 * 60 + 1,
            max_uses=250,
        )
    assert ttl_error.value.code == "invalid_grant_expiry"
    with pytest.raises(authority.ApprovalAuthorityError) as uses_error:
        _mint_grant(
            authority.ApprovalAuthorityStore(tmp_path / "over-uses.db"),
            job_id="job-over-uses",
            now=now,
            ttl_seconds=60 * 24 * 60 * 60,
            max_uses=251,
        )
    assert uses_error.value.code == "invalid_grant_uses"

    from colony_sidecar.api.routers.task_queue import BoundedGrantRequest

    assert BoundedGrantRequest(
        expires_in_seconds=45 * 24 * 60 * 60,
        max_uses=200,
    ).max_uses == 200
    monkeypatch.setenv("COLONY_GRANT_MAX_TTL_SECONDS", "60")
    monkeypatch.setenv("COLONY_GRANT_MAX_USES", "1")
    narrowed_defaults = BoundedGrantRequest()
    assert narrowed_defaults.expires_in_seconds == 60
    assert narrowed_defaults.max_uses == 1


def test_standing_grant_neither_expires_nor_exhausts(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_GRANT_MAX_TTL_SECONDS", "unlimited")
    monkeypatch.setenv("COLONY_GRANT_MAX_USES", "unlimited")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = authority.ApprovalAuthorityStore(tmp_path / "authority.db")
    grant = _mint_grant(store, now=now, ttl_seconds=60, max_uses=1)

    assert grant["expires_at"] is None
    assert grant["ttl_unbounded"] is True
    assert grant["max_uses"] is None
    assert grant["uses_unbounded"] is True
    for index in range(3):
        job_id = f"job-use-{index}"
        binding = _binding(job_id)
        resolved = authority.prepare_action_approval(
            store,
            job_id=job_id,
            job_type="agent_action",
            payload=_payload(),
            now=now + timedelta(days=3650 + index),
        )
        assert resolved["state"] == "authorized_grant"
        assert resolved["grant_use"]["grant_status"] == "active"
        assert resolved["grant_use"]["grant_expires_at"] is None
        assert resolved["grant_use"]["max_uses"] is None
        assert resolved["tags"]["bounded_grant_expires_at"] == "unlimited"
        assert resolved["tags"]["bounded_grant_ttl_state"] == "unbounded"
        assert resolved["tags"]["bounded_grant_uses_state"] == "unbounded"
        receipt = store.get_grant_use(binding.action_digest)
        assert receipt is not None
        assert receipt["source_request_id"] == grant["source_request_id"]
        assert receipt["decision_id"] == grant["decision_id"]
    assert store.list_grants(now=now + timedelta(days=5000))[0]["uses"] == 3
    assert store.grant_posture(now=now + timedelta(days=5000)) == {
        "max_ttl_seconds": None,
        "max_ttl_state": "unbounded",
        "max_uses": None,
        "max_uses_state": "unbounded",
        "standing": True,
        "sentinel": "unlimited",
        "active_standing_grants": 1,
        "active_no_expiry_grants": 1,
        "active_no_use_cap_grants": 1,
    }
    bounded_restart = authority.ApprovalAuthorityStore(
        tmp_path / "authority.db",
        grant_envelope=authority.GrantEnvelope(
            max_ttl_seconds=30 * 24 * 60 * 60,
            max_uses=100,
        ),
    )
    bounded_posture = bounded_restart.grant_posture(
        now=now + timedelta(days=5000)
    )
    assert bounded_posture["standing"] is False
    assert bounded_posture["active_standing_grants"] == 1


def test_revoke_grant_kills_standing_authority_at_point_of_use(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_GRANT_MAX_TTL_SECONDS", "unlimited")
    monkeypatch.setenv("COLONY_GRANT_MAX_USES", "unlimited")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = authority.ApprovalAuthorityStore(tmp_path / "authority.db")
    grant = _mint_grant(store, now=now, ttl_seconds=60, max_uses=1)
    assert store.grant_envelope.standing_dimensions == (
        "COLONY_GRANT_MAX_TTL_SECONDS",
        "COLONY_GRANT_MAX_USES",
    )

    assert store.revoke_grant(grant["grant_id"], now=now + timedelta(seconds=1))
    assert store.consume_grant(
        binding=_binding("job-after-revocation"),
        operation_id="operation-after-revocation",
        now=now + timedelta(seconds=2),
    ) is None


def test_non_grantable_tool_is_refused_under_standing_envelope(monkeypatch):
    """Exercise both the worker allowlist and its in-transaction backstop."""

    monkeypatch.setenv("COLONY_GRANT_MAX_TTL_SECONDS", "unlimited")
    monkeypatch.setenv("COLONY_GRANT_MAX_USES", "unlimited")
    assert authority.resolve_grant_envelope().standing_dimensions

    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo_root / "hostworker"))
    from colony_hostworker.catalog import GRANT_AUTHORIZABLE_TOOL_NAMES
    from colony_hostworker.conformance.harness import sqlite_harness
    from colony_hostworker.conformance.suite import (
        check_non_grantable_tool_with_grant_proof,
    )

    assert "colony_autonomy_enable" not in GRANT_AUTHORIZABLE_TOOL_NAMES
    assert "colony_autonomy_disable" not in GRANT_AUTHORIZABLE_TOOL_NAMES
    check_non_grantable_tool_with_grant_proof(sqlite_harness)
