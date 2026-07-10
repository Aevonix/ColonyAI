"""L2.1 — level-2 eligibility pipeline: ordered, first-fail-wins, fail-closed.

Includes the 'content-never-new' property test: across randomized store
states, no eligible inference can ever render fact text the reader does not
already hold — level 2 adds epistemic topology, never content.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from colony_sidecar.channels.presence import ConversationPresenceStore
from colony_sidecar.tom.eligibility import (
    EligibilityDecision, eligible_inferences, evaluate_inference,
    l2_approval_mode, mutual_window_days)
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.tom2 import Tom2Store, render_inference_for_contact

OWNER = "cid-owner"
READER = "cid-alice"
SUBJECT = "cid-bob"
CONV = "dm:alice"


class FakeContacts:
    def __init__(self, tiers):
        self._tiers = dict(tiers)

    async def get(self, contact_id):
        tier = self._tiers.get(contact_id)
        return None if tier is None else SimpleNamespace(
            contact_id=contact_id, trust_tier=tier)


class World:
    """A world where every check passes for (READER, SUBJECT)."""

    def __init__(self):
        self.facts = SharedFactsStore(":memory:")
        self.tom2 = Tom2Store()
        self.presence = ConversationPresenceStore()
        self.contacts = FakeContacts({READER: "trusted", SUBJECT: "regular",
                                      OWNER: "inner_circle"})
        self.fact = self.facts.create_fact(
            contact_id=READER, fact="the launch moved to friday",
            confidence=0.9)
        self.tom2.record_inference(contact_id=SUBJECT, kind="unaware_of",
                                   fact_ref=self.fact["id"], confidence=0.4)
        self.inference = self.tom2.list_inferences(contact_id=SUBJECT)[0]
        # reader + subject demonstrably know each other (another room)
        self.presence.record("dm:lobby", READER, method="handle")
        self.presence.record("dm:lobby", SUBJECT, method="handle")
        # the reader is alone with the owner in CONV
        self.presence.record(CONV, READER, method="handle")

    async def evaluate(self, inference=None, **over):
        kw = dict(reader_contact_id=READER, conversation_key=CONV,
                  facts_store=self.facts, contacts_store=self.contacts,
                  presence_store=self.presence, owner_id=OWNER,
                  approval_check=lambda r, s: True,
                  budget_check=lambda r, s, f: True)
        kw.update(over)
        return await evaluate_inference(
            inference if inference is not None else self.inference, **kw)


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    monkeypatch.delenv("COLONY_TOM2_L2_APPROVAL", raising=False)
    monkeypatch.delenv("COLONY_TOM2_MUTUAL_WINDOW_DAYS", raising=False)
    return World()


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

def test_approval_mode_default_required(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_L2_APPROVAL", raising=False)
    assert l2_approval_mode() == "required"
    monkeypatch.setenv("COLONY_TOM2_L2_APPROVAL", "off")
    assert l2_approval_mode() == "off"
    monkeypatch.setenv("COLONY_TOM2_L2_APPROVAL", "banana")
    assert l2_approval_mode() == "required"       # unknown => fail closed


def test_mutual_window_default(monkeypatch):
    monkeypatch.delenv("COLONY_TOM2_MUTUAL_WINDOW_DAYS", raising=False)
    assert mutual_window_days() == 30.0
    monkeypatch.setenv("COLONY_TOM2_MUTUAL_WINDOW_DAYS", "junk")
    assert mutual_window_days() == 30.0


# ---------------------------------------------------------------------------
# The happy path, then each check failing alone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_checks_pass(world):
    d = await world.evaluate()
    assert d.eligible is True
    assert d.failed_check is None
    assert d.checks_passed == ["subject-scope", "subject-owner",
                               "subject-present", "ref-visibility",
                               "mutual-knowledge", "tier-floors",
                               "approval", "budget"]


@pytest.mark.asyncio
async def test_subject_scope_failures(world):
    assert (await world.evaluate({})).failed_check == "subject-scope"
    d = await world.evaluate(dict(world.inference, contact_id=READER))
    assert d.failed_check == "subject-scope"
    d = await world.evaluate(dict(world.inference, contact_id="cid-ghost"))
    assert d.failed_check == "subject-scope"      # unresolvable subject
    d = await world.evaluate(contacts_store=None)
    assert d.failed_check == "subject-scope"
    d = await world.evaluate(reader_contact_id="system")
    assert d.failed_check == "subject-scope"


@pytest.mark.asyncio
async def test_subject_owner_excluded(world):
    d = await world.evaluate(dict(world.inference, contact_id=OWNER))
    assert d.failed_check == "subject-owner"
    d = await world.evaluate(owner_id="")          # owner unknown => closed
    assert d.failed_check == "subject-owner"


@pytest.mark.asyncio
async def test_subject_present_excluded(world):
    world.presence.record(CONV, SUBJECT, method="handle")
    d = await world.evaluate()
    assert d.failed_check == "subject-present"


@pytest.mark.asyncio
async def test_subject_present_fails_closed(world):
    d = await world.evaluate(presence_store=None)
    assert d.failed_check == "subject-present"

    class Broken:
        def is_present(self, *a, **k):
            raise RuntimeError("io")

    d = await world.evaluate(presence_store=Broken())
    assert d.failed_check == "subject-present"


@pytest.mark.asyncio
async def test_ref_visibility_delegates_to_h35(world, monkeypatch):
    # master flag off => the H3.5 gate refuses => ineligible
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "0")
    d = await world.evaluate()
    assert d.failed_check == "ref-visibility"
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    # a fact the reader does not own => refused
    foreign = world.facts.create_fact(contact_id="cid-carol",
                                      fact="carol's private thing",
                                      confidence=0.9)
    world.tom2.record_inference(contact_id=SUBJECT, kind="unaware_of",
                                fact_ref=foreign["id"], confidence=0.4)
    row = [r for r in world.tom2.list_inferences(contact_id=SUBJECT)
           if r["fact_ref"] == foreign["id"]][0]
    d = await world.evaluate(row)
    assert d.failed_check == "ref-visibility"


@pytest.mark.asyncio
async def test_mutual_knowledge_required(world):
    fresh = ConversationPresenceStore()          # no shared sighting
    fresh.record(CONV, READER, method="handle")
    d = await world.evaluate(presence_store=fresh)
    assert d.failed_check == "mutual-knowledge"


@pytest.mark.asyncio
async def test_tier_floors(world):
    world.contacts._tiers[READER] = "regular"     # reader below trusted
    d = await world.evaluate()
    assert d.failed_check == "tier-floors"
    world.contacts._tiers[READER] = "trusted"
    world.contacts._tiers[SUBJECT] = "peripheral"  # subject below regular
    d = await world.evaluate()
    assert d.failed_check == "tier-floors"


@pytest.mark.asyncio
async def test_approval_required_by_default(world):
    d = await world.evaluate(approval_check=None)
    assert d.failed_check == "approval"
    d = await world.evaluate(approval_check=lambda r, s: False)
    assert d.failed_check == "approval"

    def boom(r, s):
        raise RuntimeError("approvals db down")

    d = await world.evaluate(approval_check=boom)
    assert d.failed_check == "approval"


@pytest.mark.asyncio
async def test_approval_off_skips_hook(world, monkeypatch):
    monkeypatch.setenv("COLONY_TOM2_L2_APPROVAL", "off")
    d = await world.evaluate(approval_check=None)
    assert d.eligible is True
    assert "approval" in d.checks_passed


@pytest.mark.asyncio
async def test_budget_hook_required(world):
    d = await world.evaluate(budget_check=None)
    assert d.failed_check == "budget"
    d = await world.evaluate(budget_check=lambda r, s, f: False)
    assert d.failed_check == "budget"


@pytest.mark.asyncio
async def test_async_hooks_supported(world):
    async def yes(*a):
        return True

    d = await world.evaluate(approval_check=yes, budget_check=yes)
    assert d.eligible is True


@pytest.mark.asyncio
async def test_first_fail_wins(world):
    """Subject is the owner AND present: the pipeline reports the FIRST
    failing check, deterministically."""
    world.presence.record(CONV, OWNER, method="handle")
    d = await world.evaluate(dict(world.inference, contact_id=OWNER))
    assert d.failed_check == "subject-owner"


@pytest.mark.asyncio
async def test_internal_error_is_ineligible(world):
    class ExplodingContacts:
        async def get(self, cid):
            raise RuntimeError("db on fire")

    d = await world.evaluate(contacts_store=ExplodingContacts())
    assert d.eligible is False
    assert d.failed_check == "error"


@pytest.mark.asyncio
async def test_eligible_inferences_filters_and_limits(world):
    rows = world.tom2.list_inferences(limit=100)
    out = await eligible_inferences(
        rows, limit=5, reader_contact_id=READER, conversation_key=CONV,
        facts_store=world.facts, contacts_store=world.contacts,
        presence_store=world.presence, owner_id=OWNER,
        approval_check=lambda r, s: True, budget_check=lambda r, s, f: True)
    assert [r["id"] for r in out] == [world.inference["id"]]
    out = await eligible_inferences(
        rows, limit=0, reader_contact_id=READER, conversation_key=CONV,
        facts_store=world.facts, contacts_store=world.contacts,
        presence_store=world.presence, owner_id=OWNER,
        approval_check=lambda r, s: True, budget_check=lambda r, s, f: True)
    assert out == []


# ---------------------------------------------------------------------------
# PROPERTY: content-never-new
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_property_content_never_new(monkeypatch):
    """Across randomized store states, an eligible inference's rendered line
    never contains fact text the reader does not already hold. Level 2 is
    epistemic topology over the reader's OWN facts — by construction."""
    monkeypatch.setenv("COLONY_TOM2_CROSS_CONTEXT", "1")
    monkeypatch.setenv("COLONY_TOM2_L2_APPROVAL", "off")
    rng = random.Random(20260709)
    cids = [f"cid-p{i}" for i in range(5)]

    for trial in range(25):
        facts = SharedFactsStore(":memory:")
        tom2 = Tom2Store()
        presence = ConversationPresenceStore()
        contacts = FakeContacts({cid: "trusted" for cid in cids})
        for cid in cids:                          # everyone knows everyone
            presence.record("dm:lobby", cid, method="handle")

        texts_by_owner = {}
        all_facts = []
        for cid in cids:
            for j in range(rng.randint(1, 3)):
                text = f"secret {cid} number {j} trial {trial}"
                row = facts.create_fact(contact_id=cid, fact=text,
                                        confidence=0.9)
                texts_by_owner.setdefault(cid, set()).add(text)
                all_facts.append(row)

        for _ in range(12):                       # random topology
            f = rng.choice(all_facts)
            ev = [e["id"] for e in rng.sample(all_facts, rng.randint(0, 2))]
            tom2.record_inference(
                contact_id=rng.choice(cids),
                kind=rng.choice(["knows", "unaware_of"]),
                fact_ref=f["id"], evidence_refs=ev, confidence=0.5)

        reader = rng.choice(cids)
        conv = f"dm:room{trial}"
        presence.record(conv, reader, method="handle")
        rows = tom2.list_inferences(limit=200)
        eligible = await eligible_inferences(
            rows, limit=200, reader_contact_id=reader,
            conversation_key=conv, facts_store=facts,
            contacts_store=contacts, presence_store=presence,
            owner_id="cid-the-owner", budget_check=lambda r, s, f: True)

        reader_texts = texts_by_owner.get(reader, set())
        foreign_texts = {t for cid, ts in texts_by_owner.items()
                         if cid != reader for t in ts}
        for row in eligible:
            line = render_inference_for_contact(row, facts, reader)
            assert line is not None               # eligible => renderable
            for foreign in foreign_texts:
                assert foreign not in line        # NEVER new content
            assert any(t in line for t in reader_texts)
