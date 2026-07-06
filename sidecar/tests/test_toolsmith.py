"""Toolsmith loop (Mind M1): registry, miner, draft/verify/graduate, exposure."""

import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import colony_sidecar.api.routers.host as host_mod
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
            approved=False):
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


async def test_shadow_accumulation_and_graduation(tmp_path):
    ts, reg, sm = make_toolsmith(tmp_path, stage="ask_first")
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)  # -> shadow
    # 5 clean shadow runs
    for _ in range(5):
        passed, _ = await ts.verify_shadow_run(reg.get(tool.tool_id))
        assert passed
    cands = ts.graduation_candidates()
    assert len(cands) == 1 and cands[0].shadow_runs >= 5
    # shadow outcomes recorded to trust
    assert any(c[0] == "toolsmith" and c[2].get("shadow")
               for c in sm.competence.calls)
    assert ts.graduate(tool.tool_id)
    assert reg.get(tool.tool_id).status == ToolStatus.LIVE


async def test_failing_shadow_blocks_graduation(tmp_path):
    ts, reg, _ = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    # a failing shadow run (sandbox forced to a failing verdict)
    ts._sandbox = FakeSandbox(force={
        "ran": True, "result": {"stdout": "@@VERDICT@@ {\"passed\": false}",
                                "exit_code": 1, "stderr": ""}})
    await ts.verify_shadow_run(reg.get(tool.tool_id))
    assert ts.graduation_candidates() == []
    assert reg.get(tool.tool_id).failures >= 1


# --- live invocation + dynamic exposure -----------------------------------

async def test_invoke_live_runs_in_sandbox(tmp_path):
    ts, reg, sm = make_toolsmith(tmp_path)
    from colony_sidecar.toolsmith.miner import ToolCandidate
    tool = await ts.draft(ToolCandidate("s", "d", "d", 6))
    await ts.verify(tool)
    ts.graduate(tool.tool_id)
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
    ts.graduate(tool.tool_id)      # live -> exposed
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
    te.set_dynamic_provider(lambda: {
        "add_numbers": ({"type": "function",
                         "function": {"name": "add_numbers",
                                      "description": "d",
                                      "parameters": {}}}, None)})
    names = [d["function"]["name"] for d in te.get_definitions()]
    assert "add_numbers" in names


# --- API -------------------------------------------------------------------

@asynccontextmanager
async def _client(ts):
    orig = host_mod._toolsmith
    host_mod._toolsmith = ts
    app = FastAPI()
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
    async with _client(ts) as c:
        r = await c.get("/v1/host/self/tools")
        assert r.status_code == 200 and r.json()["available"]
        r = await c.post(f"/v1/host/self/tools/{tool.tool_id}/graduate")
        assert r.status_code == 200
        assert reg.get(tool.tool_id).status == ToolStatus.LIVE
        # already-live cannot graduate again
        r = await c.post(f"/v1/host/self/tools/{tool.tool_id}/graduate")
        assert r.status_code == 400
        r = await c.post(f"/v1/host/self/tools/{tool.tool_id}/retire")
        assert r.status_code == 200
        assert reg.get(tool.tool_id).status == ToolStatus.RETIRED


async def test_api_unavailable():
    async with _client(None) as c:
        assert (await c.get("/v1/host/self/tools")).json() == {"available": False}
