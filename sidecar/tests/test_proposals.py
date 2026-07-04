"""Tests for the proposals subsystem (thinker/research -> Proposal -> delivery)."""

from __future__ import annotations

from types import SimpleNamespace

from colony_sidecar.proposals import (
    Proposal, ProposalStore, build_from_thinker, build_from_research,
    proposal_to_payload,
)


def test_build_from_thinker_maps_fields():
    init = SimpleNamespace(
        description="Investigate why deploy latency doubled",
        type="research", priority=0.72,
        rationale="[self-directed thinking] latency rose 2x over the last week and nothing is tracking it",
    )
    p = build_from_thinker(init)
    assert p.title.startswith("Investigate why deploy latency")
    assert "latency rose 2x" in p.finding
    assert "[self-directed thinking]" not in p.finding  # prefix stripped
    assert p.suggested_action == init.description
    assert p.why_it_helps  # non-empty benefit framing
    assert p.initiative_type == "research"
    assert abs(p.confidence - 0.72) < 1e-6


def test_proposal_render_is_well_formed():
    p = Proposal(
        title="Renew the SSL cert",
        finding="the cert expires in 6 days",
        why_it_helps="prevents an outage",
        suggested_action="schedule the renewal",
        citations=[{"title": "Cert dashboard", "url": "https://x/y"}],
    )
    r = p.render()
    assert "What I found:" in r and "Why it helps you:" in r
    assert "Suggested next step:" in r and "Sources:" in r


def test_proposal_to_payload_is_dedicated_type():
    p = Proposal(title="X", finding="f", why_it_helps="w", suggested_action="a")
    payload = proposal_to_payload(p)
    assert payload["type"] == "proposal"          # dedicated type
    assert payload["entity_type"] == "proposal"
    assert payload["description"] == p.render()


def test_build_from_research_carries_citations():
    p = build_from_research(
        "What are the top 3 competitors?",
        "Competitor A, B, C lead the market...",
        [{"title": "Report", "url": "https://r"}],
    )
    assert p.source == "research"
    assert p.citations and p.citations[0]["url"] == "https://r"
    assert "Competitor A" in p.finding


def test_store_roundtrip():
    s = ProposalStore(db_path=None)
    p = build_from_thinker(SimpleNamespace(
        description="Do X", type="task", priority=0.5, rationale="because Y"))
    s.add(p)
    assert s.count() == 1
    got = s.list(limit=5)[0]
    assert got.title == "Do X"
    assert s.set_status(p.id, "delivered") is True
    assert s.count(status="delivered") == 1
