"""Provenance-based cross-context leak detection: a reply may not surface an entity known
only from a different private conversation. Entities from the current conversation, and
public entities, are never flagged.
"""

import pytest

from colony_sidecar.gate.context_provenance import (
    ContextProvenanceStore,
    ProvenanceCrossContextGuard,
)
from colony_sidecar.gate.response_guard import GuardMode, ResponseGuard
from colony_sidecar.intelligence.relationships.trust_tiers import TrustTier

CONV_A = "rcs:conv-A"
CONV_B = "rcs:conv-B"


@pytest.fixture
def store():
    s = ContextProvenanceStore(":memory:")
    yield s
    s.close()


@pytest.mark.asyncio
async def test_leak_from_another_conversation_is_flagged(store):
    store.record(CONV_A, ["Project Falcon"], contact_id="alice")
    store.record(CONV_B, ["the lunch plan"], contact_id="bob")
    guard = ProvenanceCrossContextGuard(store)

    # replying to B while surfacing something only known from A -> leak
    findings = await guard.check(response_text="", conversation_key=CONV_B,
                                 mentioned_entities=["Project Falcon"])
    assert len(findings) == 1 and findings[0].check == "cross_context"


@pytest.mark.asyncio
async def test_entity_from_this_conversation_is_fine(store):
    store.record(CONV_A, ["Project Falcon"], contact_id="alice")
    guard = ProvenanceCrossContextGuard(store)
    # talking about Falcon IN conversation A (where it belongs) is fine
    findings = await guard.check(response_text="", conversation_key=CONV_A,
                                 mentioned_entities=["Project Falcon"])
    assert findings == []


@pytest.mark.asyncio
async def test_public_entity_never_leaks(store):
    store.record(CONV_A, ["New York"], contact_id="alice", is_public=True)
    guard = ProvenanceCrossContextGuard(store)
    findings = await guard.check(response_text="", conversation_key=CONV_B,
                                 mentioned_entities=["New York"])
    assert findings == []


@pytest.mark.asyncio
async def test_unknown_entity_is_not_a_leak(store):
    guard = ProvenanceCrossContextGuard(store)
    findings = await guard.check(response_text="", conversation_key=CONV_B,
                                 mentioned_entities=["Something Never Seen"])
    assert findings == []


@pytest.mark.asyncio
async def test_no_conversation_key_is_a_noop(store):
    store.record(CONV_A, ["Project Falcon"])
    guard = ProvenanceCrossContextGuard(store)
    assert await guard.check(response_text="", conversation_key=None,
                             mentioned_entities=["Project Falcon"]) == []


@pytest.mark.asyncio
async def test_extractor_pulls_entities_from_response_text(store):
    store.record(CONV_A, ["Project Falcon"], contact_id="alice")

    class FakeCand:
        def __init__(self, name):
            self.name = name

    class FakeExtractor:
        async def extract(self, text, existing_entities=None):
            class R:
                entities = [FakeCand("Project Falcon")] if "falcon" in text.lower() else []
            return R()

    guard = ProvenanceCrossContextGuard(store, extractor=FakeExtractor())
    # entity only in the free text, not passed explicitly -> still caught
    findings = await guard.check(response_text="sure, Project Falcon ships Friday",
                                 conversation_key=CONV_B, mentioned_entities=[])
    assert len(findings) == 1


@pytest.mark.asyncio
async def test_end_to_end_through_response_guard(store):
    store.record(CONV_A, ["Project Falcon"], contact_id="alice")
    guard = ResponseGuard(default_mode=GuardMode.ENFORCE,
                          cross_context=ProvenanceCrossContextGuard(store))
    r = await guard.evaluate(
        response_text="re: Project Falcon", trust_tier=TrustTier.REGULAR,
        target_gateway="rcs", conversation_key=CONV_B,
        mentioned_entities=["Project Falcon"])
    assert r.decision == "revise"
    assert any(f.check == "cross_context" for f in r.findings)

    # same content delivered back into conversation A is allowed
    r2 = await guard.evaluate(
        response_text="re: Project Falcon", trust_tier=TrustTier.REGULAR,
        target_gateway="rcs", conversation_key=CONV_A,
        mentioned_entities=["Project Falcon"])
    assert r2.decision == "allow"
