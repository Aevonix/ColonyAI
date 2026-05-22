"""Tests for SessionReportStore and the /session-report /context-digest endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from colony_sidecar.sessions.reports import SessionReport, SessionReportStore
from colony_sidecar.api.schemas.host import (
    SessionReportRequest,
    ContextDigestResponse,
    AgentSnapshotSystemState,
)


class TestSessionReportStore:
    """Unit tests for the in-memory SessionReportStore."""

    @pytest.fixture
    def store(self):
        return SessionReportStore(max_per_contact=3)

    @pytest.fixture
    def sample_report(self):
        return SessionReport(
            report_id="r-1",
            session_id="s-1",
            contact_id="whatsapp:+1XXXXXX",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            ended_at=datetime.now(timezone.utc),
            summary="Test session",
            topics=["test"],
            resolutions=["fixed"],
            pending=["nothing"],
            notified_user=False,
            metadata={},
        )

    @pytest.mark.asyncio
    async def test_add_report(self, store, sample_report):
        rid = await store.add_report(sample_report)
        assert rid == "r-1"
        recent = await store.get_recent("whatsapp:+1XXXXXX", hours=24, limit=10)
        assert len(recent) == 1
        assert recent[0].summary == "Test session"

    @pytest.mark.asyncio
    async def test_eviction(self, store, sample_report):
        """Only max_per_contact reports are kept."""
        for i in range(5):
            r = SessionReport(
                report_id=f"r-{i}",
                session_id=f"s-{i}",
                contact_id="whatsapp:+1XXXXXX",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                ended_at=datetime.now(timezone.utc),
                summary=f"Report {i}",
                topics=[],
                resolutions=[],
                pending=[],
                notified_user=False,
                metadata={},
            )
            await store.add_report(r)

        recent = await store.get_recent("whatsapp:+1XXXXXX", hours=24, limit=10)
        assert len(recent) == 3  # max_per_contact=3
        assert recent[0].summary == "Report 2"  # oldest of the retained 3
        assert recent[-1].summary == "Report 4"  # newest

    @pytest.mark.asyncio
    async def test_hours_filter(self, store, sample_report):
        old = SessionReport(
            report_id="r-old",
            session_id="s-old",
            contact_id="whatsapp:+1XXXXXX",
            started_at=datetime.now(timezone.utc) - timedelta(hours=72),
            ended_at=datetime.now(timezone.utc) - timedelta(hours=72),
            summary="Old report",
            topics=[],
            resolutions=[],
            pending=[],
            notified_user=False,
            metadata={},
        )
        await store.add_report(old)
        await store.add_report(sample_report)

        recent = await store.get_recent("whatsapp:+1XXXXXX", hours=24, limit=10)
        assert len(recent) == 1
        assert recent[0].summary == "Test session"

    @pytest.mark.asyncio
    async def test_unknown_contact(self, store):
        recent = await store.get_recent("unknown", hours=24, limit=10)
        assert recent == []

    def test_to_dict(self, store, sample_report):
        import asyncio

        asyncio.run(store.add_report(sample_report))
        d = store.to_dict()
        assert d["contacts"] == 1
        assert d["total_reports"] == 1


class TestSessionReportEndpoint:
    """Integration tests for POST /v1/host/session-report."""

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from colony_sidecar.api.routers.host import (
            set_telemetry,
            set_autonomy_loop,
            set_session_report_store,
            set_initiative_store,
        )
        from colony_sidecar.server import create_app
        from colony_sidecar.telemetry import TelemetryStore
        from colony_sidecar.initiatives.store import InitiativeStore

        monkeypatch.setenv("COLONY_API_KEY", "test-api-key")

        telemetry = TelemetryStore()
        set_telemetry(telemetry)

        initiative_store = InitiativeStore(state_dir=tmp_path)
        set_initiative_store(initiative_store)

        autonomy_loop = Mock()
        autonomy_loop.config.mode.value = "proactive"
        autonomy_loop.is_running = True
        set_autonomy_loop(autonomy_loop)

        session_report_store = SessionReportStore()
        set_session_report_store(session_report_store)

        app = create_app()
        return TestClient(app, headers={"Authorization": "Bearer test-api-key"})

    def test_store_report(self, client: TestClient):
        payload = {
            "session_id": "s-abc",
            "contact_id": "whatsapp:+1XXXXXX",
            "started_at": "2026-05-22T15:00:00Z",
            "ended_at": "2026-05-22T15:30:00Z",
            "summary": "Resolved auth issues",
            "topics": ["auth"],
            "resolutions": ["fixed header"],
            "pending": ["deploy"],
            "notified_user": False,
            "metadata": {"tools": ["patch"]},
        }
        resp = client.post("/v1/host/session-report", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["stored"] is True
        assert data["report_id"] is not None

    def test_store_without_ended_at(self, client: TestClient):
        payload = {
            "session_id": "s-def",
            "contact_id": "whatsapp:+1XXXXXX",
            "started_at": "2026-05-22T15:00:00Z",
            "summary": "Ongoing session",
            "topics": [],
            "resolutions": [],
            "pending": [],
            "notified_user": False,
            "metadata": {},
        }
        resp = client.post("/v1/host/session-report", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["stored"] is True

    def test_store_store_not_ready(self, client: TestClient, monkeypatch):
        """Return 501 if the store global is None."""
        import colony_sidecar.api.routers.host as host_router

        monkeypatch.setattr(host_router, "_session_report_store", None)
        resp = client.post(
            "/v1/host/session-report",
            json={
                "session_id": "x",
                "contact_id": "c",
                "started_at": "2026-05-22T15:00:00Z",
                "summary": "x",
            },
        )
        assert resp.status_code == 501


class TestContextDigestEndpoint:
    """Integration tests for GET /v1/host/context-digest."""

    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        from colony_sidecar.api.routers.host import (
            set_telemetry,
            set_autonomy_loop,
            set_session_report_store,
            set_initiative_store,
        )
        from colony_sidecar.server import create_app
        from colony_sidecar.telemetry import TelemetryStore
        from colony_sidecar.initiatives.store import InitiativeStore

        monkeypatch.setenv("COLONY_API_KEY", "test-api-key")

        telemetry = TelemetryStore()
        set_telemetry(telemetry)

        initiative_store = InitiativeStore(state_dir=tmp_path)
        set_initiative_store(initiative_store)

        autonomy_loop = Mock()
        autonomy_loop.config.mode.value = "proactive"
        autonomy_loop.is_running = True
        set_autonomy_loop(autonomy_loop)

        session_report_store = SessionReportStore()
        set_session_report_store(session_report_store)

        app = create_app()
        return TestClient(app, headers={"Authorization": "Bearer test-api-key"})

    def test_digest_structure(self, client: TestClient):
        resp = client.get(
            "/v1/host/context-digest?contact_id=whatsapp:+1XXXXXX&hours=24&initiative_limit=5"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "generated_at" in data
        assert "contact_id" in data
        assert "session_reports" in data
        assert "pending_initiatives" in data
        assert "system_state" in data
        assert "last_outreach" in data

    def test_digest_system_state(self, client: TestClient):
        resp = client.get("/v1/host/context-digest")
        assert resp.status_code == 200
        data = resp.json()
        state = data["system_state"]
        assert "autonomy_running" in state
        assert "mode" in state
        assert "silence_hours" in state
        assert "stale_flags" in state

    def test_digest_unknown_contact(self, client: TestClient):
        resp = client.get("/v1/host/context-digest?contact_id=unknown")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_reports"] == []

    def test_digest_with_stored_reports(self, client: TestClient):
        # Store a report first
        payload = {
            "session_id": "s-digest",
            "contact_id": "whatsapp:+1XXXXXX",
            "started_at": "2026-05-22T15:00:00Z",
            "ended_at": "2026-05-22T15:30:00Z",
            "summary": "Digest test session",
            "topics": ["digest"],
            "resolutions": ["done"],
            "pending": [],
            "notified_user": True,
            "metadata": {},
        }
        post_resp = client.post("/v1/host/session-report", json=payload)
        assert post_resp.status_code == 200

        # Now fetch digest (note: + must be URL-encoded as %2B in query params)
        from urllib.parse import quote
        encoded = quote(payload["contact_id"], safe="")
        resp = client.get(
            f"/v1/host/context-digest?contact_id={encoded}&hours=24"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["session_reports"]) == 1
        assert data["session_reports"][0]["summary"] == "Digest test session"
        assert data["session_reports"][0]["notified_user"] is True


class TestSchemaValidation:
    """Validate Pydantic schemas construct and serialize correctly."""

    def test_session_report_request(self):
        req = SessionReportRequest(
            session_id="s-1",
            contact_id="c-1",
            started_at="2026-05-22T15:00:00Z",
            summary="test",
        )
        assert req.contact_id == "c-1"
        assert req.notified_user is False
        assert req.metadata == {}

    def test_context_digest_response(self):
        resp = ContextDigestResponse(
            generated_at="2026-05-22T16:00:00Z",
            system_state=AgentSnapshotSystemState(
                autonomy_running=True,
                mode="proactive",
            ),
            last_outreach={"at": None, "reason": None},
        )
        d = resp.model_dump()
        assert d["generated_at"] == "2026-05-22T16:00:00Z"
        assert d["system_state"]["mode"] == "proactive"
