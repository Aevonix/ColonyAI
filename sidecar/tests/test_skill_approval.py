"""Tests for /skills/drafts, /skills/{id}/approve, /skills/{id}/reject."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from colony_sidecar.api.routers import host as host_mod


class _FakeRegistry:
    """In-memory registry that satisfies the host router's interface."""

    def __init__(self):
        self._by_id = {}

    def add_draft(self, skill_id: str, name: str = "", description: str = ""):
        from colony_sidecar.skills.models import SkillStatus
        self._by_id[skill_id] = SimpleNamespace(
            skill_id=skill_id,
            name=name,
            description=description,
            status=SkillStatus.DRAFT,
            created_at=None,
        )

    async def list_all(self, status=None):
        if status is None:
            return list(self._by_id.values())
        return [s for s in self._by_id.values() if s.status == status]

    async def get(self, skill_id: str):
        return self._by_id.get(skill_id)

    async def activate(self, skill_id: str):
        from colony_sidecar.skills.models import SkillStatus
        if skill_id in self._by_id:
            self._by_id[skill_id].status = SkillStatus.ACTIVE

    async def archive(self, skill_id: str):
        from colony_sidecar.skills.models import SkillStatus
        if skill_id in self._by_id:
            self._by_id[skill_id].status = SkillStatus.ARCHIVED


@asynccontextmanager
async def _client_with_registry(registry):
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


@pytest.mark.asyncio
async def test_list_drafts_returns_only_draft_skills():
    reg = _FakeRegistry()
    reg.add_draft("s1", name="skill1")
    reg.add_draft("s2", name="skill2")
    async with _client_with_registry(reg) as client:
        resp = await client.get("/v1/host/skills/drafts")
        assert resp.status_code == 200
        data = resp.json()
        ids = {d["id"] for d in data["drafts"]}
        assert ids == {"s1", "s2"}


@pytest.mark.asyncio
async def test_approve_flips_to_active():
    reg = _FakeRegistry()
    reg.add_draft("s1")
    async with _client_with_registry(reg) as client:
        resp = await client.post("/v1/host/skills/s1/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"
    # Subsequent list_drafts should no longer include it.
    async with _client_with_registry(reg) as client:
        drafts = (await client.get("/v1/host/skills/drafts")).json()["drafts"]
        assert all(d["id"] != "s1" for d in drafts)


@pytest.mark.asyncio
async def test_reject_archives_skill():
    reg = _FakeRegistry()
    reg.add_draft("s1")
    async with _client_with_registry(reg) as client:
        resp = await client.post("/v1/host/skills/s1/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"


@pytest.mark.asyncio
async def test_approve_missing_skill_returns_404():
    reg = _FakeRegistry()
    async with _client_with_registry(reg) as client:
        resp = await client.post("/v1/host/skills/nope/approve")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approve_without_registry_returns_503():
    async with _client_with_registry(None) as client:
        resp = await client.post("/v1/host/skills/s1/approve")
        assert resp.status_code == 503
