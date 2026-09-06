"""Exact guest recall works without P8 and never queries private legacy producers."""
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.commitments.store import CommitmentStore
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.media import SourceMedia
from test_scoped_api_authority import _principal, _write_keyring
from test_source_claim_projection import Model, claim
from test_source_media import message as image_message
from test_turn_source_evidence import source_app


class PrivateProducer:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError("private legacy producer was queried")


def headers(person):
    return {"Authorization": "Bearer fixture-" + person}


def context(person, query, session="second-session"):
    return {
        "identity": {"host_id": "fixture"},
        "context": {"contact_id": person, "session_id": session},
        "incoming_message": {"role": "user", "content": query},
        "projection_policy": "scoped_viewer_required",
    }


@pytest.mark.asyncio
async def test_guest_http_capture_claim_media_and_commitment_recall_without_p8(
    source_app, tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner")
    monkeypatch.setenv("COLONY_RECALL_RERANK", "off")
    principals = []
    for person in ("guest-a", "guest-b", "owner"):
        principal = _principal(principal=person, secret="fixture-" + person, viewer=person)
        principal["allow_unscoped_api"] = False
        principals.append(principal)
    keyring = tmp_path / "keys.json"
    _write_keyring(keyring, principals)
    source_app.add_middleware(ApiKeyMiddleware, api_key=None, keyring_path=str(keyring))

    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        text = "My office is in River."
        for person, content in (
            ("guest-a", text), ("guest-b", "My office is other-guest-secret."),
            ("owner", "My office is owner-secret."),
        ):
            turn = "turn-" + person
            response = await client.put("/v2/host/turns/" + turn, headers=headers(person), json={
                "identity": {"host_id": "fixture"},
                "context": {"contact_id": person, "session_id": "first-session", "turn_id": turn},
                "user_message": {"role": "user", "content": content},
                "assistant_message": {"role": "assistant", "content": "I will prepare the office handout."} if person == "guest-a" else None,
            })
            assert response.status_code == 201, response.text
        ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
        projection = SourceClaimProjection(ledger)
        assert await projection.process_one(Model({text: claim(text, "River")}))
        ledger.record_source("guest-image", contact_id="guest-a", session_id="first-session",
                             messages=[image_message()])
        media = SourceMedia(ledger)
        assert media.finish(media.claim_job(), description="An orchid beside a red rectangle.", model="fixture-vision")
        ledger.record_source("old-checkpoint", contact_id="guest-a", session_id="first-session", scope="session",
                             messages=[{"role": "user", "content": "office mixed-speaker-secret"}])
        commitments = CommitmentStore(tmp_path / "commitments.db")
        due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        own = commitments.create("guest-a", "Prepare the office handout", due_at=due,
                                 metadata={"source_turn_id": "turn-guest-a"})
        commitments.create("guest-a", "owner-private task ABOUT the guest", due_at=due)
        commitments.create("guest-a", "private task with an invented link", due_at=due,
                           metadata={"source_turn_id": "turn-guest-a"})
        commitments.create("guest-a", "My office is owner-secret", due_at=due,
                           metadata={"source_turn_id": "turn-owner"})
        commitments.create("owner", "owner-secret obligation", due_at=due)
        commitments.create("guest-b", "other-guest-secret obligation", due_at=due)
        monkeypatch.setattr(host, "_commitment_store", commitments)
        private = PrivateProducer()
        for name in ("_graph", "_contacts_store", "_facts_store", "_goals_store", "_initiative_store",
                     "_briefings_engine", "_world_store", "_skills_registry", "_affect_store",
                     "_relationship_profiler", "_preference_learner", "_tom2_store", "_directive_manager",
                     "_engagement_store", "_comms_log", "_surprise_store"):
            monkeypatch.setattr(host, name, private)

        ready = await client.get("/v1/host/context/projection-readiness",
                                 params={"contact_id": "guest-a"}, headers=headers("guest-a"))
        assert ready.status_code == 200
        assert ready.json()["projection_backend"] == "canonical_sources"
        assert ready.json()["p8_mode"] == "off"
        assert ready.json()["scoped_projection_ready"] is True
        assert ready.json()["legacy_global_allowed"] is False
        response = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"), headers=headers("guest-a"))
        assert response.status_code == 200, response.text
        sections = {row["id"]: row["body"] for row in response.json()["sections"]}
        assert "source_assertion" in sections["colony-memory"] and "River" in sections["colony-memory"]
        assert own["id"] in sections["colony-commitments"] and "Prepare the office handout" in sections["colony-commitments"]
        assert set(sections) == {"temporal-context", "colony-memory", "colony-commitments"}
        assert not any(secret in response.text for secret in ("owner-secret", "other-guest-secret", "mixed-speaker-secret", "owner-private", "invented link"))
        assert "omitted" in response.json()["notices"][0]

        image = await client.post("/v1/host/context/assemble", json=context("guest-a", "orchid"), headers=headers("guest-a"))
        assert image.status_code == 200 and "orchid" in image.text and "derived_unverified" in image.text
        other = await client.post("/v1/host/context/assemble", json=context("guest-b", "orchid"), headers=headers("guest-b"))
        assert other.status_code == 200 and "orchid" not in other.text
        forged = await client.post("/v1/host/context/assemble", json=context("owner", "office"), headers=headers("guest-a"))
        assert forged.status_code == 403
        missing = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"))
        assert missing.status_code == 401
        assert private.calls == []

        monkeypatch.setenv("COLONY_RECALL_CONTEXT_MAX_CHARS", "80")
        small = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"), headers=headers("guest-a"))
        assert small.status_code == 200
        assert all(len(row["body"]) <= 80 for row in small.json()["sections"] if row["id"] == "colony-memory")
        assert private.calls == []

        ledger.erase_sources(contact_id="guest-a", turn_ids=["turn-guest-a"])
        erased = await client.post("/v1/host/context/assemble", json=context("guest-a", "office"), headers=headers("guest-a"))
        assert erased.status_code == 200
        assert "colony-commitments" not in erased.text and "River" not in erased.text
        assert private.calls == []
