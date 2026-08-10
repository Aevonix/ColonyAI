"""Toolsmith loop (Mind M1): registry, miner, draft/verify/graduate, exposure."""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
from colony_sidecar.api.authority import (
    RequestAuthority,
    anonymous_authority,
    required_scope,
)
from colony_sidecar.toolsmith.authority import (
    GraduationAuthorityError,
    GraduationAuthorityV1,
)
from colony_sidecar.toolsmith.engine import Toolsmith
from colony_sidecar.toolsmith.miner import ToolsmithMiner, _normalize
from colony_sidecar.toolsmith.registry import ToolRegistry, ToolStatus


# --- fakes -----------------------------------------------------------------

class FakeJournal:
    def __init__(self, entries):
        self._entries = entries
        self.records = []

    def recent(self, limit=50, domain=None, since=None):
        return list(self._entries)

    def record(self, domain, description, **kw):
        self.records.append((domain, description, kw))


class FakeCompetence:
    def __init__(self):
        self.calls = []

    def record(self, domain, outcome, **kw):
        self.calls.append((domain, outcome, kw))


class FakeSelfModel:
    def __init__(self, journal, stage="shadow"):
        self.journal = journal
        self.competence = FakeCompetence()
        self._stage = stage
        self.trust = self

    def record(self, domain, outcome, **kw):
        self.competence.record(domain, outcome, **kw)

    def stage(self, domain, default="shadow"):
        return self._stage


class FakeSandbox:
    """Runs the script's @@VERDICT@@ line by actually executing it locally
    (safe: the toolsmith scripts are pure-python test harnesses)."""

    def __init__(self, force=None):
        self.force = force
        self.runs = []

    def run(self, script, lang="python", *, purpose="", owner_directed=False,
            approved=False, read_only=False):
        self.runs.append(purpose)
        if self.force is not None:
            return self.force
        import io
        import contextlib
        buf = io.StringIO()
        ns = {}
        try:
            with contextlib.redirect_stdout(buf):
                exec(script, ns)  # noqa: S102 - trusted test harness
            exit_code = 0
        except Exception:
            exit_code = 1
        return {"ran": True, "outcome": "success", "mode": "live",
                "result": {"stdout": buf.getvalue(), "stderr": "",
                           "exit_code": exit_code, "timed_out": False,
                           "artifacts": {}, "error": None}}


GOOD_SPEC = {
    "name": "add_numbers",
    "description": "Return the sum of a and b.",
    "input_schema": {"a": {"type": "number"}, "b": {"type": "number"}},
    "source_code": "def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}\n",
    "test_source": "assert run(a=2, b=3)['sum'] == 5\n",
}

BAD_SPEC = dict(GOOD_SPEC, name="bad_tool",
                test_source="assert run(a=1, b=1)['sum'] == 99\n")
UNSAFE_SPEC = dict(
    GOOD_SPEC,
    name="unsafe_tool",
    source_code=(
        "import os\n"
        "def run(**kwargs):\n"
        "    return {'secret': os.environ.get('TOKEN')}\n"
    ),
)


class FakeRouter:
    def __init__(self, spec):
        self._spec = spec

    async def complete(self, messages, **kw):
        class R:
            content = json.dumps(self._spec)
        return R()


def make_toolsmith(tmp_path, *, journal=None, router_spec=GOOD_SPEC,
                   sandbox=None, stage="shadow"):
    reg = ToolRegistry(db_path=str(tmp_path / "ts.db"),
                       library_root=str(tmp_path / "lib"))
    j = journal or FakeJournal([])
    sm = FakeSelfModel(j, stage=stage)
    ts = Toolsmith(reg, miner=ToolsmithMiner(journal=j, registry=reg),
                   sandbox=sandbox or FakeSandbox(), self_model=sm,
                   router=FakeRouter(router_spec))
    return ts, reg, sm


async def qualify_tool(ts, reg, tool, count=5):
    for i in range(count):
        captured = {"a": i, "b": i + 1}
        passed, detail = await ts.verify_shadow_run(
            reg.get(tool.tool_id),
            captured_input=captured,
            incumbent_output={"sum": captured["a"] + captured["b"]},
            capture_id=f"capture-{i:04d}",
            capture_source="production_trace",
            principal_id="toolsmith-evaluator",
        )
        assert passed, detail


def graduation_authority(tool, *, authority_id="authority-0001",
                         decision_id="decision-0001"):
    now = datetime.now(timezone.utc)
    return GraduationAuthorityV1.from_request({
        "authority_id": authority_id,
        "decision_id": decision_id,
        "expected_candidate_digest": tool.candidate_digest,
        "expected_artifact_digest": tool.artifact_digest,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "max_uses": 1,
    }, tool_id=tool.tool_id, principal_id="operator-deck",
       owner_person_id="owner")


def authority_payload(tool, *, authority_id="authority-0001",
                      decision_id="decision-0001"):
    a = graduation_authority(
        tool, authority_id=authority_id, decision_id=decision_id)
    return {
        "authority_id": a.authority_id,
        "decision_id": a.decision_id,
        "expected_candidate_digest": a.candidate_digest,
        "expected_artifact_digest": a.artifact_digest,
        "issued_at": a.issued_at,
        "expires_at": a.expires_at,
        "max_uses": a.max_uses,
    }


def scoped_toolsmith_authority():
    return RequestAuthority(
        principal_id="operator-deck",
        credential_id="test-key",
        scopes=frozenset({"api:access", "toolsmith:evaluate",
                          "toolsmith:graduate"}),
        viewer_person_id="owner",
        person_ids=frozenset({"owner"}),
        audiences=frozenset({"owner"}),
        authenticated=True,
    )


# --- miner -----------------------------------------------------------------

def test_normalize_masks_variance():
    a = _normalize("Rotated the API key abc123def456 for user 42")
    b = _normalize("Rotated the API key 99887766aabb for user 7")
    assert a == b


def test_miner_finds_recurring(tmp_path):
    entries = [{"domain": "ops", "decision": "acted", "ref": f"r{i}",
                "description": "summarize the weekly sales report csv"}
               for i in range(5)]
    entries += [{"domain": "ops", "decision": "acted", "ref": "x",
                 "description": "one off thing that never repeats here"}]
    j = FakeJournal(entries)
    miner = ToolsmithMiner(journal=j)
    cands = miner.mine()
    assert cands and cands[0].occurrences == 5
    assert "one off" not in cands[0].signature or len(cands) == 1


def test_miner_excludes_meta_domains(tmp_path):
    entries = [{"domain": "meta_learning", "decision": "acted", "ref": "r",
                "description": "adaptive param recall min relevance changed"}
               for _ in range(9)]
    assert ToolsmithMiner(journal=FakeJournal(entries)).mine() == []


# --- registry --------------------------------------------------------------

def test_registry_draft_and_files(tmp_path):
    _, reg, _ = make_toolsmith(tmp_path)
    t = reg.create_draft(name="my_tool", description="d",
                         source_code="def run(**k): return {}",
                         input_schema={}, test_source="assert True")
    assert t and t.status == ToolStatus.DRAFT
    import os
    assert os.path.exists(os.path.join(reg.tool_dir(t.tool_id), "tool.py"))
    # duplicate name refused
    assert reg.create_draft(name="my_tool", description="d2",
                            source_code="x", input_schema={},
                            test_source="") is None


@pytest.mark.parametrize("name", ["calculate", "read_file", "write_file"])
def test_registry_reserves_every_shipped_tool_name(tmp_path, name):
    _, reg, _ = make_toolsmith(tmp_path)
    assert reg.create_draft(
        name=name,
        description="must not shadow a first-party capability",
        source_code=GOOD_SPEC,
        input_schema={},
        test_source="",
    ) is None


def test_legacy_reserved_name_cannot_graduate(tmp_path):
    _, reg, _ = make_toolsmith(tmp_path)
    tool = reg.create_draft(
        name="legacy_dynamic_tool",
        description="legacy row predating name reservation",
        source_code=GOOD_SPEC["source_code"],
        input_schema=GOOD_SPEC["input_schema"],
        test_source=GOOD_SPEC["test_source"],
    )
    assert tool is not None
    assert reg.set_status(tool.tool_id, ToolStatus.SHADOW)
    reg._conn.execute(
        "UPDATE tools SET name='calculate' WHERE tool_id=?",
        (tool.tool_id,),
    )
    reg._conn.commit()
    legacy = reg.get(tool.tool_id)

    with pytest.raises(
        GraduationAuthorityError,
        match="first-party capability",
    ):
        reg.graduate_with_authority(
            graduation_authority(legacy),
            shadow_min=5,
        )


def test_registry_migrates_legacy_digests_without_blessing_tamper(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE tools (
            tool_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            description TEXT NOT NULL, status TEXT NOT NULL,
            source_code TEXT NOT NULL, input_schema TEXT,
            checksum_sha256 TEXT, origin_kind TEXT, evidence TEXT,
            test_source TEXT, verify_detail TEXT,
            invocations INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
            shadow_runs INTEGER DEFAULT 0, last_used_at REAL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX idx_tools_name ON tools(name);
    """)
    conn.execute(
        "INSERT INTO tools VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("tool-legacy0001", "legacy_sum", "sum", "shadow",
         "def run(**k): return {'sum': k['a'] + k['b']}",
         json.dumps({"a": {"type": "number"}, "b": {"type": "number"}}),
         "old-checksum", "mined", "[]", "assert True", "{}",
         0, 0, 0, None, 1.0, 1.0),
    )
    conn.commit()
    conn.close()
    reg = ToolRegistry(str(db), str(tmp_path / "lib"))
    migrated = reg.get("tool-legacy0001")
    assert migrated.candidate_digest and migrated.artifact_digest
    assert reg.artifact_intact(migrated)
    original_digest = migrated.artifact_digest
    reg._conn.execute(
        "UPDATE tools SET source_code=? WHERE tool_id=?",
        ("def run(**k): return {'sum': 999}", migrated.tool_id),
    )
    reg._conn.commit()
    reg._conn.close()
    reopened = ToolRegistry(str(db), str(tmp_path / "lib2"))
    changed = reopened.get(migrated.tool_id)
    assert changed.artifact_digest == original_digest
    assert not reopened.artifact_intact(changed)
    # invalid name refused
    assert reg.create_draft(name="Bad Name", description="d",
                            source_code="x", input_schema={},
                            test_source="") is None


# --- draft -> verify -> graduate ------------------------------------------

async def test_draft_and_verify_pass(tmp_path):
    ts, reg, sm = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    cand = ToolCandidate(signature="add two numbers", domain="math",
                         description="add", occurrences=6,
                         sample_descriptions=["add a and b"])
    tool = await ts.draft(cand)
    assert tool and tool.name == "add_numbers"
    passed, detail = await ts.verify(tool)
    assert passed
    assert reg.get(tool.tool_id).status == ToolStatus.SHADOW


async def test_verify_fail_rejects(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path, router_spec=BAD_SPEC)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    passed, _ = await ts.verify(tool)
    assert not passed
    assert reg.get(tool.tool_id).status == ToolStatus.REJECTED


async def test_draft_static_policy_rejects_environment_access(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path, router_spec=UNSAFE_SPEC)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    assert tool is None and reg.list() == []


async def test_shadow_accumulation_and_graduation(tmp_path):
    ts, reg, sm = make_toolsmith(tmp_path, stage="ask_first")
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)  # -> shadow
    # A generated self-test verifies code but earns no operational evidence.
    assert reg.get(tool.tool_id).shadow_runs == 0
    # Five distinct, same-input incumbent/candidate comparisons qualify it.
    await qualify_tool(ts, reg, tool)
    cands = ts.graduation_candidates()
    assert len(cands) == 1 and cands[0].shadow_runs >= 5
    # shadow outcomes recorded to trust
    assert any(c[0] == "toolsmith" and c[2].get("shadow")
               for c in sm.competence.calls)
    result = ts.graduate(
        tool.tool_id, authority=graduation_authority(reg.get(tool.tool_id)))
    assert not result["replayed"]
    assert reg.get(tool.tool_id).status == ToolStatus.LIVE


async def test_shadow_comparison_is_digest_bound_and_idempotent(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    kwargs = dict(
        captured_input={"a": 2, "b": 3},
        incumbent_output={"sum": 5},
        capture_id="capture-replay-01",
        capture_source="captured",
        principal_id="toolsmith-evaluator",
    )
    passed, first = await ts.verify_shadow_run(reg.get(tool.tool_id), **kwargs)
    assert passed and not first["replayed"]
    passed, replay = await ts.verify_shadow_run(reg.get(tool.tool_id), **kwargs)
    assert passed and replay["replayed"]
    assert reg.get(tool.tool_id).shadow_runs == 1
    with pytest.raises(ValueError, match="conflicting content"):
        await ts.verify_shadow_run(
            reg.get(tool.tool_id),
            **{**kwargs, "incumbent_output": {"sum": 500}},
        )
    audit = reg.audit_projection(tool.tool_id)
    assert len(audit["shadow_comparisons"]) == 1
    assert "captured_input" not in json.dumps(audit)


async def test_shadow_mismatch_is_failure_not_graduation_evidence(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    passed, detail = await ts.verify_shadow_run(
        reg.get(tool.tool_id), captured_input={"a": 1, "b": 1},
        incumbent_output={"sum": 99}, capture_id="capture-mismatch-01",
        principal_id="toolsmith-evaluator")
    assert not passed and detail["deterministic"] and not detail["matched"]
    assert reg.get(tool.tool_id).failures == 1
    with pytest.raises(GraduationAuthorityError,
                       match="clean same-input comparisons"):
        ts.graduate(tool.tool_id,
                    authority=graduation_authority(reg.get(tool.tool_id)))


async def test_legacy_shadow_counter_cannot_replace_comparison_receipts(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    reg._conn.execute(
        "UPDATE tools SET shadow_runs=99 WHERE tool_id=?", (tool.tool_id,))
    reg._conn.commit()
    current = reg.get(tool.tool_id)
    assert current.shadow_runs == 99
    assert reg.clean_comparison_count(tool.tool_id) == 0
    assert ts.graduation_candidates() == []
    with pytest.raises(GraduationAuthorityError,
                       match="clean same-input comparisons"):
        ts.graduate(current.tool_id, authority=graduation_authority(current))


async def test_failing_shadow_blocks_graduation(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    # a failing shadow run (sandbox forced to a failing verdict)
    ts._sandbox = FakeSandbox(force={
        "ran": True, "result": {"stdout": "@@VERDICT@@ {\"passed\": false}",
                                "exit_code": 1, "stderr": ""}})
    await ts.verify_shadow_run(
        reg.get(tool.tool_id), captured_input={"a": 1, "b": 1},
        incumbent_output={"sum": 2}, capture_id="capture-failure-01",
        principal_id="toolsmith-evaluator")
    assert ts.graduation_candidates() == []
    assert reg.get(tool.tool_id).failures >= 1


# --- live invocation + dynamic exposure -----------------------------------

async def test_invoke_live_runs_in_sandbox(tmp_path):
    ts, reg, sm = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    await qualify_tool(ts, reg, tool)
    ts.graduate(tool.tool_id,
                authority=graduation_authority(reg.get(tool.tool_id)))
    out = await ts.invoke_live(tool.tool_id, {"a": 4, "b": 5})
    assert out["result"]["sum"] == 9
    assert reg.get(tool.tool_id).invocations == 1


async def test_dynamic_provider_exposes_live(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)          # shadow -> not exposed
    provider = ts.build_dynamic_provider()
    assert provider() == {}
    await qualify_tool(ts, reg, tool)
    ts.graduate(tool.tool_id,
                authority=graduation_authority(reg.get(tool.tool_id)))
    exposed = provider()
    assert "add_numbers" in exposed
    definition, handler = exposed["add_numbers"]
    assert definition["function"]["name"] == "add_numbers"
    result = json.loads(await handler({"a": 1, "b": 2}))
    assert result["result"]["sum"] == 3


def test_executor_merges_dynamic_defs(tmp_path):
    from colony_sidecar.reasoning.executor import ToolExecutor
    ts, reg, _ = make_toolsmith(tmp_path)
    te = ToolExecutor()

    async def add_numbers(_arguments):
        return "3"

    te.set_dynamic_provider(lambda: {
        "add_numbers": ({"type": "function",
                         "function": {"name": "add_numbers",
                                      "description": "d",
                                      "parameters": {}}}, add_numbers)})
    names = [d["function"]["name"] for d in te.get_definitions()]
    assert "add_numbers" in names


# --- API -------------------------------------------------------------------

@asynccontextmanager
async def _client(ts, authority=None):
    orig = host_mod._toolsmith
    host_mod._toolsmith = ts
    app = FastAPI()

    @app.middleware("http")
    async def _bind_authority(request, call_next):
        request.state.colony_authority = (
            authority if authority is not None else scoped_toolsmith_authority()
        )
        return await call_next(request)

    app.include_router(host_mod.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            yield c
    finally:
        host_mod._toolsmith = orig


async def test_api_list_graduate_retire(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    await qualify_tool(ts, reg, tool)
    async with _client(ts) as c:
        r = await c.get("/v1/host/self/tools")
        assert r.status_code == 200 and r.json()["available"]
        payload = authority_payload(reg.get(tool.tool_id))
        r = await c.post(
            f"/v1/host/self/tools/{tool.tool_id}/graduate", json=payload)
        assert r.status_code == 200
        assert reg.get(tool.tool_id).status == ToolStatus.LIVE
        # Exact retry is idempotent; it cannot consume a second use.
        r = await c.post(
            f"/v1/host/self/tools/{tool.tool_id}/graduate", json=payload)
        assert r.status_code == 200 and r.json()["replayed"]
        r = await c.post(f"/v1/host/self/tools/{tool.tool_id}/retire")
        assert r.status_code == 200
        assert reg.get(tool.tool_id).status == ToolStatus.RETIRED


async def test_api_shadow_comparison_is_transport_attested(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    payload = {
        "capture_id": "capture-api-0001",
        "captured_input": {"a": 10, "b": 2},
        "incumbent_output": {"sum": 12},
        "capture_source": "production_trace",
    }
    async with _client(ts) as c:
        response = await c.post(
            f"/v1/host/self/tools/{tool.tool_id}/shadow-compare",
            json=payload,
        )
        assert response.status_code == 200 and response.json()["passed"]
        assert '"sum"' not in response.text
    audit = reg.audit_projection(tool.tool_id)
    assert audit["shadow_comparisons"][0]["principal_id"] == "operator-deck"
    async with _client(ts, anonymous_authority()) as c:
        response = await c.post(
            f"/v1/host/self/tools/{tool.tool_id}/shadow-compare",
            json={**payload, "capture_id": "capture-api-0002"},
        )
        assert response.status_code == 403


async def test_api_unavailable():
    async with _client(None) as c:
        assert (await c.get("/v1/host/self/tools")).json() == {"available": False}


async def test_api_graduation_rejects_anonymous_and_digest_changes(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    await qualify_tool(ts, reg, tool)
    payload = authority_payload(reg.get(tool.tool_id))
    async with _client(ts, anonymous_authority()) as c:
        response = await c.post(
            f"/v1/host/self/tools/{tool.tool_id}/graduate", json=payload)
        assert response.status_code == 403
    payload["expected_artifact_digest"] = "0" * 64
    async with _client(ts) as c:
        response = await c.post(
            f"/v1/host/self/tools/{tool.tool_id}/graduate", json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "artifact_digest_mismatch"


def test_toolsmith_mutations_have_exact_middleware_scopes():
    assert required_scope(
        "POST", "/v1/host/self/tools/tool-abc/shadow-compare"
    ) == "toolsmith:evaluate"
    assert required_scope(
        "POST", "/v1/host/self/tools/tool-abc/graduate"
    ) == "toolsmith:graduate"


def test_graduation_authority_rejects_expired_and_multi_use():
    now = datetime.now(timezone.utc)
    base = {
        "authority_id": "authority-bounds-01",
        "decision_id": "decision-bounds-01",
        "expected_candidate_digest": "1" * 64,
        "expected_artifact_digest": "2" * 64,
        "issued_at": (now - timedelta(minutes=10)).isoformat(),
        "expires_at": (now - timedelta(minutes=1)).isoformat(),
        "max_uses": 1,
    }
    with pytest.raises(GraduationAuthorityError, match="expired"):
        GraduationAuthorityV1.from_request(
            base, tool_id="tool-bounds-01", principal_id="operator-deck",
            owner_person_id="owner", now=now)
    with pytest.raises(GraduationAuthorityError, match="max_uses=1"):
        GraduationAuthorityV1.from_request(
            {**base,
             "issued_at": now.isoformat(),
             "expires_at": (now + timedelta(minutes=5)).isoformat(),
             "max_uses": 2},
            tool_id="tool-bounds-01", principal_id="operator-deck",
            owner_person_id="owner", now=now)


async def test_graduation_authority_cannot_be_rebound_to_another_tool(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    first = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(first)
    await qualify_tool(ts, reg, first)
    first = reg.get(first.tool_id)
    ts.graduate(first.tool_id, authority=graduation_authority(first))

    second = reg.create_draft(
        name="add_numbers_alt", description="Return another bounded sum.",
        source_code=GOOD_SPEC["source_code"],
        input_schema=GOOD_SPEC["input_schema"],
        test_source=GOOD_SPEC["test_source"],
        candidate_digest="3" * 64,
    )
    await ts.verify(second)
    await qualify_tool(ts, reg, second)
    second = reg.get(second.tool_id)
    rebound = graduation_authority(
        second, authority_id="authority-0001", decision_id="decision-0002")
    with pytest.raises(GraduationAuthorityError, match="used for another"):
        ts.graduate(second.tool_id, authority=rebound)
    assert reg.get(second.tool_id).status == ToolStatus.SHADOW


async def test_act_first_never_auto_graduates_tool(tmp_path):
    from colony_sidecar.autonomy.loop import AutonomyLoop
    from colony_sidecar.toolsmith.miner import ToolCandidate
    ts, reg, _ = make_toolsmith(tmp_path, stage="act_first")
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    await qualify_tool(ts, reg, tool)
    fake_loop = SimpleNamespace(
        _registry=SimpleNamespace(delivery=None),
        _route_reachout_delivery=None,
    )
    await AutonomyLoop._toolsmith_propose_graduations(fake_loop, ts)
    assert reg.get(tool.tool_id).status == ToolStatus.SHADOW
