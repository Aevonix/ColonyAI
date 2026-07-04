"""Hermes↔Colony skills bridge (v0.18.0) — SKILL.md export.

Covers: render format (frontmatter parses back as YAML), env gating
(off by default), foreign-file overwrite protection, the approval-hook
firing the export, and the "skills" observation domain accepting
batches from the plugin.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.skills.hermes_export import (
    HERMES_AUTHOR,
    PROVENANCE_MARKER,
    export_approved_skill,
    export_to_hermes,
    is_procedural,
    load_body_source,
    render_skill_md,
)
from colony_sidecar.skills.models import SkillManifest, SkillStatus


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _manifest(**overrides) -> SkillManifest:
    now = datetime.now(timezone.utc)
    base = dict(
        skill_id="fetch-weather-report_a1b2c3d4",
        name="Fetch Weather Report",
        version="1.0.0",
        description="Solves: fetch the weather report for a city",
        author_colony_id="colony-test",
        created_at=now,
        updated_at=now,
        status=SkillStatus.DRAFT,
        tags=["weather", "fetch"],
        dependencies=["requests"],
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
        output_schema={"type": "string"},
        origin_task_id="task-123",
    )
    base.update(overrides)
    return SkillManifest(**base)


PROCEDURAL_PATTERN = {
    "docstring": (
        "Solves: fetch the weather report for a city\n\n"
        "Capture steps:\n  1. web_search(weather in city)\n  2. summarize(report)"
    ),
    "step_sequence": [
        "web_search(weather in city)",
        "summarize(report)",
    ],
    "source_code": (
        "async def run(colony, city: string):\n"
        "    _r0 = await colony.tools.invoke('web_search', {'query': city})\n"
        "    return _r0\n"
    ),
}


def _frontmatter(text: str) -> str:
    assert text.startswith("---\n")
    end = text.find("\n---", 3)
    assert end != -1, "frontmatter not closed"
    return text[4:end]


# ---------------------------------------------------------------------------
# render_skill_md
# ---------------------------------------------------------------------------

class TestRenderSkillMd:
    def test_frontmatter_parses_back_as_yaml(self):
        md = render_skill_md(_manifest(), PROCEDURAL_PATTERN)
        fm = yaml.safe_load(_frontmatter(md))
        assert fm["name"] == "fetch-weather-report-a1b2c3d4"  # slug of skill_id
        assert fm["description"] == "Solves: fetch the weather report for a city"
        assert fm["version"] == "1.0.0"
        assert fm["author"] == HERMES_AUTHOR
        assert fm["metadata"]["hermes"]["tags"] == ["weather", "fetch"]

    def test_provenance_comment_block_in_frontmatter(self):
        md = render_skill_md(_manifest(), PROCEDURAL_PATTERN)
        fm_raw = _frontmatter(md)
        assert f"# {PROVENANCE_MARKER}" in fm_raw
        assert "colony_skill_id" in fm_raw
        assert "fetch-weather-report_a1b2c3d4" in fm_raw
        assert "origin_task_id" in fm_raw
        assert "task-123" in fm_raw

    def test_body_has_procedure_steps_and_usage_notes(self):
        md = render_skill_md(_manifest(), PROCEDURAL_PATTERN)
        body = md.split("\n---", 1)[1]
        assert "# Fetch Weather Report" in body
        assert "## What this skill does" in body
        assert "## When to use this skill" in body
        assert "## Procedure" in body
        assert "1. web_search(weather in city)" in body
        assert "2. summarize(report)" in body
        assert "## Usage notes" in body
        assert "Input `city` (string, required)" in body
        assert "Produces a string result" in body
        assert "Depends on: requests" in body

    def test_steps_fall_back_to_docstring_numbered_lines(self):
        pattern = dict(PROCEDURAL_PATTERN, step_sequence=[])
        md = render_skill_md(_manifest(), pattern)
        assert "1. web_search(weather in city)" in md

    def test_renders_without_pattern_data(self):
        md = render_skill_md(_manifest(), {})
        assert "## Procedure" in md
        assert "No step-by-step trace was captured" in md
        # Frontmatter still parses
        fm = yaml.safe_load(_frontmatter(md))
        assert fm["author"] == HERMES_AUTHOR


# ---------------------------------------------------------------------------
# Procedural heuristic
# ---------------------------------------------------------------------------

class TestProceduralHeuristic:
    def test_trace_steps_are_procedural(self):
        assert is_procedural(_manifest(), PROCEDURAL_PATTERN) is True

    def test_tool_invoke_in_source_is_procedural(self):
        p = {"source_code": "async def run(colony):\n    await colony.tools.invoke('x', {})\n"}
        assert is_procedural(_manifest(), p) is True

    def test_mcp_bridged_skill_is_not_procedural(self):
        m = _manifest(origin="mcp", mcp_server="github", mcp_tool="list_prs")
        assert is_procedural(m, PROCEDURAL_PATTERN) is False

    def test_pure_computation_source_is_not_procedural(self):
        p = {"source_code": "def run(x: int) -> int:\n    return x * 2\n"}
        assert is_procedural(_manifest(), p) is False

    def test_no_pattern_data_exports_anyway(self):
        # When in doubt, export — gated by env + approval already.
        assert is_procedural(_manifest(), {}) is True
        assert is_procedural(_manifest(), None) is True

    def test_load_body_source_recovers_steps_from_skill_dir(self, tmp_path):
        skill_dir = tmp_path / "skill_v1"
        skill_dir.mkdir()
        (skill_dir / "skill.py").write_text(
            'async def run(colony, city: str):\n'
            '    """Auto-generated skill — replays captured tool sequence.\n\n'
            '    Original task: fetch the weather\n'
            '    """\n'
            "    _r0 = await colony.tools.invoke('web_search', {'query': city})\n"
            "    return _r0\n",
            encoding="utf-8",
        )
        m = _manifest(skill_dir=str(skill_dir))
        body = load_body_source(m)
        assert "colony.tools.invoke" in body["source_code"]
        assert body["step_sequence"] == ["Invoke the `web_search` tool"]
        assert "Original task: fetch the weather" in body["docstring"]


# ---------------------------------------------------------------------------
# export_to_hermes — gating + overwrite protection
# ---------------------------------------------------------------------------

class TestExportToHermes:
    def test_disabled_by_default_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COLONY_EMIT_HERMES_SKILLS", raising=False)
        out = export_to_hermes(_manifest(), PROCEDURAL_PATTERN, base_dir=tmp_path)
        assert out is None
        assert list(tmp_path.iterdir()) == []

    def test_explicit_false_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "false")
        assert export_to_hermes(_manifest(), PROCEDURAL_PATTERN, base_dir=tmp_path) is None

    def test_enabled_writes_skill_md(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
        out = export_to_hermes(_manifest(), PROCEDURAL_PATTERN, base_dir=tmp_path)
        assert out == tmp_path / "fetch-weather-report-a1b2c3d4" / "SKILL.md"
        text = out.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert PROVENANCE_MARKER in text
        # No temp files left behind
        assert [p.name for p in out.parent.iterdir()] == ["SKILL.md"]

    def test_base_dir_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "1")
        monkeypatch.setenv("COLONY_HERMES_SKILLS_DIR", str(tmp_path / "via-env"))
        out = export_to_hermes(_manifest(), PROCEDURAL_PATTERN)
        assert out is not None
        assert out.parent.parent == tmp_path / "via-env"

    def test_never_overwrites_foreign_skill_md(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
        target_dir = tmp_path / "fetch-weather-report-a1b2c3d4"
        target_dir.mkdir(parents=True)
        foreign = (
            "---\nname: fetch-weather-report-a1b2c3d4\n"
            "description: hand-written by a human\nauthor: sam\n---\n\n# Mine\n"
        )
        (target_dir / "SKILL.md").write_text(foreign, encoding="utf-8")

        out = export_to_hermes(_manifest(), PROCEDURAL_PATTERN, base_dir=tmp_path)
        assert out is None
        assert (target_dir / "SKILL.md").read_text(encoding="utf-8") == foreign

    def test_overwrites_own_previous_export(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
        first = export_to_hermes(_manifest(), PROCEDURAL_PATTERN, base_dir=tmp_path)
        assert first is not None
        second = export_to_hermes(
            _manifest(description="Solves: fetch the weather report v2"),
            PROCEDURAL_PATTERN,
            base_dir=tmp_path,
        )
        assert second == first
        assert "v2" in second.read_text(encoding="utf-8")

    def test_export_approved_skill_skips_mcp(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
        monkeypatch.setenv("COLONY_HERMES_SKILLS_DIR", str(tmp_path))
        m = _manifest(origin="mcp", mcp_server="github", mcp_tool="list_prs")
        assert export_approved_skill(m, PROCEDURAL_PATTERN) is None
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Approval-transition hook (DRAFT→ACTIVE fires export)
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Mirrors the host router's expected registry surface."""

    def __init__(self):
        self._by_id = {}

    def add_draft(self, manifest):
        manifest.status = SkillStatus.DRAFT
        self._by_id[manifest.skill_id] = manifest

    async def list_all(self, status=None):
        if status is None:
            return list(self._by_id.values())
        return [s for s in self._by_id.values() if s.status == status]

    async def get(self, skill_id):
        return self._by_id.get(skill_id)

    async def activate(self, skill_id):
        if skill_id in self._by_id:
            self._by_id[skill_id].status = SkillStatus.ACTIVE


@asynccontextmanager
async def _client_with_registry(registry):
    from colony_sidecar.api.routers import host as host_mod

    prev = host_mod._skills_registry
    host_mod._skills_registry = registry
    app = FastAPI()
    app.include_router(host_mod.router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        host_mod._skills_registry = prev


class TestApprovalHook:
    @pytest.mark.asyncio
    async def test_approve_fires_hermes_export(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
        monkeypatch.setenv("COLONY_HERMES_SKILLS_DIR", str(tmp_path))

        skill_dir = tmp_path / "library" / "fetch-weather_v1.0.0"
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.py").write_text(
            "async def run(colony, city: str):\n"
            "    _r0 = await colony.tools.invoke('web_search', {'query': city})\n"
            "    return _r0\n",
            encoding="utf-8",
        )
        manifest = _manifest(skill_dir=str(skill_dir))

        reg = _FakeRegistry()
        reg.add_draft(manifest)
        async with _client_with_registry(reg) as client:
            resp = await client.post(f"/v1/host/skills/{manifest.skill_id}/approve")
            assert resp.status_code == 200
            assert resp.json()["status"] == "active"

        exported = tmp_path / "fetch-weather-report-a1b2c3d4" / "SKILL.md"
        assert exported.is_file()
        text = exported.read_text(encoding="utf-8")
        assert PROVENANCE_MARKER in text
        assert "Invoke the `web_search` tool" in text

    @pytest.mark.asyncio
    async def test_approve_with_export_disabled_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("COLONY_EMIT_HERMES_SKILLS", raising=False)
        monkeypatch.setenv("COLONY_HERMES_SKILLS_DIR", str(tmp_path))

        manifest = _manifest()
        reg = _FakeRegistry()
        reg.add_draft(manifest)
        async with _client_with_registry(reg) as client:
            resp = await client.post(f"/v1/host/skills/{manifest.skill_id}/approve")
            assert resp.status_code == 200
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_export_failure_does_not_block_approval(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
        monkeypatch.setenv("COLONY_HERMES_SKILLS_DIR", str(tmp_path))
        import colony_sidecar.skills.hermes_export as hx

        def _boom(*a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(hx, "export_to_hermes", _boom)

        manifest = _manifest()
        reg = _FakeRegistry()
        reg.add_draft(manifest)
        async with _client_with_registry(reg) as client:
            resp = await client.post(f"/v1/host/skills/{manifest.skill_id}/approve")
            assert resp.status_code == 200
            assert resp.json()["status"] == "active"

    @pytest.mark.asyncio
    async def test_approval_hook_skips_legacy_namespace_manifest(self, tmp_path, monkeypatch):
        # Manifests from older registries can be bare objects without
        # pattern data: "in doubt" → exported anyway when enabled.
        monkeypatch.setenv("COLONY_EMIT_HERMES_SKILLS", "true")
        monkeypatch.setenv("COLONY_HERMES_SKILLS_DIR", str(tmp_path))
        bare = SimpleNamespace(
            skill_id="bare-skill_99",
            name="Bare Skill",
            description="A skill with no packaged dir",
            status=SkillStatus.DRAFT,
            created_at=None,
        )
        reg = _FakeRegistry()
        reg._by_id[bare.skill_id] = bare
        async with _client_with_registry(reg) as client:
            resp = await client.post("/v1/host/skills/bare-skill_99/approve")
            assert resp.status_code == 200
        assert (tmp_path / "bare-skill-99" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# Observations: "skills" domain accepts plugin batches
# ---------------------------------------------------------------------------

class TestSkillsObservationDomain:
    @pytest.fixture
    def client(self, tmp_path):
        from fastapi.testclient import TestClient
        from colony_sidecar.api.routers import observations as obs_router
        from colony_sidecar.observations.store import ObservationStore

        store = ObservationStore(state_dir=tmp_path)
        app = FastAPI()
        app.include_router(obs_router.router)
        obs_router.set_observation_store(store)
        yield TestClient(app)
        obs_router.set_observation_store(None)
        store.close()

    def test_skills_domain_is_known(self):
        from colony_sidecar.observations.store import (
            OBSERVATION_DOMAINS,
            OBSERVATION_SYNC_INTERVALS,
        )

        assert "skills" in OBSERVATION_DOMAINS
        assert "skills" in OBSERVATION_SYNC_INTERVALS

    def test_skills_batch_accepted_and_listed(self, client):
        resp = client.post(
            "/v1/host/observations",
            json={
                "domain": "skills",
                "reported_by": "hermes-plugin",
                "observations": [
                    {
                        "entity_id": "pdf-tools",
                        "payload": {
                            "description": "Work with PDF files",
                            "tags": ["pdf", "documents"],
                            "path": "/home/u/.hermes/skills/documents/pdf-tools/SKILL.md",
                            "source": "hermes",
                        },
                    },
                    {
                        "entity_id": "fetch-weather-report-a1b2c3d4",
                        "payload": {"description": "Colony export", "source": "hermes"},
                    },
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "recorded", "domain": "skills", "written": 2}

        listed = client.get("/v1/host/observations/skills").json()
        assert listed["total"] == 2
        by_id = {o["entity_id"]: o for o in listed["observations"]}
        assert by_id["pdf-tools"]["payload"]["tags"] == ["pdf", "documents"]
        assert by_id["pdf-tools"]["reported_by"] == "hermes-plugin"

    def test_skills_domain_never_gets_sync_action(self):
        from colony_sidecar.initiatives.action_registry import OBSERVATION_SYNC_ACTIONS

        assert "skills" not in OBSERVATION_SYNC_ACTIONS
