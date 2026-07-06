"""Wizard autonomy & approvals step (v0.19).

Covers the pure helpers extracted from the setup wizard:
- LLM host-config footgun fixes (baseUrl /v1 normalization, non-empty apiKey)
- owner contact creation against a tmp-path contact store
- env-values assembly for approval policy / autonomy gates / home channel
  with scripted answers via the injectable ``ask`` callable
"""

import json

import pytest

from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.setup import (
    _prompt,
    apply_llm_config_fixes,
    build_owner_contact,
    collect_autonomy_env,
    collect_owner_handles,
    ensure_api_key,
    normalize_llm_base_url,
    write_llm_host_config,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Scripted-defaults env var must not leak into prompt assertions."""
    monkeypatch.delenv("COLONY_INIT_DEFAULTS", raising=False)


def make_ask(answers):
    """Return an ``ask`` callable that pops scripted answers (blank when exhausted)."""
    queue = list(answers)

    def ask(prompt_text):
        return queue.pop(0) if queue else ""

    return ask


# ── _prompt with injectable ask ──────────────────────────────────────────────


def test_prompt_uses_injected_ask():
    assert _prompt("Name", "default", ask=lambda p: "value") == "value"


def test_prompt_falls_back_to_default_on_blank_answer():
    assert _prompt("Name", "default", ask=lambda p: "") == "default"


def test_prompt_non_interactive_ignores_ask():
    assert _prompt("Name", "default", non_interactive=True, ask=lambda p: "value") == "default"


# ── normalize_llm_base_url ───────────────────────────────────────────────────


def test_base_url_appends_v1_for_vllm():
    url, changed = normalize_llm_base_url("http://localhost:8000", "vllm")
    assert url == "http://localhost:8000/v1"
    assert changed is True


def test_base_url_strips_trailing_slash_before_appending():
    url, changed = normalize_llm_base_url("http://localhost:1234/", "openai-compatible")
    assert url == "http://localhost:1234/v1"
    assert changed is True


@pytest.mark.parametrize("existing", [
    "http://localhost:8000/v1",
    "http://localhost:8000/v1/",
])
def test_base_url_already_v1_unchanged(existing):
    url, changed = normalize_llm_base_url(existing, "vllm")
    assert url == existing
    assert changed is False


@pytest.mark.parametrize("provider", ["ollama", "anthropic", ""])
def test_base_url_non_openai_compat_passes_through(provider):
    url, changed = normalize_llm_base_url("http://localhost:11434", provider)
    assert url == "http://localhost:11434"
    assert changed is False


def test_base_url_empty_passes_through():
    assert normalize_llm_base_url("", "vllm") == ("", False)


@pytest.mark.parametrize("provider", ["lmstudio", "custom", "local", "OpenAI"])
def test_base_url_all_openai_compat_providers_normalized(provider):
    url, changed = normalize_llm_base_url("http://10.0.0.5:9000", provider)
    assert url == "http://10.0.0.5:9000/v1"
    assert changed is True


# ── ensure_api_key ───────────────────────────────────────────────────────────


def test_empty_api_key_defaults_to_local_no_key():
    cfg = {"provider": "vllm", "apiKey": ""}
    fixed, changed = ensure_api_key(cfg)
    assert fixed["apiKey"] == "local-no-key"
    assert changed is True
    # Input dict must not be mutated.
    assert cfg["apiKey"] == ""


def test_whitespace_api_key_treated_as_empty():
    fixed, changed = ensure_api_key({"provider": "lmstudio", "apiKey": "   "})
    assert fixed["apiKey"] == "local-no-key"
    assert changed is True


def test_missing_api_key_field_defaults():
    fixed, changed = ensure_api_key({"provider": "openai-compatible"})
    assert fixed["apiKey"] == "local-no-key"
    assert changed is True


def test_real_api_key_preserved():
    fixed, changed = ensure_api_key({"provider": "vllm", "apiKey": "sk-real"})
    assert fixed["apiKey"] == "sk-real"
    assert changed is False


def test_non_openai_compat_provider_may_keep_empty_key():
    fixed, changed = ensure_api_key({"provider": "ollama", "apiKey": ""})
    assert fixed.get("apiKey") == ""
    assert changed is False


# ── apply_llm_config_fixes / write_llm_host_config ───────────────────────────


def test_apply_llm_config_fixes_applies_both_with_notes():
    cfg = {"provider": "vllm", "baseUrl": "http://localhost:8000", "apiKey": ""}
    fixed, notes = apply_llm_config_fixes(cfg)
    assert fixed["baseUrl"] == "http://localhost:8000/v1"
    assert fixed["apiKey"] == "local-no-key"
    assert len(notes) == 2


def test_apply_llm_config_fixes_noop_for_good_config():
    cfg = {"provider": "vllm", "baseUrl": "http://localhost:8000/v1", "apiKey": "k"}
    fixed, notes = apply_llm_config_fixes(cfg)
    assert fixed == cfg
    assert notes == []


def test_write_llm_host_config_persists_fixed_config(tmp_path):
    path = tmp_path / ".colony-llm-config.json"
    cfg = {
        "provider": "openai-compatible",
        "baseUrl": "http://localhost:1234",
        "apiKey": "",
        "models": {"small": "qwen2.5"},
    }
    fixed, notes = write_llm_host_config(path, cfg)
    on_disk = json.loads(path.read_text())
    assert on_disk == fixed
    assert on_disk["baseUrl"] == "http://localhost:1234/v1"
    assert on_disk["apiKey"] == "local-no-key"
    assert on_disk["models"] == {"small": "qwen2.5"}
    assert len(notes) == 2


# ── build_owner_contact (tmp-path store) ─────────────────────────────────────


def _store(tmp_path):
    return SQLiteContactStore(
        config=ContactsConfig(sqlite_path=str(tmp_path / "contacts.db"))
    )


@pytest.mark.asyncio
async def test_build_owner_contact_creates_inner_circle_owner(tmp_path):
    store = _store(tmp_path)
    await store.connect()
    try:
        cid = await build_owner_contact(
            store, "Sam",
            [("whatsapp", "555123@lid"), ("email", "Sam@Example.com")],
        )
        contact = await store.get(cid)
        assert contact is not None
        assert contact.display_name == "Sam"
        assert contact.trust_tier == "inner_circle"
        assert contact.interaction_allowed is True
        assert contact.import_source == "wizard"

        handles = await store.get_handles(cid)
        # Email is normalized to lowercase by the store.
        assert {(h.gateway, h.address) for h in handles} == {
            ("whatsapp", "555123@lid"),
            ("email", "sam@example.com"),
        }
        primary = [h for h in handles if h.is_primary]
        assert len(primary) == 1 and primary[0].gateway == "whatsapp"

        resolved = await store.resolve_handle("email", "sam@example.com")
        assert resolved is not None and resolved.contact_id == cid
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_owner_contact_without_handles(tmp_path):
    store = _store(tmp_path)
    await store.connect()
    try:
        cid = await build_owner_contact(store, "Owner", [])
        assert (await store.get(cid)) is not None
        assert await store.get_handles(cid) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_build_owner_contact_skips_colliding_handles(tmp_path):
    store = _store(tmp_path)
    await store.connect()
    try:
        other = await store.create(display_name="Someone Else")
        await store.add_handle(other.contact_id, gateway="telegram", address="tg-1")

        cid = await build_owner_contact(
            store, "Owner", [("telegram", "tg-1"), ("signal", "+12025550100")]
        )
        # Owner record exists despite the collision; only the free handle landed.
        assert (await store.get(cid)) is not None
        handles = await store.get_handles(cid)
        assert [(h.gateway, h.address) for h in handles] == [("signal", "+12025550100")]
    finally:
        await store.close()


# ── collect_owner_handles (scripted answers) ─────────────────────────────────


def test_collect_owner_handles_loop_with_invalid_gateway():
    ask = make_ask([
        "whatsapp", "555123@lid",     # valid pair
        "carrier-pigeon",             # invalid gateway → re-prompt
        "email", "sam@example.com",  # valid pair
        "",                           # blank gateway → finish
    ])
    handles = collect_owner_handles(ask=ask)
    assert handles == [("whatsapp", "555123@lid"), ("email", "sam@example.com")]


def test_collect_owner_handles_skips_blank_address():
    ask = make_ask(["telegram", "", ""])  # gateway, blank address, finish
    assert collect_owner_handles(ask=ask) == []


def test_collect_owner_handles_non_interactive_is_empty():
    assert collect_owner_handles(non_interactive=True) == []


# ── collect_autonomy_env (env-values assembly) ───────────────────────────────


def test_collect_autonomy_env_scripted_answers():
    ask = make_ask([
        "2",            # approval policy → graduated
        "n",            # skill synthesis → false
        "3",            # autonomy preset → autonomous
        "telegram",     # home channel platform
        "123456789",    # home channel id
    ])
    updates = collect_autonomy_env({}, ask=ask)
    assert updates == {
        "COLONY_APPROVAL_POLICY": "graduated",
        "COLONY_ENABLE_SKILL_SYNTHESIS": "false",
        "COLONY_AUTONOMY_PRESET": "autonomous",
        "TELEGRAM_HOME_CHANNEL": "123456789",
    }


def test_collect_autonomy_env_defaults_are_safe():
    # All-blank answers: strict policy, synthesis off, calibration preset
    # (shadow everything — nothing acts live without an earned record).
    updates = collect_autonomy_env({}, ask=make_ask([]))
    assert updates == {
        "COLONY_APPROVAL_POLICY": "strict",
        "COLONY_ENABLE_SKILL_SYNTHESIS": "false",
        "COLONY_AUTONOMY_PRESET": "calibration",
    }
    assert not any(k.endswith("_HOME_CHANNEL") for k in updates)


def test_collect_autonomy_env_rerun_preserves_existing_values():
    existing = {
        "COLONY_APPROVAL_POLICY": "graduated",
        "COLONY_ENABLE_SKILL_SYNTHESIS": "false",
        "COLONY_AUTONOMY_PRESET": "autonomous",
        "WHATSAPP_HOME_CHANNEL": "999@g.us",
    }
    # Accept every default (blank answers).
    updates = collect_autonomy_env(existing, ask=make_ask([]))
    assert updates == {
        "COLONY_APPROVAL_POLICY": "graduated",
        "COLONY_ENABLE_SKILL_SYNTHESIS": "false",
        "COLONY_AUTONOMY_PRESET": "autonomous",
        "WHATSAPP_HOME_CHANNEL": "999@g.us",
    }


def test_collect_autonomy_env_none_drops_home_channel():
    existing = {"WHATSAPP_HOME_CHANNEL": "999@g.us"}
    ask = make_ask(["1", "n", "1", "none"])
    updates = collect_autonomy_env(existing, ask=ask)
    assert "WHATSAPP_HOME_CHANNEL" not in updates
    assert updates["COLONY_APPROVAL_POLICY"] == "strict"
    assert updates["COLONY_AUTONOMY_PRESET"] == "passive"


def test_collect_autonomy_env_invalid_platform_reprompts():
    ask = make_ask(["1", "n", "2", "fax", "discord", "chan-42"])
    updates = collect_autonomy_env({}, ask=ask)
    assert updates["DISCORD_HOME_CHANNEL"] == "chan-42"


def test_collect_autonomy_env_non_interactive_uses_defaults():
    updates = collect_autonomy_env({}, non_interactive=True)
    assert updates["COLONY_APPROVAL_POLICY"] == "strict"
    assert updates["COLONY_ENABLE_SKILL_SYNTHESIS"] == "false"
    assert updates["COLONY_AUTONOMY_PRESET"] == "calibration"
