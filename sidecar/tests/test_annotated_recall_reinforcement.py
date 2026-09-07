"""Annotation packet identities must not replace graph reinforcement targets."""

import asyncio

import pytest

from test_recall_ranking import RecallFixture, _node


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "belief"])
async def test_annotated_belief_reinforces_underlying_graph_memory(kind):
    fixture = RecallFixture([], [_node("underlying-belief")])
    packet = {
        "id": "annotated:packet-id",
        "_recall_memory_id": "underlying-belief",
        "atomic_evidence": True,
        "epistemic_state": "correction_evidence",
    }
    if kind is not None:
        packet["kind"] = kind

    # Exercise retained background dispatch through touch_memory to the driver,
    # rather than mocking the method that selects the graph update target.
    fixture.graph.record_recall_use([packet])
    await asyncio.gather(*fixture.graph._bg_tasks)

    assert len(fixture.queries) == 1
    query, parameters = fixture.queries[0]
    assert "MATCH (m:Memory {id: $memory_id})" in query
    assert "m.recalls = coalesce(m.recalls, 0) + 1" in query
    assert parameters == {"memory_id": "underlying-belief"}
    assert packet["id"] == "annotated:packet-id"


@pytest.mark.asyncio
async def test_plain_belief_keeps_its_existing_reinforcement_target():
    fixture = RecallFixture([], [_node("plain-belief")])
    fixture.graph.record_recall_use([{"id": "plain-belief", "kind": "belief"}])
    await asyncio.gather(*fixture.graph._bg_tasks)

    assert [parameters for _, parameters in fixture.queries] == [
        {"memory_id": "plain-belief"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source_quote", "media_description"])
async def test_source_evidence_never_reinforces_graph_memory(kind):
    fixture = RecallFixture([], [_node("underlying-belief")])
    fixture.graph.record_recall_use([{
        "id": "annotated:source-packet",
        "_recall_memory_id": "underlying-belief",
        "kind": kind,
    }])
    await asyncio.gather(*getattr(fixture.graph, "_bg_tasks", ()))

    assert fixture.queries == []
