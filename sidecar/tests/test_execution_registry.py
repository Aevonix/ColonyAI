"""Actual shared SQLite state, scoped HTTP reads, and native-adapter events."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import RequestAuthority, required_scope
from colony_sidecar.api.routers import executions
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.executions import ExecutionRegistry, format_view
from test_hermes_general_governance import runtime


def observation(name="a", **changes):
    return {"execution_id": hashlib.sha256(name.encode()).hexdigest(),
            "contact_id": "contact-a", "session_id": "session-" + name,
            "turn_id": "turn-" + name, "parent_execution_id": "", "platform": "sms",
            "state": "observed", "phase": "turn", "tool_name": "", "sequence": 1,
            **changes}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    return ExecutionRegistry(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))


def test_two_sessions_share_durable_view_and_terminal_never_resurrects(store):
    def write(name):
        peer = ExecutionRegistry(TurnIdempotencyLedger(store.ledger.db_path))
        return peer.observe(observation(name), principal_id="host", contact_id="contact-a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(row["accepted"] for row in pool.map(write, ("sms", "voice")))
    reopened = ExecutionRegistry(TurnIdempotencyLedger(store.ledger.db_path))
    assert reopened.view(contact_id="owner", owner=True)["total"] == 2
    assert reopened.view(contact_id="contact-a", session_id="session-sms")["total"] == 1
    reopened.observe(observation("sms", sequence=3, state="interrupted", phase="ended"), principal_id="host", contact_id="contact-a")
    assert not reopened.observe(observation("sms", sequence=99, phase="model"), principal_id="host", contact_id="contact-a")["accepted"]
    assert reopened.view(contact_id="owner", owner=True)["total"] == 1
    # Existing source evidence can coexist without a new store or DB rewind.
    reopened.ledger.record_source("source-a", contact_id="contact-a", session_id="session-sms", messages=[{"role": "user", "content": "A remembered fact."}])
    assert reopened.ledger.search_sources("remembered", contact_id="contact-a", session_id="session-sms")


def test_lease_expiry_is_unknown_not_completion_and_out_of_order_does_not_refresh(store):
    now = [1000.0]
    store.clock = lambda: now[0]
    store.observe(observation(), principal_id="host", contact_id="contact-a")
    now[0] += 180
    stale = store.view(contact_id="owner", owner=True)
    assert stale["items"][0]["liveness"] == "unknown"
    assert stale["items"][0]["observation_age_seconds"] == 180
    assert "unknown" in format_view(stale)
    assert not store.observe(observation(), principal_id="host", contact_id="contact-a")["accepted"]
    store.observe(observation(sequence=2, phase="model"), principal_id="host", contact_id="contact-a")
    assert store.view(contact_id="owner", owner=True)["items"][0]["liveness"] == "recently_observed"


def test_parent_scope_and_writer_are_immutable(store):
    parent = observation()
    store.observe(parent, principal_id="host", contact_id="contact-a")
    child = observation("child", parent_execution_id=parent["execution_id"], platform="subagent")
    with pytest.raises(ValueError, match="parent_scope"):
        store.observe(child, principal_id="other-host", contact_id="contact-a")
    with pytest.raises(ValueError, match="parent_scope"):
        store.observe(child, principal_id="host", contact_id="contact-b")
    store.observe(child, principal_id="host", contact_id="contact-a")
    with pytest.raises(ValueError, match="execution_scope_conflict"):
        store.observe({**child, "sequence": 2}, principal_id="host", contact_id="contact-b")
    assert store.view(contact_id="contact-b", session_id=child["session_id"])["total"] == 0


@pytest.mark.asyncio
async def test_api_binds_owner_and_subject_to_existing_authority(store, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    store.observe(observation(), principal_id="host", contact_id="contact-a")
    store.observe(observation("other"), principal_id="host", contact_id="contact-b")
    principal = [RequestAuthority(principal_id="guest-host", credential_id="key", scopes=frozenset({"context:read", "turns:write"}), viewer_person_id="contact-a", person_ids=frozenset({"contact-a"}), audiences=frozenset({"viewer"}), authenticated=True)]
    app = FastAPI()
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.colony_authority = principal[0]
        return await call_next(request)
    app.include_router(executions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/host/executions", params={"contact_id": "owner"})).status_code == 403
        assert (await client.post("/v1/host/executions/observe", json=observation("forged", contact_id="owner"))).status_code == 403
        own = await client.get("/v1/host/executions", params={"contact_id": "contact-a", "session_id": "session-a"})
        assert own.json()["total"] == 1
        assert (await client.get('/v1/host/executions', params={
            'contact_id': 'contact-a', 'projection': 'request'})).status_code == 403
        assert (await client.get("/v1/host/executions", params={"contact_id": "contact-a", "session_id": "session-other"})).json()["total"] == 0
        principal[0] = RequestAuthority(principal_id="owner-host", credential_id="key", scopes=frozenset({"context:read"}), viewer_person_id="owner", person_ids=frozenset({"owner"}), audiences=frozenset({"owner"}), authenticated=True)
        assert (await client.get("/v1/host/executions", params={"contact_id": "owner"})).json()["total"] == 2
        current = await client.get('/v1/host/executions', params={
            'contact_id': 'owner', 'projection': 'request', 'limit': 100})
        assert current.status_code == 200
        assert current.json()['schema'] == 'ColonyRequestWorkV1'
        assert observation()['execution_id'] in current.json()['text']
        assert len(current.json()['text']) <= 4000
        assert (await client.post("/v1/host/executions/observe", json=observation("write", contact_id="owner"))).status_code == 403
    assert required_scope("GET", "/v1/host/executions") == "context:read"
    assert required_scope("POST", "/v1/host/executions/observe") == "turns:write"


@pytest.mark.asyncio
async def test_anonymous_and_legacy_cannot_claim_owner(store):
    app = FastAPI()
    app.include_router(executions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/host/executions", params={"contact_id": "owner"})).status_code == 403
        assert (await client.post("/v1/host/executions/observe", json=observation())).status_code == 403
    from colony_sidecar.api.authority import legacy_authority
    request = SimpleNamespace(state=SimpleNamespace(colony_authority=legacy_authority()))
    with pytest.raises(Exception) as error:
        executions.authorized_viewer(request, "owner", scope="context:read")
    assert error.value.status_code == 403


def test_adapter_parent_binding_rotation_and_interruption_reach_durable_registry(store):
    path = Path(__file__).resolve().parents[2] / "plugins/hermes-plugin/executions.py"
    spec = importlib.util.spec_from_file_location("execution_observer_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    class Client:
        def post(self, path, *, json, **kwargs):
            calls.append(json)
            body = executions.ExecutionObservation(**json)
            store.observe(body.model_dump(), principal_id="host", contact_id=body.contact_id)
            assert kwargs["timeout"] <= .4
            return SimpleNamespace(raise_for_status=lambda: None)
    observer = module.ExecutionObserver(Client())
    owner = SimpleNamespace(valid_participant=True, contact_id="owner", platform="sms")
    guest = SimpleNamespace(valid_participant=True, contact_id="contact-a", platform="voice")
    observer.start(owner, session_id="owner-session", turn_id="owner-turn")
    observer.start(guest, session_id="guest-session", turn_id="guest-turn")
    observer.child(parent_session_id="guest-session", parent_turn_id="guest-turn", child_session_id="child-session", child_goal="Never store this task prose")
    observer.start(owner, session_id="child-session", turn_id="child-turn", parent_session_id="guest-session")
    child = calls[-1]
    assert child["contact_id"] == "contact-a" and child["parent_execution_id"] == calls[1]["execution_id"]
    assert "Never store" not in str(calls)
    observer.start(owner, session_id="unbound-child", turn_id="unbound-turn", parent_session_id="owner-session")
    assert len(calls) == 3
    # Native compression rotates session IDs inside a turn. End still closes
    # the original observed row by the unchanged native turn ID.
    observer.update("model", session_id="rotated-child", turn_id="child-turn")
    observer.end(session_id="rotated-child", turn_id="child-turn", interrupted=True)
    observer.update("model", session_id="rotated-child", turn_id="child-turn")
    assert len(calls) == 5
    assert store.view(contact_id="owner", owner=True)["total"] == 2
    assert calls[-1]["state"] == "interrupted"


def test_scope_conflict_does_not_replace_child_binding(store):
    from test_hermes_general_governance import _load_plugin
    module = _load_plugin("colony_execution_binding_test")
    observer = module.ExecutionObserver(SimpleNamespace(post=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None)))
    scope = SimpleNamespace(valid_participant=True, contact_id="contact-a", platform="sms")
    observer.start(scope, session_id="a", turn_id="ta")
    observer.start(scope, session_id="b", turn_id="tb")
    observer.child(parent_session_id="a", parent_turn_id="ta", child_session_id="child")
    observer.child(parent_session_id="b", parent_turn_id="tb", child_session_id="child")
    observer.start(scope, session_id="child", turn_id="tc", parent_session_id="b")
    assert "tc" not in observer._records


@pytest.mark.asyncio
async def test_owner_context_observes_other_sessions_but_guest_context_omits_them(store, monkeypatch):
    from colony_sidecar.api.routers import host
    from colony_sidecar.api.schemas.host import ContextAssembleRequest
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setattr(host, "_p8_runtime", None)
    # Other producer behavior is outside this focused read-view test.
    monkeypatch.setattr(host, "_require_scoped_context_runtime_for_guest", lambda *a: None)
    store.observe(observation("voice"), principal_id="host", contact_id="owner")
    store.observe(observation("cron", platform="cron"), principal_id="host", contact_id="owner")
    for person in ("owner", "contact-a"):
        authority = RequestAuthority(principal_id="host-" + person, credential_id="key", scopes=frozenset({"context:read"}), viewer_person_id=person, person_ids=frozenset({person}), audiences=frozenset({"viewer"}), authenticated=True)
        request = SimpleNamespace(state=SimpleNamespace(colony_authority=authority))
        body = ContextAssembleRequest(identity={"host_id": "test"}, context={"contact_id": person, "session_id": "new-chat"}, incoming_message={"role": "user", "content": "What are you doing now?"})
        response = await host.context_assemble(body, request)
        sections = [s for s in response.sections if s.id == "colony-executions"]
        if person == "owner":
            assert len(sections) == 1
            assert "session-voice" in sections[0].body and "session-cron" in sections[0].body
        else:
            assert not sections


def test_enabled_tool_observer_preserves_owner_guest_authority(runtime):
    from test_hermes_general_governance import _pre, _tool
    module, context, _client, _mediator = runtime
    context.config["plugins"]["colony"]["execution_registry_enabled"] = True
    module.register(context)
    _pre(context, session="owner-session", task="owner-task", turn="owner-turn", platform="sms", sender="+15550001")
    _pre(context, session="guest-session", task="guest-task", turn="guest-turn", platform="sms", sender="+15550002")
    owner = _tool(context, "colony_autonomy_status", {}, session="owner-session", task="owner-task", turn="owner-turn", call="call-owner")
    guest = _tool(context, "colony_autonomy_status", {}, session="guest-session", task="guest-task", turn="guest-turn", call="call-guest")
    import json
    assert json.loads(owner)["running"] is True
    assert json.loads(guest).get("running") is not True
    assert module._TOOL_EXECUTION_CONTEXT.get() is None
