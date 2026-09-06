"""P8 server, authenticated context, read-model, and lifecycle wiring."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest
from starlette.requests import Request

from colony_sidecar.api.authority import (
    RequestAuthority,
    anonymous_authority,
    legacy_authority,
    required_scope,
)
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.api.schemas.host import (
    ContextAssembleRequest,
    EnrichedContextRequest,
    HostIdentity,
    HostMessage,
    HostTurnContext,
    MultimodalSearchRequest,
    ReasoningTurnRequest,
    SharedFactCreateRequest,
    SharedFactUpdateRequest,
    ToolInvokeRequest,
)
from colony_sidecar.server import (
    _attach_p8_runtime,
    _build_research_pipeline,
)
from colony_sidecar.intelligence.relationships.profiler import (
    RelationshipProfiler,
)
from colony_sidecar.tom.facts import SharedFactsStore
from colony_sidecar.tom.integration import P8Runtime
from colony_sidecar.tom.leveled import render_level1
from colony_sidecar.tom.tom2 import Tom2Store


P8_FILES = (
    "colony-p8-visibility.db",
    "colony-p8-arcs.db",
    "colony-p8-recipient-audit.db",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _authority(
    person: str = "alice",
    *,
    principal: str = "hermes-text",
    person_ids: tuple[str, ...] = (),
    scopes: tuple[str, ...] = ("context:read", "tom:read"),
) -> RequestAuthority:
    return RequestAuthority(
        principal_id=principal,
        credential_id="current",
        scopes=frozenset(scopes),
        viewer_person_id=person,
        person_ids=frozenset((person, *person_ids)),
        audiences=frozenset(("viewer",)),
        authenticated=True,
    )


def _request(authority: RequestAuthority) -> Request:
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
        "scheme": "http",
    })
    request.state.colony_authority = authority
    return request


def _context(person: str) -> ContextAssembleRequest:
    return ContextAssembleRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(
            contact_id=person,
            session_id="session:1",
            channel_id="body-claimed-channel",
        ),
        incoming_message=HostMessage(role="user", content="hello"),
    )


@pytest.fixture(autouse=True)
def _restore_host_globals():
    names = (
        "_p8_runtime", "_facts_store", "_graph", "_tom2_store",
        "_relationship_profiler", "_embedder", "_goals_store",
        "_initiative_store", "_briefings_engine", "_world_store",
        "_directive_manager", "_surprise_store", "_contacts_store",
        "_connection_discoverer", "_metalearner", "_commitment_store",
        "_preference_learner", "_affect_store", "_engagement_store",
        "_comms_log", "_tool_executor", "_reasoning_loop",
    )
    originals = {name: getattr(host, name, None) for name in names}
    yield
    for name, value in originals.items():
        setattr(host, name, value)


class _LegacyGlobalContextSpies:
    """Content-bearing legacy sources without a P8 visibility envelope."""

    def __init__(self):
        self.calls = {
            name: 0 for name in (
                "goals", "initiatives", "briefings", "world",
                "directive_ack", "directive_pending", "directive_brief",
                "surprises", "insights", "contacts_list", "cognition",
            )
        }

        owner_goal = SimpleNamespace(
            priority=SimpleNamespace(name="HIGH"),
            title="owner-global-goal",
            description="owner-global-goal-description",
            progress_pct=0.5,
            status=SimpleNamespace(value="active"),
        )

        outer = self

        class Goals:
            def list_goals(self, *args, **kwargs):
                outer.calls["goals"] += 1
                return [owner_goal]

        class Initiatives:
            def list(self, *args, **kwargs):
                outer.calls["initiatives"] += 1
                return [SimpleNamespace(
                    type="research",
                    description="owner-global-initiative",
                    priority=0.8,
                )]

        class Briefings:
            def get_recent(self, *args, **kwargs):
                outer.calls["briefings"] += 1
                return [{
                    "title": "owner-global-briefing",
                    "body": "owner-global-briefing-body",
                }]

        class Directives:
            def consume_ack(self):
                outer.calls["directive_ack"] += 1
                return "owner-global-directive-ack"

            def pending_confirmation(self):
                outer.calls["directive_pending"] += 1
                return "owner-global-directive-pending"

            def context_brief(self):
                outer.calls["directive_brief"] += 1
                return "owner-global-directive-brief"

        class Surprises:
            def get_unresolved(self, *args, **kwargs):
                outer.calls["surprises"] += 1
                return [{
                    "surprise_score": 0.9,
                    "observation": "owner-global-surprise",
                }]

        class Contacts:
            async def get(self, _contact_id):
                return None

            async def get_style(self, _contact_id):
                return {}

            async def list(self):
                outer.calls["contacts_list"] += 1
                return [{
                    "contact_id": "owner-private-contact",
                    "display_name": "owner-global-contact-list",
                    "trust_tier": "trusted",
                }]

            async def compute_cadence_overdue(self, **_kwargs):
                return []

        class Insights:
            async def discover_connections(self, *args, **kwargs):
                outer.calls["insights"] += 1
                return [SimpleNamespace(
                    novelty=0.9,
                    description="owner-global-insight",
                )]

        class Cognition:
            async def evaluate(self):
                outer.calls["cognition"] += 1
                return SimpleNamespace(overall=0.9)

        self.goals = Goals()
        self.initiatives = Initiatives()
        self.briefings = Briefings()
        self.directives = Directives()
        self.surprises = Surprises()
        self.contacts = Contacts()
        self.insights = Insights()
        self.cognition = Cognition()

    def wire(self, monkeypatch):
        host._goals_store = self.goals
        host._initiative_store = self.initiatives
        host._briefings_engine = self.briefings
        host._world_store = object()
        host._directive_manager = self.directives
        host._surprise_store = self.surprises
        host._contacts_store = self.contacts
        host._connection_discoverer = self.insights
        host._metalearner = self.cognition

        async def world_context(_query, limit=5):
            self.calls["world"] += 1
            return [{
                "name": "owner-global-world-entity",
                "entity_type": "concept",
            }]

        monkeypatch.setattr(host, "_world_context_entities", world_context)


class _PersonalContextSpies:
    """Exact-person stores that must never see an unsealed P8 selector."""

    def __init__(self):
        self.calls = {
            name: [] for name in (
                "graph", "commitment_list", "commitment_overdue",
                "commitment_pending", "contact_get", "contact_style",
                "contact_cadence", "affect", "engagement", "comms",
                "relationship_profile",
            )
        }
        outer = self

        class Graph:
            async def recall(self, **kwargs):
                outer.calls["graph"].append(kwargs)
                return [{
                    "content": "PERSONAL OWNER MEMORY",
                    "relevance": 0.99,
                    "score": 0.99,
                    "source_uri": "session:private",
                }]

        class Commitments:
            def list(self, **kwargs):
                outer.calls["commitment_list"].append(kwargs)
                return {"commitments": [{
                    "id": "commitment-private",
                    "person_id": kwargs.get("person_id"),
                    "description": "PERSONAL OWNER COMMITMENT",
                    "status": "pending",
                }]}

            def get_overdue(self):
                outer.calls["commitment_overdue"].append(True)
                return [{
                    "id": "overdue-private",
                    "person_id": "owner",
                    "description": "PERSONAL OWNER OVERDUE",
                    "status": "overdue",
                }]

            def get_pending_for_person(self, person_id):
                outer.calls["commitment_pending"].append(person_id)
                return [{
                    "description": "PERSONAL OWNER PENDING",
                    "priority": "high",
                    "due_at": None,
                }]

        class ContactRecord:
            display_name = "Private Person"
            given_name = "Private"
            timezone = "UTC"
            last_interaction_at = None
            relationship_score = 0.9
            trust_tier = "trusted"

            def get(self, key, default=None):
                return {
                    "trust_tier": self.trust_tier,
                    "style_notes": "PERSONAL OWNER STYLE",
                }.get(key, default)

        class Contacts:
            async def get(self, person_id):
                outer.calls["contact_get"].append(person_id)
                return ContactRecord()

            async def get_style(self, person_id):
                outer.calls["contact_style"].append(person_id)
                return {"tone": "PERSONAL OWNER STYLE"}

            async def compute_cadence_overdue(self, **kwargs):
                outer.calls["contact_cadence"].append(kwargs)
                return []

        class Affect:
            def get_state(self, person_id):
                outer.calls["affect"].append(person_id)
                return {
                    "event_count": 1,
                    "current_valence": 0.4,
                    "current_arousal": 0.5,
                    "valence": 0.4,
                    "arousal": 0.5,
                    "trend": "stable",
                }

        class Engagement:
            def get_profile(self, person_id):
                outer.calls["engagement"].append(person_id)
                return None

        class Comms:
            def last_per_channel(self, person_id):
                outer.calls["comms"].append(("per", person_id))
                return {"rcs": {"ts": "2026-07-12"}}

            def last_outbound(self, person_id):
                outer.calls["comms"].append(("outbound", person_id))
                return None

        class Brief:
            def render(self):
                return "PERSONAL OWNER RELATIONSHIP PROFILE"

        class Profiler:
            def cached(self, person_id, **kwargs):
                outer.calls["relationship_profile"].append(
                    (person_id, kwargs))
                return Brief()

        self.graph = Graph()
        self.commitments = Commitments()
        self.contacts = Contacts()
        self.affect = Affect()
        self.engagement = Engagement()
        self.comms = Comms()
        self.profiler = Profiler()

    def wire(self):
        host._graph = self.graph
        host._commitment_store = self.commitments
        host._contacts_store = self.contacts
        host._affect_store = self.affect
        host._engagement_store = self.engagement
        host._comms_log = self.comms
        host.set_relationship_profiler(self.profiler)


def _enriched_request(person: str) -> EnrichedContextRequest:
    return EnrichedContextRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(
            contact_id=person,
            session_id="session:1",
            channel_id="body-claimed-channel",
        ),
        message="hello",
        features={
            "goals": True,
            "worldModel": True,
            "insights": True,
            "briefings": True,
            "contactsList": True,
            "cognition": True,
            "surprises": True,
        },
    )


@pytest.mark.asyncio
async def test_p8_non_owner_never_queries_untyped_global_context(
    tmp_path, monkeypatch,
):
    """Reproduce the legacy-global P8 leak before adding its boundary."""

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)

    assembled_body = _context("alice")
    assembled_body.include_initiatives = True
    assembled_body.projection_policy = "scoped_viewer_required"
    assembled = await host.context_assemble(
        assembled_body, request=_request(_authority("alice")))
    enriched = await host.enriched_context(
        _enriched_request("alice"), request=_request(_authority("alice")))

    rendered = repr((assembled, enriched))
    assert "owner-global" not in rendered
    assert all(count == 0 for count in spies.calls.values())
    projection = assembled.projection_attestation
    assert projection is not None
    assert projection.viewer_person_id == "alice"
    assert projection.viewer_attested is True
    assert projection.viewer_is_owner is False
    assert projection.p8_mode == "shadow"
    assert projection.scoped_projection_ready is True
    assert projection.legacy_global_allowed is False


@pytest.mark.asyncio
async def test_p8_unsealed_owner_claim_never_queries_untyped_global_context(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)
    legacy_request = _request(legacy_authority())

    assembled_body = _context("owner")
    assembled_body.include_initiatives = True
    assembled = await host.context_assemble(
        assembled_body, request=legacy_request)
    enriched = await host.enriched_context(
        _enriched_request("owner"), request=legacy_request)

    assert "owner-global" not in repr((assembled, enriched))
    assert all(count == 0 for count in spies.calls.values())


@pytest.mark.asyncio
async def test_p8_exact_owner_retains_untyped_global_context(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)
    owner_request = _request(_authority("owner"))

    assembled_body = _context("owner")
    assembled_body.include_initiatives = True
    assembled = await host.context_assemble(
        assembled_body, request=owner_request)
    enriched = await host.enriched_context(
        _enriched_request("owner"), request=owner_request)

    rendered = repr((assembled, enriched))
    for marker in (
        "owner-global-goal", "owner-global-initiative",
        "owner-global-briefing", "owner-global-world-entity",
        "owner-global-directive", "owner-global-surprise",
        "owner-global-insight", "owner-global-contact-list",
    ):
        assert marker in rendered
    assert all(count > 0 for count in spies.calls.values())
    projection = assembled.projection_attestation
    assert projection is not None
    assert projection.viewer_person_id == "owner"
    assert projection.viewer_is_owner is True
    assert projection.p8_mode == "shadow"
    assert projection.scoped_projection_ready is True
    assert projection.legacy_global_allowed is True


@pytest.mark.asyncio
async def test_p8_off_scoped_guest_uses_canonical_projection_without_global_producers(
    monkeypatch,
):
    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    host.set_p8_runtime(None)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)
    alice_request = _request(_authority("alice"))

    assembled_body = _context("alice")
    assembled_body.include_initiatives = True
    assembled_body.projection_policy = "scoped_viewer_required"
    response = await host.context_assemble(assembled_body, request=alice_request)
    assert response.projection_attestation.projection_backend == "canonical_sources"
    assert response.projection_attestation.scoped_projection_ready
    assert response.projection_attestation.p8_mode == "off"
    assert not response.projection_attestation.legacy_global_allowed
    assert "owner-global" not in repr(response)
    assert all(count == 0 for count in spies.calls.values())


@pytest.mark.asyncio
async def test_p8_off_exact_owner_keeps_legacy_global_context(monkeypatch):
    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    host.set_p8_runtime(None)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)

    assembled_body = _context("owner")
    assembled_body.include_initiatives = True
    assembled = await host.context_assemble(
        assembled_body, request=_request(_authority("owner")))

    assert "owner-global" in repr(assembled)
    assert spies.calls["goals"] > 0
    assert spies.calls["directive_ack"] > 0


@pytest.mark.asyncio
async def test_p8_off_legacy_migration_keeps_historical_context(monkeypatch):
    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
    host.set_p8_runtime(None)
    spies = _LegacyGlobalContextSpies()
    spies.wire(monkeypatch)

    assembled_body = _context("alice")
    assembled_body.include_initiatives = True
    assembled = await host.context_assemble(
        assembled_body, request=_request(legacy_authority()))

    assert "owner-global" in repr(assembled)
    assert spies.calls["goals"] > 0
    assert spies.calls["directive_ack"] > 0


@pytest.mark.asyncio
async def test_p8_temporal_keeps_global_owner_heads_up_owner_only(
    tmp_path, monkeypatch,
):
    calls = {"commitments": 0, "cadence": 0}

    class Commitments:
        def get_overdue(self):
            calls["commitments"] += 1
            return [{
                "description": "owner-global-overdue",
                "due_at": _now(),
            }]

    class Contacts:
        async def get(self, _contact_id):
            return None

        async def compute_cadence_overdue(self, **_kwargs):
            calls["cadence"] += 1
            return [{
                "name": "owner-global-cadence",
                "days_since": 10,
                "cadence_days": 3,
            }]

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    host._commitment_store = Commitments()
    host._contacts_store = Contacts()

    guest = await host.context_temporal(
        contact_id="alice", request=_request(_authority("alice")))
    assert "owner-global" not in guest["body"]
    assert calls == {"commitments": 0, "cadence": 0}

    owner = await host.context_temporal(
        contact_id="owner", request=_request(_authority("owner")))
    assert "owner-global-overdue" in owner["body"]
    assert "owner-global-cadence" in owner["body"]
    assert calls == {"commitments": 1, "cadence": 1}

    host.set_p8_runtime(None)
    legacy = await host.context_temporal(
        contact_id="alice", request=_request(_authority("alice")))
    assert "owner-global-overdue" in legacy["body"]
    assert "owner-global-cadence" in legacy["body"]
    assert calls == {"commitments": 2, "cadence": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["", "   ", "owner", "alice"])
async def test_p8_unsealed_selector_never_queries_person_context(
    tmp_path, monkeypatch, selector,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _PersonalContextSpies()
    spies.wire()
    legacy_request = _request(legacy_authority())

    assembled = await host.context_assemble(
        _context(selector), request=legacy_request)
    enriched = await host.enriched_context(
        _enriched_request(selector), request=legacy_request)
    temporal = await host.context_temporal(
        contact_id=selector, request=legacy_request)

    assert "PERSONAL OWNER" not in repr((assembled, enriched, temporal))
    assert all(not calls for calls in spies.calls.values())


@pytest.mark.asyncio
async def test_p8_scoped_non_owner_queries_only_exact_person_commitments(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _PersonalContextSpies()
    spies.wire()
    request = _request(_authority("alice"))

    assembled = await host.context_assemble(
        _context("alice"), request=request)
    enriched = await host.enriched_context(
        _enriched_request("alice"), request=request)

    assert spies.calls["graph"] and all(
        call["person_id"] == "alice" for call in spies.calls["graph"])
    assert spies.calls["commitment_list"]
    assert all(call == {
        "person_id": "alice",
        "status": ["pending", "overdue"],
        "limit": 5,
    } for call in spies.calls["commitment_list"])
    assert spies.calls["commitment_pending"] == ["alice"]
    assert spies.calls["commitment_overdue"] == []
    assert "PERSONAL OWNER COMMITMENT" in repr(assembled)
    assert "PERSONAL OWNER PENDING" in repr(enriched)


@pytest.mark.asyncio
async def test_p8_guest_comms_uses_neutral_owner_label(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_OWNER_NAME", "PRIVATE OWNER NAME")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    spies = _PersonalContextSpies()
    spies.wire()

    response = await host.context_assemble(
        _context("alice"), request=_request(_authority("alice")))
    comms = next(
        section for section in response.sections
        if section.id == "colony-comms-landscape"
    )
    assert "PRIVATE OWNER NAME" not in comms.body
    assert "owner approval" in comms.body.lower()


@pytest.mark.asyncio
async def test_p8_off_preserves_blank_person_legacy_queries(
    monkeypatch,
):
    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
    host.set_p8_runtime(None)
    spies = _PersonalContextSpies()
    spies.wire()
    legacy_request = _request(legacy_authority())

    await host.context_assemble(_context(""), request=legacy_request)
    await host.enriched_context(
        _enriched_request(""), request=legacy_request)

    assert len(spies.calls["graph"]) == 2
    assert spies.calls["graph"][0]["person_id"] == ""
    assert spies.calls["graph"][1]["person_id"] is None
    assert spies.calls["commitment_list"][0]["person_id"] == ""
    assert spies.calls["commitment_overdue"]


def test_temporal_context_uses_context_read_scope():
    assert required_scope(
        "GET", "/v1/host/context/temporal") == "context:read"
    assert required_scope(
        "GET", "/v1/host/context/projection-readiness") == "context:read"


class _ToolDirectives:
    def __init__(self, *, allowed=True, explode=False):
        self.allowed = allowed
        self.explode = explode
        self.calls = []

    def check(self, action):
        self.calls.append(action)
        if self.explode:
            raise RuntimeError("directive dependency unavailable")
        return SimpleNamespace(
            allowed=self.allowed,
            reason="ok" if self.allowed else "standing owner boundary",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("directives", [
    _ToolDirectives(allowed=False),
    _ToolDirectives(explode=True),
])
async def test_tool_executor_blocks_mutation_on_boundary_or_guard_failure(
    directives,
):
    from colony_sidecar.reasoning.executor import ToolExecutor

    calls = []

    async def mutate(arguments):
        calls.append(arguments)
        return "MUTATED"

    executor = ToolExecutor(handlers={"write_file": mutate})
    executor.configure_execution_policy(
        directive_manager=directives,
        boundary_required=True,
    )
    result = await executor.execute_batch([{
        "id": "call-1",
        "name": "write_file",
        "arguments": {"path": "x", "content": "secret"},
    }])

    assert calls == []
    assert result[0]["executed"] is False
    assert result[0]["error"] in {
        "tool_boundary_denied", "tool_boundary_unavailable",
    }
    assert len(directives.calls) == 1


@pytest.mark.asyncio
async def test_p8_direct_tool_requires_sealed_owner_mutation_authority(
    tmp_path, monkeypatch,
):
    from colony_sidecar.reasoning.executor import ToolExecutor

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    directives = _ToolDirectives(allowed=True)
    host._directive_manager = directives
    calls = []

    async def mutate(arguments):
        calls.append(arguments)
        return "MUTATED"

    executor = ToolExecutor(handlers={"write_file": mutate})
    executor.configure_execution_policy(
        directive_manager=directives,
        boundary_required=True,
    )
    host._tool_executor = executor
    body = ToolInvokeRequest(
        identity=HostIdentity(host_id="hermes"),
        name="write_file",
        arguments={"path": "x", "content": "secret"},
    )

    with pytest.raises(HTTPException) as guest_denied:
        await host.tools_invoke(
            body, request=_request(_authority(
                "alice", scopes=("api:access", "tools:mutate"))))
    assert guest_denied.value.status_code == 403
    with pytest.raises(HTTPException) as owner_scope_denied:
        await host.tools_invoke(
            body, request=_request(_authority(
                "owner", scopes=("api:access",))))
    assert owner_scope_denied.value.status_code == 403
    assert calls == []

    response = await host.tools_invoke(
        body, request=_request(_authority(
            "owner", scopes=("api:access", "tools:mutate"))))
    assert response.result == "MUTATED"
    assert calls == [{"path": "x", "content": "secret"}]
    assert directives.calls


@pytest.mark.asyncio
async def test_p8_non_owner_retains_public_read_tool_only(tmp_path, monkeypatch):
    from colony_sidecar.reasoning.executor import ToolExecutor

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    directives = _ToolDirectives(allowed=True)
    host._directive_manager = directives

    async def calculate(arguments):
        return str(arguments["expression"])

    async def private_read(_arguments):
        raise AssertionError("private read must not execute for a guest")

    executor = ToolExecutor(handlers={
        "calculate": calculate,
        "read_file": private_read,
    })
    executor.configure_execution_policy(
        directive_manager=directives,
        boundary_required=True,
    )
    host._tool_executor = executor
    request = _request(_authority("alice", scopes=("api:access",)))

    public = await host.tools_invoke(ToolInvokeRequest(
        identity=HostIdentity(host_id="hermes"),
        name="calculate",
        arguments={"expression": "2+3"},
    ), request=request)
    assert public.result == "2+3"

    with pytest.raises(HTTPException) as private_denied:
        await host.tools_invoke(ToolInvokeRequest(
            identity=HostIdentity(host_id="hermes"),
            name="read_file",
            arguments={"path": "owner.txt"},
        ), request=request)
    assert private_denied.value.status_code == 403


@pytest.mark.asyncio
async def test_p8_model_tool_batch_cannot_call_filtered_mutation(
    tmp_path, monkeypatch,
):
    from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor

    class Model:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            if len(self.calls) == 1:
                function = SimpleNamespace(
                    name="write_file",
                    arguments=json.dumps({
                        "path": "x", "content": "secret",
                    }),
                )
                raw = SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(tool_calls=[SimpleNamespace(
                        id="malicious-call", function=function,
                    )]),
                )])
                return SimpleNamespace(raw=raw, content="", usage={})
            raw = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(tool_calls=[]),
            )])
            return SimpleNamespace(raw=raw, content="done", usage={})

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    directives = _ToolDirectives(allowed=True)
    calls = []

    async def mutate(arguments):
        calls.append(arguments)
        return "MUTATED"

    executor = ToolExecutor(handlers={"write_file": mutate})
    executor.configure_execution_policy(
        directive_manager=directives,
        boundary_required=True,
    )
    model = Model()
    host._reasoning_loop = ReasoningLoop(model=model, tools=executor)
    host._tool_executor = executor

    response = await host.reasoning_turn(ReasoningTurnRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(
            contact_id="alice",
            session_id="session:1",
            channel_id="channel:1",
        ),
        messages=[HostMessage(role="user", content="please mutate")],
        available_tools=["write_file"],
    ), request=_request(_authority("alice", scopes=("api:access",))))

    assert response.status == "completed"
    assert calls == []
    assert model.calls[0][1]["tools"] is None
    assert "tool_not_authorized" in repr(model.calls[1][0])


@pytest.mark.asyncio
async def test_tool_executor_rejects_malformed_mutation_verdict():
    from colony_sidecar.reasoning.executor import ToolExecutor

    class MalformedDirectives:
        def check(self, _action):
            return SimpleNamespace(allowed="false", reason="not a boolean")

    calls = []

    async def mutate(arguments):
        calls.append(arguments)
        return "MUTATED"

    executor = ToolExecutor(handlers={"write_file": mutate})
    executor.configure_execution_policy(
        directive_manager=MalformedDirectives(),
        boundary_required=True,
    )
    result = await executor.execute_batch([{
        "id": "malformed-verdict",
        "name": "write_file",
        "arguments": {"path": "x", "content": "secret"},
    }])

    assert calls == []
    assert result[0]["executed"] is False
    assert result[0]["error"] == "tool_boundary_unavailable"


@pytest.mark.asyncio
async def test_unknown_dynamic_tool_defaults_to_mutation_authority():
    from colony_sidecar.reasoning.executor import ToolExecutor
    from colony_sidecar.reasoning.tool_policy import ToolActorPolicy

    calls = []

    async def dynamic_mutation(arguments):
        calls.append(arguments)
        return "MUTATED"

    executor = ToolExecutor()
    executor.set_dynamic_provider(lambda: {
        "new_dynamic_tool": ({
            "type": "function",
            "function": {
                "name": "new_dynamic_tool",
                "description": "new tool with no reviewed effect declaration",
                "parameters": {"type": "object", "properties": {}},
            },
        }, dynamic_mutation),
    })
    result = await executor.execute_batch(
        [{
            "id": "dynamic-call",
            "name": "new_dynamic_tool",
            "arguments": {},
        }],
        allowed_tools=frozenset({"new_dynamic_tool"}),
        actor_policy=ToolActorPolicy(
            principal_id="owner-reader",
            viewer_person_id="owner",
            allow_private_read=True,
            allow_mutation=False,
        ),
    )

    assert calls == []
    assert result[0]["error"] == "tool_authority_denied"


def _dynamic_provider(name, handler):
    return lambda: {
        name: ({
            "type": "function",
            "function": {
                "name": name,
                "description": "must never shadow the shipped capability",
                "parameters": {"type": "object", "properties": {}},
            },
        }, handler),
    }


def _collision_authority(name):
    if name == "calculate":
        return _authority("alice", scopes=("api:access",))
    if name == "read_file":
        return _authority("owner", scopes=("api:access",))
    return _authority(
        "owner", scopes=("api:access", "tools:mutate"))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["calculate", "read_file", "write_file"])
async def test_p8_direct_dynamic_collision_fails_before_name_authority(
    tmp_path, monkeypatch, name,
):
    from colony_sidecar.reasoning.executor import ToolExecutor

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    executor = ToolExecutor()
    executor.set_dynamic_provider(
        _dynamic_provider(name, dynamic_handler))
    executor.configure_execution_policy(
        directive_manager=_ToolDirectives(allowed=True),
        boundary_required=True,
    )
    host._tool_executor = executor

    with pytest.raises(HTTPException) as denied:
        await host.tools_invoke(ToolInvokeRequest(
            identity=HostIdentity(host_id="hermes"),
            name=name,
            arguments={},
        ), request=_request(_collision_authority(name)))

    assert denied.value.status_code == 503
    assert denied.value.detail["code"] == "tool_name_collision"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["calculate", "read_file", "write_file"])
async def test_p8_model_dynamic_collision_fails_before_model_call(
    tmp_path, monkeypatch, name,
):
    from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor

    class Model:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("colliding definitions must not reach the model")

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)

    async def dynamic_handler(_arguments):
        raise AssertionError("colliding handler must not execute")

    executor = ToolExecutor()
    executor.set_dynamic_provider(
        _dynamic_provider(name, dynamic_handler))
    executor.configure_execution_policy(
        directive_manager=_ToolDirectives(allowed=True),
        boundary_required=True,
    )
    host._tool_executor = executor
    host._reasoning_loop = ReasoningLoop(model=Model(), tools=executor)
    authority = _collision_authority(name)

    with pytest.raises(HTTPException) as denied:
        await host.reasoning_turn(ReasoningTurnRequest(
            identity=HostIdentity(host_id="hermes"),
            context=HostTurnContext(
                contact_id=authority.viewer_person_id,
                session_id="collision-session",
                channel_id="collision-channel",
            ),
            messages=[HostMessage(role="user", content="use the tool")],
            available_tools=[name],
        ), request=_request(authority))

    assert denied.value.status_code == 503
    assert denied.value.detail["code"] == "tool_name_collision"


@pytest.mark.asyncio
async def test_p8_off_direct_dynamic_tool_contract_is_unchanged():
    from colony_sidecar.reasoning.executor import ToolExecutor

    host._p8_runtime = None
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "LEGACY DYNAMIC"

    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(
        "fresh_dynamic_action", dynamic_handler))
    host._tool_executor = executor

    response = await host.tools_invoke(ToolInvokeRequest(
        identity=HostIdentity(host_id="legacy-host"),
        name="fresh_dynamic_action",
        arguments={"value": 1},
    ), request=_request(anonymous_authority()))

    assert response.available is True
    assert response.result == "LEGACY DYNAMIC"
    assert calls == [{"value": 1}]


@pytest.mark.asyncio
async def test_p8_legacy_bearer_cannot_body_claim_private_tool_authority(
    tmp_path, monkeypatch,
):
    from colony_sidecar.reasoning.executor import ToolExecutor

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)

    async def private_read(_arguments):
        raise AssertionError("legacy bearer must not reach private tool")

    host._tool_executor = ToolExecutor(handlers={"read_file": private_read})
    with pytest.raises(HTTPException) as denied:
        await host.tools_invoke(ToolInvokeRequest(
            identity=HostIdentity(host_id="legacy-host"),
            name="read_file",
            arguments={"path": "owner.txt", "person_id": "owner"},
        ), request=_request(legacy_authority()))

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_p8_reasoning_body_cannot_broaden_scoped_viewer(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    host._reasoning_loop = SimpleNamespace()

    with pytest.raises(HTTPException) as denied:
        await host.reasoning_turn(ReasoningTurnRequest(
            identity=HostIdentity(host_id="hermes"),
            context=HostTurnContext(
                contact_id="owner",
                session_id="session:1",
                channel_id="channel:1",
            ),
            messages=[HostMessage(role="user", content="show private state")],
        ), request=_request(_authority(
            "alice", scopes=("api:access", "tools:mutate"))))

    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "person_scope_not_granted"


def test_default_off_and_live_request_create_no_p8_state(tmp_path, monkeypatch):
    facts = SimpleNamespace()
    for configured in (None, "off", "live", "unknown"):
        if configured is None:
            monkeypatch.delenv(
                "COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
        else:
            monkeypatch.setenv(
                "COLONY_RECIPIENT_SIMULATOR_MODE", configured)
        runtime = _attach_p8_runtime(
            state_dir=tmp_path, facts_store=facts)
        assert runtime is None
        assert host._p8_runtime is None
        assert all(not (tmp_path / name).exists() for name in P8_FILES)


def test_shadow_installs_graph_wide_mirror_exclusion_and_off_clears_it(
    tmp_path, monkeypatch,
):
    class GraphPolicy:
        def __init__(self):
            self.calls = []

        def set_recall_source_exclusions(
            self, source_uris, *, legacy_metadata_markers=(),
        ):
            self.calls.append((
                tuple(source_uris), tuple(legacy_metadata_markers)))

    graph = GraphPolicy()
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    runtime = _attach_p8_runtime(
        state_dir=tmp_path, facts_store=facts, graph=graph)
    assert runtime is not None
    assert graph.calls == [
        ((), ()),
        (("tom:shared_fact",), ("shared_fact",)),
    ]
    runtime.close()

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "off")
    assert _attach_p8_runtime(
        state_dir=tmp_path, facts_store=facts, graph=graph) is None
    assert graph.calls[-1] == ((), ())


def test_research_wiring_preserves_off_ownership_and_borrows_only_for_p8(
    monkeypatch,
):
    calls = []

    class PipelineSpy:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    import colony_sidecar.research.pipeline as pipeline_module
    monkeypatch.setattr(
        pipeline_module, "ResearchPipeline", PipelineSpy)
    graph = object()

    _build_research_pipeline(graph=graph, p8_runtime=None)
    assert calls[-1] == ((), {})

    _build_research_pipeline(graph=graph, p8_runtime=object())
    assert calls[-1] == ((), {
        "graph": graph,
        "allow_fallback_graph": False,
    })


def test_shadow_attaches_one_runtime_and_restart_closes_cleanly(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_P8_FACT_MIN_CONFIDENCE", "0")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))

    first = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert isinstance(first, P8Runtime)
    assert first is host._p8_runtime
    assert all((tmp_path / name).exists() for name in P8_FILES)
    assert first.status()["mode"] == "shadow"
    assert first.status()["fact_min_confidence"] == 0.5
    first.close()
    first.close()

    restarted = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert isinstance(restarted, P8Runtime)
    assert restarted is host._p8_runtime
    restarted.close()


def test_viewer_is_sealed_from_scoped_authority_not_body_or_legacy(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    scoped = _request(_authority("alice", person_ids=("carol",)))
    viewer = host._p8_viewer_for_request(scoped, "alice")
    assert viewer.attested is True
    assert viewer.principal_id == "hermes-text"
    assert viewer.viewer_person_id == "alice"
    assert viewer.owner_person_id == "owner"
    assert viewer.conversation_scope == ""
    assert viewer.scope_revision.startswith("scope:")

    with pytest.raises(HTTPException) as broadened:
        host._p8_viewer_for_request(scoped, "bob")
    assert broadened.value.status_code == 403
    with pytest.raises(HTTPException):
        host._p8_viewer_for_request(_request(anonymous_authority()), "alice")
    with pytest.raises(HTTPException):
        host._p8_viewer_for_request(_request(legacy_authority()), "alice")

    resolver = _authority(
        "owner",
        scopes=("turns:resolve-sender",),
    )
    resolved = host._p8_viewer_for_request(
        _request(resolver), "alice", server_resolved=True)
    assert resolved.viewer_person_id == "alice"
    assert resolved.principal_id == resolver.principal_id


def test_new_fact_uses_typed_envelope_and_legacy_row_stays_excluded(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert runtime is not None
    alice = host._p8_viewer_for_request(_request(_authority("alice")), "alice")
    bob = host._p8_viewer_for_request(_request(_authority("bob")), "bob")

    legacy = facts.create_fact(
        contact_id="alice", fact="legacy unscoped secret",
        source="shared_context", confidence=0.99)
    scoped = facts.create_fact(
        contact_id="alice", fact="Alice likes concise updates",
        source="shared_context", confidence=0.9,
        metadata={
            "viewer_scope": "public",
            "shareability": "public",
            "subject_person_id": "bob",
        },
    )
    candidate = runtime.append_shared_fact(
        scoped, producer=alice, origin="body")
    weak = facts.create_fact(
        contact_id="alice", fact="zero-confidence claim",
        source="shared_context", confidence=0.0)
    weak_candidate = runtime.append_shared_fact(
        weak, producer=alice, origin="body")
    assert candidate.visibility.subject_person_id == "alice"
    assert candidate.visibility.viewer_scope == "person:alice"
    assert candidate.visibility.shareability == "subject_private"

    alice_batch = runtime.project_shared_facts(alice, now=_now())
    assert [row.content for row in alice_batch.facts] == [
        "Alice likes concise updates"]
    assert legacy["fact"] not in repr(alice_batch.public())
    assert "zero-confidence claim" not in repr(alice_batch.public())
    deck = runtime.deck_projection(alice, now=_now())
    assert weak_candidate.visibility.fact_ref not in repr(deck)
    lowered = runtime.project_shared_facts(
        alice, now=_now(), min_confidence=0.0)
    assert "zero-confidence claim" not in repr(lowered.public())
    assert runtime.project_shared_facts(bob, now=_now()).facts == ()


@pytest.mark.asyncio
async def test_shared_fact_handlers_seal_create_and_update_from_exact_authority(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    alice_request = _request(_authority("alice"))
    alice = host._p8_viewer_for_request(alice_request, "alice")

    created = await host.create_shared_fact(SharedFactCreateRequest(
        contact_id="alice", fact="handler-created fact", confidence=0.8,
    ), request=alice_request)
    assert [fact.content for fact in runtime.project_shared_facts(
        alice, now=_now()).facts] == ["handler-created fact"]

    updated = await host.update_shared_fact(
        created.id,
        SharedFactUpdateRequest(fact="handler-updated fact"),
        request=alice_request,
    )
    assert updated.fact == "handler-updated fact"
    assert [fact.content for fact in runtime.project_shared_facts(
        alice, now=_now()).facts] == ["handler-updated fact"]

    with pytest.raises(HTTPException) as crossed:
        await host.create_shared_fact(SharedFactCreateRequest(
            contact_id="alice", fact="Bob must not write this",
        ), request=_request(_authority("bob")))
    assert crossed.value.status_code == 403
    assert facts.list_facts(contact_id="alice")["total"] == 1


@pytest.mark.asyncio
async def test_context_renders_only_authenticated_enveloped_facts(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path / "state"))
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert runtime is not None
    alice_request = _request(_authority("alice"))
    alice = host._p8_viewer_for_request(alice_request, "alice")

    included = facts.create_fact(
        contact_id="alice", fact="allowed alice context",
        confidence=0.9)
    runtime.append_shared_fact(included, producer=alice, origin="server")
    facts.create_fact(
        contact_id="alice", fact="legacy row must not render",
        confidence=1.0)
    bob_row = facts.create_fact(
        contact_id="bob", fact="Bob private context", confidence=1.0)
    bob = host._p8_viewer_for_request(_request(_authority("bob")), "bob")
    runtime.append_shared_fact(bob_row, producer=bob, origin="server")

    response = await host.context_assemble(
        _context("alice"), request=alice_request)
    section = next(
        part for part in response.sections if part.id == "colony-shared-facts")
    assert "allowed alice context" in section.body
    assert "legacy row must not render" not in section.body
    assert "Bob private context" not in section.body

    legacy_response = await host.context_assemble(
        _context("alice"), request=_request(legacy_authority()))
    assert all(
        part.id != "colony-shared-facts"
        for part in legacy_response.sections
    )


@pytest.mark.asyncio
async def test_p8_context_filters_shared_fact_graph_mirrors_before_render(
    tmp_path, monkeypatch,
):
    class GraphRecall:
        def __init__(self):
            self.calls = []

        async def recall(self, **kwargs):
            self.calls.append(kwargs)
            return [
                {
                    "id": "private-mirror",
                    "content": "P8 subject-private launch secret",
                    "source_uri": "tom:shared_fact",
                    "relevance": 0.99,
                    "score": 0.99,
                },
                {
                    "id": "ordinary-memory",
                    "content": "ordinary recipient-scoped memory",
                    "source_uri": "session:one",
                    "relevance": 0.5,
                    "score": 0.5,
                },
            ]

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    graph = GraphRecall()
    host._graph = graph
    scoped_request = _request(_authority("alice"))

    assembled = await host.context_assemble(
        _context("alice"), request=scoped_request)
    assembled_text = "\n".join(
        section.body for section in assembled.sections)
    assert "ordinary recipient-scoped memory" in assembled_text
    assert "P8 subject-private launch secret" not in assembled_text

    enriched = await host.enriched_context(EnrichedContextRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(
            contact_id="alice", session_id="session:1",
            channel_id="body-claimed-channel",
        ),
        message="launch",
    ), request=scoped_request)
    enriched_text = "\n".join(
        section.body for section in enriched.sections)
    assert "ordinary recipient-scoped memory" in enriched_text
    assert "P8 subject-private launch secret" not in enriched_text
    assert len(graph.calls) == 2
    assert all(
        call["exclude_source_uris"] == ["tom:shared_fact"]
        for call in graph.calls
    )


@pytest.mark.asyncio
async def test_default_off_preserves_strict_graph_recall_signature(monkeypatch):
    class StrictGraphRecall:
        async def recall(self, query, limit, person_id):
            assert query
            assert limit == 5
            assert person_id == "alice"
            return [{
                "id": "legacy-memory",
                "content": "strict legacy graph memory",
                "relevance": 0.7,
                "score": 0.7,
            }]

    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
    host.set_p8_runtime(None)
    host._graph = StrictGraphRecall()
    # The legacy migration lane keeps the exact historical recall call.  A
    # scoped guest uses only canonical sources and must not reach this legacy
    # producer while P8 is unavailable.
    request = _request(legacy_authority())

    assembled = await host.context_assemble(
        _context("alice"), request=request)
    assert "strict legacy graph memory" in repr(assembled)

    enriched = await host.enriched_context(EnrichedContextRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(
            contact_id="alice", session_id="session:1",
            channel_id="channel:1",
        ),
        message="memory",
    ), request=request)
    assert "strict legacy graph memory" in repr(enriched)


@pytest.mark.asyncio
async def test_p8_fact_view_is_the_only_tom2_context_content_path(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "alice")
    monkeypatch.setenv("COLONY_TOM2_CONTEXT", "1")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    alice_request = _request(_authority("alice"))
    alice = host._p8_viewer_for_request(alice_request, "alice")

    legacy = facts.create_fact(
        contact_id="alice", fact="legacy untyped Tom2 secret",
        confidence=0.99)
    scoped = facts.create_fact(
        contact_id="alice", fact="authorized typed Tom2 fact",
        confidence=0.9)
    runtime.append_shared_fact(scoped, producer=alice, origin="server")

    tom2 = Tom2Store(str(tmp_path / "tom2.db"))
    tom2.record_inference(
        contact_id="alice", kind="knows", fact_ref=legacy["id"])
    tom2.record_inference(
        contact_id="alice", kind="knows", fact_ref=scoped["id"])
    view = runtime.projected_facts_view(alice, now=_now())
    level1 = render_level1(tom2, view, "alice")
    assert "authorized typed Tom2 fact" in level1
    assert "legacy untyped Tom2 secret" not in level1

    tom2.record_inference(
        contact_id="bob", kind="unaware_of", fact_ref=legacy["id"])
    tom2.record_inference(
        contact_id="carol", kind="unaware_of", fact_ref=scoped["id"])
    tom2.record_inference(
        contact_id="dave", kind="unaware_of", fact_ref=scoped["id"],
        evidence_refs=[legacy["id"]],
    )
    for index in range(10):
        tom2.record_inference(
            contact_id=f"denied-{index}", kind="unaware_of",
            fact_ref=legacy["id"],
        )
    host._tom2_store = tom2
    response = await host.context_assemble(
        _context("alice"), request=alice_request)
    tom2_section = next(
        section for section in response.sections
        if section.id == "colony-tom2"
    )
    assert "authorized typed Tom2 fact" in tom2_section.body
    assert "legacy untyped Tom2 secret" not in tom2_section.body
    assert legacy["id"] not in tom2_section.body
    assert "dave" not in tom2_section.body
    assert "... and" not in tom2_section.body

    report = await host.tom2_report(request=alice_request)
    assert report["count"] == 2
    assert "authorized typed Tom2 fact" in repr(report)
    assert "legacy untyped Tom2 secret" not in repr(report)
    assert legacy["id"] not in repr(report)

    with pytest.raises(HTTPException) as legacy_report:
        await host.tom2_report(request=_request(legacy_authority()))
    assert legacy_report.value.status_code == 403

    legacy_response = await host.context_assemble(
        _context("alice"), request=_request(legacy_authority()))
    assert all(
        section.id != "colony-tom2"
        for section in legacy_response.sections
    )
    assert required_scope("GET", "/v1/host/tom2/report") == "tom:read"


@pytest.mark.asyncio
async def test_enriched_context_never_falls_through_to_raw_legacy_facts(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    request = _request(_authority("alice"))
    viewer = host._p8_viewer_for_request(request, "alice")
    scoped = facts.create_fact(
        contact_id="alice", fact="authorized enriched fact", confidence=0.9)
    runtime.append_shared_fact(scoped, producer=viewer, origin="server")
    facts.create_fact(
        contact_id="alice", fact="raw legacy enriched leak", confidence=1.0)

    response = await host.enriched_context(EnrichedContextRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(
            contact_id="alice", session_id="session:1",
            channel_id="body-claimed-channel",
        ),
        message="hello",
    ), request=request)
    rendered = "\n".join(section.body for section in response.sections)
    assert "authorized enriched fact" in rendered
    assert "raw legacy enriched leak" not in rendered


@pytest.mark.asyncio
async def test_relationship_topics_require_request_sealed_exact_viewer(
    tmp_path, monkeypatch,
):
    class Contacts:
        async def get(self, contact_id):
            if contact_id != "alice":
                return None
            return SimpleNamespace(
                contact_id="alice",
                display_name="Alice",
                trust_tier="trusted",
                interaction_count=6,
                last_interaction_at="",
                relationship_score=0.7,
                timezone="",
            )

    class RawFactsMustNotRun:
        def list_facts(self, **_kwargs):
            raise AssertionError("raw SharedFacts bypassed P8")

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    host.set_facts_store(facts)
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    alice_request = _request(_authority("alice"))
    alice_viewer = host._p8_viewer_for_request(alice_request, "alice")
    for text in (
        "Alice enjoys telescope restoration",
        "Alice plans another telescope project",
    ):
        row = facts.create_fact(
            contact_id="alice", fact=text, confidence=0.9)
        runtime.append_shared_fact(row, producer=alice_viewer, origin="server")

    profiler = RelationshipProfiler(
        contacts_store=Contacts(),
        facts_store=RawFactsMustNotRun(),
        p8_runtime=runtime,
        db_path=str(tmp_path / "relationships.db"),
    )
    # Autonomy/cache persistence has no viewer authority and stays contentless.
    cached_seed = await profiler.profile("alice")
    assert cached_seed.rapport_topics == []
    host.set_relationship_profiler(profiler)

    scoped = await host.context_assemble(
        _context("alice"), request=alice_request)
    scoped_approach = next(
        section for section in scoped.sections
        if section.id == "colony-approach"
    )
    assert "telescope" in scoped_approach.body

    legacy = await host.context_assemble(
        _context("alice"), request=_request(legacy_authority()))
    assert all(
        section.id != "colony-approach"
        for section in legacy.sections
    )

    scoped_detail = await host.get_relationship_brief(
        "alice", request=alice_request)
    assert "telescope" in repr(scoped_detail)
    legacy_detail = await host.get_relationship_brief(
        "alice", request=_request(legacy_authority()))
    assert "telescope" not in repr(legacy_detail)

    with pytest.raises(HTTPException) as crossed:
        await host.get_relationship_brief(
            "alice", request=_request(_authority("bob")))
    assert crossed.value.status_code == 403


@pytest.mark.asyncio
async def test_default_off_relationship_detail_keeps_legacy_selector_behavior():
    class Brief:
        def to_dict(self):
            return {"contact_id": "alice", "rapport_topics": ["legacy"]}

        def render(self):
            return "legacy relationship brief"

    class StrictProfiler:
        def __init__(self):
            self.calls = []

        def cached(self, contact_id):
            self.calls.append(contact_id)
            return Brief()

    profiler = StrictProfiler()
    host.set_p8_runtime(None)
    host.set_relationship_profiler(profiler)

    # Baseline endpoint semantics allowed the body/path-selected contact under
    # the general API scope; P8-off must not introduce a new person gate.
    result = await host.get_relationship_brief(
        "alice", request=_request(_authority("bob")))
    assert result["rendered"] == "legacy relationship brief"
    assert profiler.calls == ["alice"]

    class RefreshProfiler:
        def __init__(self):
            self.profile_calls = []

        def cached(self, _contact_id):
            raise AssertionError("refresh=True must skip cache")

        async def profile(self, contact_id):
            self.profile_calls.append(contact_id)
            return Brief()

    refresh_profiler = RefreshProfiler()
    host.set_relationship_profiler(refresh_profiler)
    positional = await host.get_relationship_brief("alice", True)
    assert positional["rendered"] == "legacy relationship brief"
    assert refresh_profiler.profile_calls == ["alice"]


@pytest.mark.asyncio
async def test_p8_multimodal_memory_search_filters_before_content_response(
    tmp_path, monkeypatch,
):
    class Embedder:
        is_multimodal = False

        async def embed(self, _text):
            return [0.1, 0.2]

    class Store:
        def __init__(self):
            self.limits = []

        async def search_cross_modal(
            self, _collection, _vector, *, limit,
            filter_modality, min_score,
        ):
            self.limits.append(limit)
            return [
                {"id": "mirror", "text": "private mirror", "metadata": {
                    "source_uri": "tom:shared_fact"}},
                {"id": "legacy", "text": "legacy private mirror",
                 "metadata": {}},
                {"id": "ordinary", "text": "ordinary memory",
                 "metadata": {"source_uri": "session:one"}},
            ]

    class Graph:
        def __init__(self):
            self.calls = []

        async def filter_memory_vector_results(self, rows):
            self.calls.append(rows)
            return [row for row in rows if row["id"] == "ordinary"]

    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    store = Store()
    graph = Graph()
    host._embedder = Embedder()
    host._graph = graph
    import colony_sidecar.vector as vector_module
    monkeypatch.setattr(vector_module, "get_store", lambda: store)

    response = await host.memory_search_multimodal(
        MultimodalSearchRequest(
            identity=HostIdentity(host_id="test"),
            query="memory", collection="memories", limit=2,
        ))
    assert [row["id"] for row in response.results] == ["ordinary"]
    assert len(graph.calls) == 1
    assert store.limits == [40]

    # Turning P8 off preserves the historical exact search call and does not
    # invoke the graph policy adapter.
    host.set_p8_runtime(None)
    graph.calls.clear()
    response = await host.memory_search_multimodal(
        MultimodalSearchRequest(
            identity=HostIdentity(host_id="test"),
            query="memory", collection="memories", limit=2,
        ))
    assert response.results[0]["id"] == "mirror"
    assert graph.calls == []
    assert store.limits[-1] == 2


def _principal(
    name: str,
    secret: str,
    person: str,
    scopes: list[str],
    *,
    audiences: list[str] | None = None,
) -> dict:
    return {
        "principal": name,
        "status": "active",
        "scopes": scopes,
        "viewer_person_id": person,
        "audiences": audiences or ["viewer"],
        "credentials": [{
            "id": "current", "secret": secret, "status": "active",
        }],
    }


@pytest.mark.asyncio
async def test_scoped_status_and_deck_endpoints_fail_closed_across_people(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    assert runtime is not None
    alice = host._p8_viewer_for_request(_request(_authority("alice")), "alice")
    row = facts.create_fact(
        contact_id="alice", fact="Alice deck fact", confidence=0.9)
    runtime.append_shared_fact(row, producer=alice, origin="server")
    runtime.observe_outbound_payload(
        {"id": "deck:outbound", "description": "Scoped shadow sample"},
        {
            "person_id": "alice",
            "target": {"user_chat": "whatsapp:alice-thread"},
        },
        now=_now(),
    )

    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [
            _principal("alice-reader", "alice-secret", "alice", ["tom:read"]),
            _principal("bob-reader", "bob-secret", "bob", ["tom:read"]),
            _principal(
                "owner-deck", "owner-secret", "owner", ["tom:read"],
                audiences=["viewer", "owner"],
            ),
            _principal("no-scope", "none-secret", "alice", ["api:access"]),
        ],
    }))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(
        ApiKeyMiddleware,
        api_key="legacy-secret",
        keyring_path=str(keyring),
    )
    app.include_router(host.router)
    headers = lambda secret: {"Authorization": f"Bearer {secret}"}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        status = await client.get(
            "/v1/host/tom/p8/status", headers=headers("alice-secret"))
        deck = await client.get(
            "/v1/host/tom/p8/deck?person_id=alice",
            headers=headers("alice-secret"),
        )
        crossed = await client.get(
            "/v1/host/tom/p8/deck?person_id=alice",
            headers=headers("bob-secret"),
        )
        unscoped = await client.get(
            "/v1/host/tom/p8/status", headers=headers("none-secret"))
        legacy = await client.get(
            "/v1/host/tom/p8/status", headers=headers("legacy-secret"))
        unbounded = await client.get(
            "/v1/host/tom/p8/deck?person_id=alice&max_facts=65",
            headers=headers("alice-secret"),
        )
        owner_deck = await client.get(
            "/v1/host/tom/p8/deck", headers=headers("owner-secret"))

    assert status.status_code == 200
    assert status.json()["mode"] == "shadow"
    assert deck.status_code == 200
    assert "Alice deck fact" in repr(deck.json())
    assert crossed.status_code == 403
    assert unscoped.status_code == 403
    assert legacy.status_code == 403
    assert unbounded.status_code == 422
    assert owner_deck.status_code == 200
    assert len(owner_deck.json()["recipient_audit"]["events"]) == 2
    assert owner_deck.json()["coverage"]["status"] == "complete"
    assert required_scope("GET", "/v1/host/tom/p8/status") == "tom:read"
    assert required_scope("GET", "/v1/host/tom/p8/deck") == "tom:read"


def test_restart_replays_envelopes_and_all_runtime_stores_close(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_RECIPIENT_SIMULATOR_MODE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    facts = SharedFactsStore(str(tmp_path / "facts.db"))
    runtime = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    viewer = host._p8_viewer_for_request(_request(_authority("alice")), "alice")
    row = facts.create_fact(
        contact_id="alice", fact="survives restart", confidence=0.8)
    runtime.append_shared_fact(row, producer=viewer, origin="server")
    runtime.close()

    restarted = _attach_p8_runtime(state_dir=tmp_path, facts_store=facts)
    batch = restarted.project_shared_facts(viewer, now=_now())
    assert [fact.content for fact in batch.facts] == ["survives restart"]
    restarted.close()
