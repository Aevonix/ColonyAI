"""Tests for the colony doctor check engine (v0.19.0)."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar import doctor
from colony_sidecar.doctor import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    CheckResult,
    exit_code,
    format_report,
    results_to_json,
    run_doctor,
    run_local_checks,
    run_server_checks,
)

_ENV_VARS = (
    "COLONY_STATE_DIR",
    "COLONY_CONTACTS_DB",
    "COLONY_OWNER_CONTACT_ID",
    "COLONY_HOST_CONTACT_ID",
    "COLONY_APPROVAL_POLICY",
    "COLONY_ENABLE_INTERNAL_THINKING",
    "COLONY_ENABLE_SKILL_SYNTHESIS",
    "COLONY_EMIT_HERMES_SKILLS",
    "COLONY_HERMES_SKILLS_DIR",
    "COLONY_API_KEY",
    "COLONY_URL",
    "COLONY_SIDECAR_URL",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolated env + a real, writable state dir."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for key in list(os.environ):
        if key.endswith("_HOME_CHANNEL"):
            monkeypatch.delenv(key, raising=False)
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLONY_STATE_DIR", str(state))
    return state


def _by_name(results):
    return {r.name: r for r in results}


def _fake_http(responses):
    """Build an _http_get stand-in keyed by URL path suffix."""
    def _get(url, api_key="", timeout=10.0):
        for suffix in sorted(responses, key=len, reverse=True):
            if url.endswith(suffix):
                resp = responses[suffix]
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected URL in test: {url}")
    return _get


# ---------------------------------------------------------------------------
# Engine plumbing
# ---------------------------------------------------------------------------

def test_run_wrapper_turns_exception_into_fail():
    def boom():
        raise RuntimeError("kaput")

    results = doctor._run("exploding-check", boom)
    assert len(results) == 1
    assert results[0].name == "exploding-check"
    assert results[0].status == FAIL
    assert "RuntimeError" in results[0].detail
    assert "kaput" in results[0].detail


def test_run_local_checks_never_raises(clean_env, monkeypatch):
    monkeypatch.setattr(
        doctor, "check_contacts_db",
        lambda: (_ for _ in ()).throw(OSError("disk on fire")),
    )
    results = run_local_checks()
    assert _by_name(results)["contacts-db"].status == FAIL
    assert "disk on fire" in _by_name(results)["contacts-db"].detail


# ---------------------------------------------------------------------------
# 1. state dir
# ---------------------------------------------------------------------------

def test_state_dir_pass(clean_env):
    result = doctor.check_state_dir()
    assert result.status == PASS


def test_state_dir_missing_fails(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "nope"))
    result = doctor.check_state_dir()
    assert result.status == FAIL
    assert "does not exist" in result.detail
    assert result.remedy


# ---------------------------------------------------------------------------
# 2. persisted LLM config
# ---------------------------------------------------------------------------

def _write_llm_config(state, **overrides):
    cfg = {
        "provider": "vllm",
        "apiKey": "local",
        "baseUrl": "http://127.0.0.1:8000/v1",
        "models": {"small": "qwen2.5-7b", "medium": "qwen2.5-7b", "large": "qwen2.5-72b"},
    }
    cfg.update(overrides)
    (state / ".colony-llm-config.json").write_text(json.dumps(cfg))
    return cfg


def test_llm_config_missing_warns_and_skips_subchecks(clean_env):
    by = _by_name(doctor.check_llm_config())
    assert by["llm-config"].status == WARN
    for name in ("llm-config-baseurl", "llm-config-apikey", "llm-config-models"):
        assert by[name].status == SKIP


def test_llm_config_corrupt_fails(clean_env):
    (clean_env / ".colony-llm-config.json").write_text("{not json")
    by = _by_name(doctor.check_llm_config())
    assert by["llm-config"].status == FAIL
    assert by["llm-config-baseurl"].status == SKIP


def test_llm_config_happy_path(clean_env):
    _write_llm_config(clean_env)
    by = _by_name(doctor.check_llm_config())
    assert by["llm-config"].status == PASS
    assert by["llm-config-baseurl"].status == PASS
    assert by["llm-config-apikey"].status == PASS
    assert by["llm-config-models"].status == PASS


def test_llm_config_baseurl_missing_v1_warns_with_exact_remedy(clean_env):
    _write_llm_config(clean_env, baseUrl="http://127.0.0.1:8000")
    result = _by_name(doctor.check_llm_config())["llm-config-baseurl"]
    assert result.status == WARN
    assert "all tiers exhausted" in result.detail
    assert '"http://127.0.0.1:8000/v1"' in result.remedy


def test_llm_config_baseurl_not_required_for_anthropic(clean_env):
    _write_llm_config(clean_env, provider="anthropic", baseUrl="", apiKey="sk-ant-x")
    result = _by_name(doctor.check_llm_config())["llm-config-baseurl"]
    assert result.status == PASS


def test_llm_config_empty_apikey_fails(clean_env):
    _write_llm_config(clean_env, apiKey="")
    result = _by_name(doctor.check_llm_config())["llm-config-apikey"]
    assert result.status == FAIL
    assert "OPENAI_API_KEY" in result.detail
    assert "apiKey" in result.remedy


def test_llm_config_ollama_needs_no_apikey(clean_env):
    _write_llm_config(clean_env, provider="ollama", apiKey="",
                      baseUrl="http://127.0.0.1:11434")
    result = _by_name(doctor.check_llm_config())["llm-config-apikey"]
    assert result.status == PASS


def test_llm_config_empty_models_warns(clean_env):
    _write_llm_config(clean_env, models={})
    result = _by_name(doctor.check_llm_config())["llm-config-models"]
    assert result.status == WARN


# ---------------------------------------------------------------------------
# 3. contacts DB
# ---------------------------------------------------------------------------

def test_contacts_db_memory_fails(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_CONTACTS_DB", ":memory:")
    result = doctor.check_contacts_db()
    assert result.status == FAIL
    assert ":memory:" in result.detail


def test_contacts_db_not_created_yet_passes(clean_env):
    assert doctor.check_contacts_db().status == PASS


def test_contacts_db_missing_parent_fails(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_CONTACTS_DB", str(tmp_path / "nodir" / "contacts.db"))
    result = doctor.check_contacts_db()
    assert result.status == FAIL
    assert "parent" in result.detail.lower()


def test_contacts_db_real_sqlite_passes(clean_env):
    db_path = clean_env / "colony-contacts.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE contacts (id TEXT)")
    conn.commit()
    conn.close()
    assert doctor.check_contacts_db().status == PASS


def test_contacts_db_garbage_file_fails(clean_env):
    (clean_env / "colony-contacts.db").write_text("not a database")
    result = doctor.check_contacts_db()
    assert result.status == FAIL
    assert "SQLite" in result.detail


# ---------------------------------------------------------------------------
# 4. owner contact id
# ---------------------------------------------------------------------------

def test_owner_unset_warns_with_degradation_explained(clean_env):
    result = doctor.check_owner_contact_id()
    assert result.status == WARN
    assert "CRITICAL" in result.detail
    assert "COLONY_OWNER_CONTACT_ID" in result.remedy


def test_owner_set_passes(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-123-abc")
    result = doctor.check_owner_contact_id()
    assert result.status == PASS
    assert "cid-123-abc" in result.detail


def test_owner_legacy_alias_warns(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_HOST_CONTACT_ID", "cid-old")
    result = doctor.check_owner_contact_id()
    assert result.status == WARN
    assert "deprecated" in result.detail


# ---------------------------------------------------------------------------
# 5. approval policy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "strict", "graduated", "  Graduated "])
def test_approval_policy_valid_values_pass(clean_env, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("COLONY_APPROVAL_POLICY", value)
    assert doctor.check_approval_policy().status == PASS


def test_approval_policy_typo_fails(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_APPROVAL_POLICY", "gradutaed")
    result = doctor.check_approval_policy()
    assert result.status == FAIL
    assert "strict" in result.detail  # explains fail-closed behavior
    assert result.remedy


# ---------------------------------------------------------------------------
# 6. standing approvals
# ---------------------------------------------------------------------------

def test_standing_approvals_absent_passes(clean_env):
    assert doctor.check_standing_approvals().status == PASS


def test_standing_approvals_valid_passes(clean_env):
    (clean_env / "standing_approvals.json").write_text(
        json.dumps({"agent_git_push": {"approved_by": "owner"}})
    )
    result = doctor.check_standing_approvals()
    assert result.status == PASS
    assert "1" in result.detail


def test_standing_approvals_corrupt_fails(clean_env):
    (clean_env / "standing_approvals.json").write_text("{broken")
    result = doctor.check_standing_approvals()
    assert result.status == FAIL
    assert "fail closed" in result.detail


def test_standing_approvals_non_object_fails(clean_env):
    (clean_env / "standing_approvals.json").write_text("[1, 2]")
    assert doctor.check_standing_approvals().status == FAIL


# ---------------------------------------------------------------------------
# 7. feature gates
# ---------------------------------------------------------------------------

def test_gates_unset_pass(clean_env):
    assert doctor.check_feature_gates().status == PASS


def test_gates_odd_value_warns(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_INTERNAL_THINKING", "1")
    result = doctor.check_feature_gates()
    assert result.status == WARN
    assert "treated as false" in result.detail


def test_gates_thinking_enabled_notes_llm_dependency(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_ENABLE_INTERNAL_THINKING", "true")
    result = doctor.check_feature_gates()
    assert result.status == PASS
    assert "LLM router" in result.detail


# ---------------------------------------------------------------------------
# 8. home channel
# ---------------------------------------------------------------------------

def test_home_channel_none_warns(clean_env):
    result = doctor.check_home_channel()
    assert result.status == WARN
    assert "never deliver" in result.detail


def test_home_channel_set_passes(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "12345")
    result = doctor.check_home_channel()
    assert result.status == PASS
    assert "TELEGRAM_HOME_CHANNEL" in result.detail


# ---------------------------------------------------------------------------
# 9. hermes skills dir
# ---------------------------------------------------------------------------

def test_hermes_disabled_skips(clean_env):
    assert doctor.check_hermes_skills_dir().status == SKIP


def test_hermes_enabled_missing_parent_warns(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
    monkeypatch.setenv(
        "COLONY_HERMES_SKILLS_DIR", str(tmp_path / "hermes" / "skills" / "colony")
    )
    result = doctor.check_hermes_skills_dir()
    assert result.status == WARN


def test_hermes_enabled_parent_exists_passes(clean_env, monkeypatch, tmp_path):
    base = tmp_path / "hermes" / "skills" / "colony"
    base.parent.mkdir(parents=True)
    monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
    monkeypatch.setenv("COLONY_HERMES_SKILLS_DIR", str(base))
    assert doctor.check_hermes_skills_dir().status == PASS


# ---------------------------------------------------------------------------
# Server checks (mocked HTTP)
# ---------------------------------------------------------------------------

URL = "http://127.0.0.1:7777"


def _happy_responses(owner="cid-owner-1"):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "/v1/host/health": (200, {"status": "ok", "capabilities": ["memory", "goals"]}),
        "/v1/host/queue/stats": (200, {"by_status": {}}),
        f"/v1/host/contacts/{owner}": (200, {"contact_id": owner}),
        "/v1/host/health/llm": (200, {"ok": True, "tier": "small",
                                      "latency_ms": 40, "error": None}),
        "/v1/host/embed/health": (200, {"status": "ok", "dims": 384, "latency_ms": 4}),
        "/v1/host/queue/jobs/blocked": (200, []),
        "/v1/host/observations/skills": (200, {
            "domain": "skills",
            "observations": [{"entity_id": "s1", "observed_at": now}],
            "total": 1,
        }),
    }


def test_server_down_fails_health_and_skips_rest(clean_env, monkeypatch):
    monkeypatch.setattr(doctor, "_http_get", _fake_http({
        "/v1/host/health": urllib.error.URLError("connection refused"),
    }))
    results = run_server_checks(URL, "key")
    by = _by_name(results)
    assert by["server-health"].status == FAIL
    assert "colony start" in by["server-health"].remedy
    for name in doctor.SERVER_CHECK_NAMES[1:]:
        assert by[name].status == SKIP
        assert "unreachable" in by[name].detail
    assert len(results) == len(doctor.SERVER_CHECK_NAMES)


def test_server_happy_path_all_pass(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-1")
    monkeypatch.setattr(doctor, "_http_get", _fake_http(_happy_responses()))
    by = _by_name(run_server_checks(URL, "key"))
    for name in doctor.SERVER_CHECK_NAMES:
        assert by[name].status == PASS, f"{name}: {by[name].detail}"


def test_server_degraded_health_warns(clean_env, monkeypatch):
    responses = _happy_responses()
    responses["/v1/host/health"] = (200, {
        "status": "degraded", "capabilities": [],
        "notes": {"embed": "EmbeddingPipeline wired [WARNING: stored models differ]"},
    })
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-health"]
    assert result.status == WARN
    assert "degraded" in result.detail


def test_server_auth_401_fails(clean_env, monkeypatch):
    responses = _happy_responses()
    responses["/v1/host/queue/stats"] = (401, {"detail": "Invalid or missing API key"})
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "wrong-key"))["server-auth"]
    assert result.status == FAIL
    assert "COLONY_API_KEY" in result.remedy


def test_server_owner_unset_skips(clean_env, monkeypatch):
    monkeypatch.setattr(doctor, "_http_get", _fake_http(_happy_responses()))
    result = _by_name(run_server_checks(URL, "key"))["server-owner-contact"]
    assert result.status == SKIP


def test_server_owner_non_cid_skips(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "Marc")
    monkeypatch.setattr(doctor, "_http_get", _fake_http(_happy_responses()))
    result = _by_name(run_server_checks(URL, "key"))["server-owner-contact"]
    assert result.status == SKIP


def test_server_owner_404_fails_with_create_remedy(clean_env, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "cid-owner-1")
    responses = _happy_responses()
    responses["/v1/host/contacts/cid-owner-1"] = (404, {"detail": "Contact not found"})
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-owner-contact"]
    assert result.status == FAIL
    assert "POST /v1/host/contacts" in result.remedy


def test_server_llm_failure_fails_with_footgun_remedy(clean_env, monkeypatch):
    responses = _happy_responses()
    responses["/v1/host/health/llm"] = (200, {
        "ok": False, "tier": None, "latency_ms": 0,
        "error": "LLMRouter: all tiers exhausted for request abc",
    })
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-llm-router"]
    assert result.status == FAIL
    assert "all tiers exhausted" in result.detail
    assert "/v1" in result.remedy
    assert "apiKey" in result.remedy


def test_server_llm_endpoint_missing_skips(clean_env, monkeypatch):
    responses = _happy_responses()
    responses["/v1/host/health/llm"] = (404, {"detail": "Not Found"})
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-llm-router"]
    assert result.status == SKIP


def test_server_embedder_degraded_warns(clean_env, monkeypatch):
    responses = _happy_responses()
    responses["/v1/host/embed/health"] = (200, {
        "status": "error", "error": "embedder not initialized",
    })
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-embedder"]
    assert result.status == WARN
    assert "embedder not initialized" in result.detail


def test_server_blocked_jobs_warn(clean_env, monkeypatch):
    responses = _happy_responses()
    responses["/v1/host/queue/jobs/blocked"] = (200, [
        {"id": "j1", "action_hint": "agent_git_push", "risk": "mutating"},
        {"id": "j2", "action_hint": "agent_deploy", "risk": "destructive"},
    ])
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-blocked-approvals"]
    assert result.status == WARN
    assert "2 job(s) pending owner approval" in result.detail


def test_server_skills_observations_empty_warns(clean_env, monkeypatch):
    responses = _happy_responses()
    responses["/v1/host/observations/skills"] = (200, {
        "domain": "skills", "observations": [], "total": 0,
    })
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-skills-observations"]
    assert result.status == WARN
    assert "colony-skills-sync" in result.remedy


def test_server_skills_observations_stale_warns(clean_env, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    responses = _happy_responses()
    responses["/v1/host/observations/skills"] = (200, {
        "domain": "skills",
        "observations": [{"entity_id": "s1", "observed_at": old}],
        "total": 1,
    })
    monkeypatch.setattr(doctor, "_http_get", _fake_http(responses))
    result = _by_name(run_server_checks(URL, "key"))["server-skills-observations"]
    assert result.status == WARN
    assert "stale" in result.detail


def test_run_doctor_combines_local_and_server(clean_env, monkeypatch):
    monkeypatch.setattr(doctor, "_http_get", _fake_http({
        "/v1/host/health": urllib.error.URLError("refused"),
    }))
    names = {r.name for r in run_doctor(colony_url=URL, api_key="k")}
    assert "state-dir" in names
    assert "llm-config-baseurl" in names
    assert set(doctor.SERVER_CHECK_NAMES) <= names


# ---------------------------------------------------------------------------
# /v1/host/health/llm endpoint (router mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_health_endpoint_ok(monkeypatch):
    from colony_sidecar.api.routers import host
    from colony_sidecar.router.tiers import ModelTier

    class FakeResp:
        tier_used = ModelTier.SMALL
        latency_ms = 42

    class FakeRouter:
        async def complete(self, messages, *, force_tier=None, **kwargs):
            assert force_tier == ModelTier.SMALL
            assert messages == [{"role": "user", "content": "Say OK"}]
            return FakeResp()

    monkeypatch.setattr(host, "_llm_router", FakeRouter())
    out = await host.llm_health()
    assert out == {"ok": True, "tier": "small", "latency_ms": 42, "error": None}


@pytest.mark.asyncio
async def test_llm_health_endpoint_all_tiers_exhausted(monkeypatch):
    from colony_sidecar.api.routers import host

    class BrokenRouter:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("LLMRouter: all tiers exhausted for request xyz")

    monkeypatch.setattr(host, "_llm_router", BrokenRouter())
    out = await host.llm_health()
    assert out["ok"] is False
    assert "all tiers exhausted" in out["error"]


@pytest.mark.asyncio
async def test_llm_health_endpoint_not_wired(monkeypatch):
    from colony_sidecar.api.routers import host

    monkeypatch.setattr(host, "_llm_router", None)
    out = await host.llm_health()
    assert out["ok"] is False
    assert "not wired" in out["error"]


# ---------------------------------------------------------------------------
# Exit code, JSON shape, report formatting, CLI
# ---------------------------------------------------------------------------

def test_exit_code_zero_with_warns_and_skips():
    results = [
        CheckResult("a", PASS),
        CheckResult("b", WARN, "meh"),
        CheckResult("c", SKIP, "later"),
    ]
    assert exit_code(results) == 0


def test_exit_code_one_with_any_fail():
    results = [CheckResult("a", PASS), CheckResult("b", FAIL, "boom")]
    assert exit_code(results) == 1


def test_results_to_json_shape():
    results = [
        CheckResult("a", PASS, "fine"),
        CheckResult("b", FAIL, "boom", "fix it"),
    ]
    payload = results_to_json(results)
    assert set(payload) == {"results", "summary", "ok"}
    assert payload["ok"] is False
    assert payload["summary"] == {"pass": 1, "warn": 0, "fail": 1, "skip": 0}
    assert payload["results"][1] == {
        "name": "b", "status": "fail", "detail": "boom", "remedy": "fix it",
    }
    json.dumps(payload)  # must be serializable


def test_format_report_includes_remedies_and_summary():
    results = [
        CheckResult("good-check", PASS, "all fine"),
        CheckResult("bad-check", FAIL, "exploded", "turn it off and on"),
    ]
    report = format_report(results, colony_url="http://x:7777", color=False)
    assert "PASS" in report and "FAIL" in report
    assert "turn it off and on" in report
    assert "1 pass, 0 warn, 1 fail, 0 skip" in report


def test_cmd_doctor_json_output_and_exit_code(clean_env, monkeypatch, capsys):
    from colony_sidecar import cli

    fake = [CheckResult("a", PASS, "ok"), CheckResult("b", FAIL, "boom", "fix")]
    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: fake)
    args = SimpleNamespace(url=None, api_key=None, json=True, timeout=5.0)
    with pytest.raises(SystemExit) as excinfo:
        cli._cmd_doctor(args)
    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert [r["name"] for r in payload["results"]] == ["a", "b"]


def test_cmd_doctor_human_output_exit_zero(clean_env, monkeypatch, capsys):
    from colony_sidecar import cli

    fake = [CheckResult("a", PASS, "ok"), CheckResult("b", WARN, "meh", "tweak")]
    monkeypatch.setattr(doctor, "run_doctor", lambda **kwargs: fake)
    args = SimpleNamespace(url=None, api_key=None, json=False, timeout=5.0)
    with pytest.raises(SystemExit) as excinfo:
        cli._cmd_doctor(args)
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "WARN" in out
    assert "tweak" in out
