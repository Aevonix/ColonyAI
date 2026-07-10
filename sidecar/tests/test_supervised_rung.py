"""H1.1: the generic supervised-live rung (self_model/supervised.py).

Locks: COLONY_SUPERVISED_LIVE_DOMAINS defaults empty (rung off everywhere);
the beliefs legacy alias still works; reversible() fails CLOSED on unknown
domains/ops; effective_mode degrades to the env mode on trust errors and
never upgrades past what stage + flag earn.
"""

import pytest

from colony_sidecar.self_model.supervised import (
    REVERSIBLE_CONTRACT, effective_mode, reversible, supervised_domains,
    supervised_enabled,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("COLONY_SUPERVISED_LIVE_DOMAINS", raising=False)
    monkeypatch.delenv("COLONY_BELIEFS_SUPERVISED_LIVE", raising=False)


# --- flag parsing ------------------------------------------------------------

def test_default_no_domains_supervised():
    """Regression lock: with nothing set, the rung is off for every domain."""
    assert supervised_domains() == frozenset()
    assert not supervised_enabled("beliefs")
    assert not supervised_enabled("world_model")
    assert not supervised_enabled("")


def test_generic_flag_parses_csv(monkeypatch):
    monkeypatch.setenv("COLONY_SUPERVISED_LIVE_DOMAINS", " Beliefs, world_model ,")
    assert supervised_domains() == {"beliefs", "world_model"}
    assert supervised_enabled("beliefs")
    assert supervised_enabled("WORLD_MODEL")
    assert not supervised_enabled("goals")


def test_legacy_beliefs_alias(monkeypatch):
    monkeypatch.setenv("COLONY_BELIEFS_SUPERVISED_LIVE", "1")
    assert supervised_enabled("beliefs")
    assert not supervised_enabled("world_model")   # alias is beliefs-only
    monkeypatch.setenv("COLONY_BELIEFS_SUPERVISED_LIVE", "0")
    assert not supervised_enabled("beliefs")


# --- reversibility contract: fail-closed ---------------------------------------

def test_reversible_contract_fail_closed():
    assert reversible("beliefs", "supersede")
    assert reversible("beliefs", "decay")
    # unlisted op in a known domain: non-reversible
    assert not reversible("beliefs", "delete")
    assert not reversible("beliefs", "merge")
    # unknown domain: non-reversible, whatever the op claims
    assert not reversible("world_model", "supersede")
    assert not reversible("", "decay")
    assert not reversible("beliefs", "")
    # the contract only ever names operations, never wildcards
    assert all("*" not in op for ops in REVERSIBLE_CONTRACT.values() for op in ops)


# --- effective_mode ------------------------------------------------------------

class _Trust:
    def __init__(self, stage):
        self._stage = stage

    def stage(self, domain, default="shadow"):
        return self._stage


class _BrokenTrust:
    def stage(self, domain, default="shadow"):
        raise RuntimeError("trust db unavailable")


def test_env_override_wins():
    assert effective_mode("beliefs", "off", _Trust("act_first")) == "off"
    assert effective_mode("beliefs", "live", _Trust("shadow")) == "live"


def test_no_trust_degrades_to_env_mode():
    assert effective_mode("beliefs", "shadow", None) == "shadow"


def test_trust_error_degrades_to_env_mode(monkeypatch):
    monkeypatch.setenv("COLONY_SUPERVISED_LIVE_DOMAINS", "beliefs")
    assert effective_mode("beliefs", "shadow", _BrokenTrust()) == "shadow"


def test_stage_ladder(monkeypatch):
    # rung off: ask_first stays shadow (the historical catch-22 posture)
    assert effective_mode("beliefs", "shadow", _Trust("ask_first")) == "shadow"
    # rung on: ask_first becomes supervised; act_first is live; shadow stays shadow
    monkeypatch.setenv("COLONY_SUPERVISED_LIVE_DOMAINS", "beliefs")
    assert effective_mode("beliefs", "shadow", _Trust("shadow")) == "shadow"
    assert effective_mode("beliefs", "shadow", _Trust("ask_first")) == "supervised"
    assert effective_mode("beliefs", "shadow", _Trust("act_first")) == "live"
    # the flag only unlocks the listed domain
    assert effective_mode("goals", "shadow", _Trust("ask_first")) == "shadow"


# --- rung visibility (H1.4) -----------------------------------------------------

def test_trust_snapshot_shows_rung(monkeypatch):
    """TrustEngine.snapshot() (surfaced via GET /v1/host/self) carries the
    rung: supervised_enabled + effective_rung per domain."""
    from colony_sidecar.self_model import (
        ActionJournal, CompetenceStore, TrustEngine,
    )
    trust = TrustEngine(CompetenceStore(), journal=ActionJournal())
    trust.set_stage("beliefs", "ask_first", notify=False)
    trust.set_stage("goals", "act_first", notify=False)

    snap = {r["domain"]: r for r in trust.snapshot()}
    assert snap["beliefs"]["supervised_enabled"] is False
    assert snap["beliefs"]["effective_rung"] == "ask_first"

    monkeypatch.setenv("COLONY_SUPERVISED_LIVE_DOMAINS", "beliefs")
    snap = {r["domain"]: r for r in trust.snapshot()}
    assert snap["beliefs"]["supervised_enabled"] is True
    assert snap["beliefs"]["effective_rung"] == "supervised"
    # the rung only ever refines ask_first; other stages pass through
    assert snap["goals"]["effective_rung"] == "act_first"
    assert snap["goals"]["supervised_enabled"] is False
