"""Natural source ingestion -> durable claims -> scoped context, with no oracle store."""
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.beliefs.source_claims import validated_claims
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.beliefs.source_time import interpret_time_query
from colony_sidecar.turns import TurnIdempotencyLedger
from test_turn_source_evidence import source_app
from test_hermes_turn_outbox import _load_client, _payload


class Model:
    """Controlled extractor output; the source/claims/selection flow is real."""
    def __init__(self, outputs, model="fixture-model-a"):
        self.outputs, self.model, self.calls = outputs, model, []

    def tier_config(self, tier):
        return SimpleNamespace(base_url="http://127.0.0.1:8080/v1")

    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        self.calls.append((payload, kwargs))
        result = dict(self.outputs[payload["message"]])
        if result.pop("match_prior", False):
            result["prior_claim_id"] = payload["prior_assertions"][0]["id"]
        return SimpleNamespace(content=json.dumps([result]), model_id=self.model)


def claim(text, value, **kwargs):
    return {"subject": "I", "predicate": "office_location", "value": value,
            "evidence": text, "operation": "assert", "prior_claim_id": None,
            "valid_from_text": None, "valid_to_text": None, "event_at_text": None, **kwargs}


async def ingest(client, turn, text, *, occurred="2026-03-01T09:00:00+00:00", contact="contact-a", tz="UTC"):
    response = await client.put(f"/v2/host/turns/{turn}", json={
        "identity": {"host_id": "test-host"},
        "context": {"session_id": "session-a", "contact_id": contact, "turn_id": turn,
                    "channel_id": "test:thread-a", "timezone": tz, "metadata": {"occurred_at": occurred}},
        "user_message": {"role": "user", "content": text},
    })
    assert response.status_code == 201, response.text


def prepared(projection, query="office", *, now="2026-03-20T12:00:00+00:00", tz="UTC", contact="contact-a"):
    hits = projection.ledger.search_sources(query, contact_id=contact, session_id="session-b", limit=10)
    _, rows = projection.prepare_context([], hits, contact_id=contact, session_id="session-b",
        time_query=interpret_time_query(query, now=datetime.fromisoformat(now), timezone_name=tz))
    return [json.loads(row["content"]) for row in rows if row.get("atomic_evidence")]


@pytest.mark.asyncio
async def test_ordinary_correction_survives_model_change_restart_and_context(source_app, tmp_path, monkeypatch):
    old, new = "My office is in River.", "Correction: My office is in Lake, not River."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    first = Model({old: claim(old, "River")})
    second = Model({new: claim(new, "Lake", operation="correct", match_prior=True)}, model="fixture-model-b")
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "old", old)
        assert await projection.process_one(first)
        await ingest(client, "new", new, occurred="2026-03-02T09:00:00+00:00")
        assert await projection.process_one(second)
        monkeypatch.setenv("COLONY_RECALL_RERANK", "off")
        response = await client.post("/v1/host/context/assemble", json={
            "identity": {"host_id": "test-host"}, "context": {"contact_id": "contact-a", "session_id": "later"},
            "incoming_message": {"role": "user", "content": "office location"}})
        assert "Lake" in response.text and "source_assertion" in response.text
    reopened = SourceClaimProjection(TurnIdempotencyLedger(projection.ledger.db_path))
    packet = prepared(reopened)[0]
    assert [row["value"] for row in packet["assertions"]] == ["Lake"]
    assert packet["assertions"][0]["operation"] == "correct"
    assert packet["assertions"][0]["prior_claim_id"]
    with sqlite3.connect(projection.ledger.db_path) as conn:
        rows = conn.execute('SELECT data_json,retracted_by FROM source_claims').fetchall()
    assert len(rows) == 2 and any("River" in data and ref for data, ref in rows)
    assert not await reopened.process_one(second)


@pytest.mark.asyncio
async def test_explicit_effective_date_preserves_historical_state(source_app, tmp_path):
    old = "From 2026-03-01, my office is in River."
    new = "Starting 2026-03-12, my office is now in Lake."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({old: claim(old, "River", valid_from_text="2026-03-01"),
                   new: claim(new, "Lake", operation="change", match_prior=True, valid_from_text="2026-03-12")})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "old", old)
        await projection.process_one(model)
        await ingest(client, "new", new, occurred="2026-03-02T09:00:00+00:00")
        await projection.process_one(model)
    past = prepared(projection, "Where was my office as of 2026-03-05?")[0]["assertions"]
    current = prepared(projection, "Where was my office as of March 20, 2026?")[0]["assertions"]
    assert [row["value"] for row in past] == ["River"]
    assert past[0]["valid_to"] == "2026-03-12T00:00:00+00:00"
    assert [row["value"] for row in current] == ["Lake"]
    assert current[0]["observed_at"] == "2026-03-02T09:00:00+00:00"
    assert current[0]["recorded_at"] != current[0]["observed_at"]


@pytest.mark.asyncio
async def test_newer_report_does_not_win_and_conflict_is_atomic(source_app, tmp_path, monkeypatch):
    a, b = "Nora reports the workshop starts at nine.", "Owen reports the workshop starts at ten."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({a: claim(a, "nine", subject="workshop", predicate="start_time"),
                   b: claim(b, "ten", subject="workshop", predicate="start_time", match_prior=True)})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "a", a)
        await projection.process_one(model)
        await ingest(client, "b", b, occurred="2026-03-19T09:00:00+00:00")
        await projection.process_one(model)
    packet = prepared(projection, "workshop")[0]
    assert packet["status"] == "unresolved_conflict"
    assert {row["value"] for row in packet["assertions"]} == {"nine", "ten"}
    assert {row["source"] for row in packet["assertions"]} == {"turn:a", "turn:b"}
    hits = projection.ledger.search_sources("workshop", contact_id="contact-a", session_id="s")
    _, rows = projection.prepare_context([], hits, contact_id="contact-a", session_id="s",
        time_query=interpret_time_query("workshop", now=datetime.now(timezone.utc)))
    from colony_sidecar.intelligence.graph.recall import pack_memory_context
    assert pack_memory_context(rows, max_chars=500) == ([], "")


@pytest.mark.asyncio
async def test_today_uses_source_event_and_timezone_not_ingestion(source_app, tmp_path):
    text = "Today I spotted a parcel by the orchard gate."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({text: claim(text, "parcel", predicate="gate_observation", event_at_text="Today")})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "old", text, occurred="2026-03-09T15:00:00+00:00", tz="America/New_York")
        await projection.process_one(model)
        await ingest(client, "today", text, occurred="2026-03-11T02:00:00+00:00", tz="America/New_York")
        await projection.process_one(model)
    packet = prepared(projection, "Was a parcel spotted today?", now="2026-03-11T03:00:00+00:00", tz="America/New_York")[0]
    assert [row["source"] for row in packet["assertions"]] == ["turn:today"]
    assert packet["assertions"][0]["event_at"] == "2026-03-10T04:00:00+00:00"


@pytest.mark.asyncio
async def test_scopes_and_erasure_do_not_revive_old_assertions(source_app, tmp_path):
    old, new = "My office is in River.", "Correction: My office is in Lake."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({old: claim(old, "River"), new: claim(new, "Lake", operation="correct", match_prior=True)})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "old", old)
        await projection.process_one(model)
        await ingest(client, "new", new)
        await projection.process_one(model)
        await ingest(client, "other", old, contact="contact-b")
        await projection.process_one(model)
    assert prepared(projection, contact="contact-b")[0]["assertions"][0]["value"] == "River"
    projection.ledger.erase_sources(contact_id="contact-a", turn_ids=["new"])
    assert prepared(projection) == []
    with sqlite3.connect(projection.ledger.db_path) as conn:
        assert not any("Lake" in row[0] for row in conn.execute('SELECT data_json FROM source_claims'))


@pytest.mark.asyncio
async def test_erasure_during_extraction_blocks_late_claim_write(source_app, tmp_path):
    text = "My office is in River."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({text: claim(text, "River")})
    complete = model.complete
    async def erase_then_return(*args, **kwargs):
        response = await complete(*args, **kwargs)
        projection.ledger.erase_sources(contact_id="contact-a", turn_ids=["source"])
        return response
    model.complete = erase_then_return
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "source", text)
    await projection.process_one(model)
    assert prepared(projection) == []


def test_ungrounded_value_or_unknown_relative_date_is_not_a_claim():
    text = "Today my office is in River."
    assert validated_claims(json.dumps([claim(text, "Lake")]), message=text, prior=[], observed_at=None) == []
    assert validated_claims(json.dumps([claim(text, "River", valid_from_text="Today")]), message=text, prior=[], observed_at=None) == []


def test_outbox_captures_once_and_never_dates_checkpoints(tmp_path, monkeypatch):
    module = _load_client("source_time_outbox")
    outbox = module.TurnOutbox(tmp_path / "outbox.sqlite3")
    monkeypatch.setattr(module.time, "time", lambda: 1772355600.0)
    first = outbox.enqueue("turn-1", _payload(), capture_ordinary=True)
    stamp = outbox.snapshot()[0]["payload"]["occurred_at"]
    monkeypatch.setattr(module.time, "time", lambda: 1774958400.0)
    assert outbox.enqueue("turn-1", _payload(), capture_ordinary=True)["envelope_sha256"] == first["envelope_sha256"]
    assert outbox.snapshot()[0]["payload"]["occurred_at"] == stamp
    checkpoint = {"session_id": "s", "contact_id": "c", "checkpoint_messages": [{"role": "user", "content": "old"}]}
    outbox.enqueue("checkpoint", checkpoint, capture_ordinary=True)
    assert "occurred_at" not in next(row for row in outbox.snapshot() if row["turn_id"] == "checkpoint")["payload"]


@pytest.mark.asyncio
async def test_correction_masks_overlapping_chunks_without_fabricating_quotes(source_app, tmp_path):
    evidence = "My office is in River."
    # The assertion starts just before the second chunk and ends inside it.
    text = "neutral padding " * 119 + evidence + " Unrelated fact: the drawer label is violet."
    new = "Correction: My office is in Lake."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({text: claim(evidence, "River"), new: claim(new, "Lake", operation="correct", match_prior=True)})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "old", text)
        await projection.process_one(model)
        await ingest(client, "new", new)
        await projection.process_one(model)
    hits = projection.ledger.search_sources("office drawer", contact_id="contact-a", session_id="s", limit=10)
    _, rows = projection.prepare_context([], hits, contact_id="contact-a", session_id="s",
        time_query=interpret_time_query("office drawer", now=datetime.now(timezone.utc)))
    quotes = [row["content"] for row in rows if not row.get("atomic_evidence")]
    assert quotes and any("violet" in quote for quote in quotes)
    assert all(quote in text for quote in quotes)
    assert all("River" not in quote for quote in quotes)
    assert any(row.get("excerpt_truncated") for row in rows)


@pytest.mark.asyncio
async def test_intraday_change_is_history_not_conflict(source_app, tmp_path):
    old = "From 2026-03-12T09:00:00Z, my office is in River."
    new = "Starting 2026-03-12T12:00:00Z, my office is now in Lake."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({old: claim(old, "River", valid_from_text="2026-03-12T09:00:00Z"),
                   new: claim(new, "Lake", operation="change", match_prior=True, valid_from_text="2026-03-12T12:00:00Z")})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "old", old)
        await projection.process_one(model)
        await ingest(client, "new", new)
        await projection.process_one(model)
    packet = prepared(projection, "Where was my office as of 2026-03-12?")[0]
    assert packet["status"] == "temporal_history"
    assert {a["value"] for a in packet["assertions"]} == {"River", "Lake"}


@pytest.mark.asyncio
async def test_repeated_identical_values_do_not_hide_conflicting_value(source_app, tmp_path):
    a, b = "The workshop starts at nine.", "The workshop starts at ten."
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "turn-idempotency.db"))
    model = Model({a: claim(a, "nine", subject="workshop", predicate="start_time"),
                   b: claim(b, "ten", subject="workshop", predicate="start_time")})
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        await ingest(client, "different", b)
        await projection.process_one(model)
        for index in range(12):
            await ingest(client, f"repeated-{index}", a)
            await projection.process_one(model)
    packet = prepared(projection, "workshop")[0]
    assert packet["status"] == "unresolved_conflict"
    assert {a["value"] for a in packet["assertions"]} == {"nine", "ten"}
    assert len(packet["assertions"]) == 2


def test_expired_worker_cannot_commit_or_complete_reclaimed_job(tmp_path):
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / "ledger.db"))
    message = {"role": "user", "content": "My office is in River."}
    projection.ledger.record_source("turn", contact_id="c", session_id="s", messages=[message])
    first = projection.claim_job()
    with sqlite3.connect(projection.ledger.db_path) as conn:
        conn.execute("UPDATE source_claim_jobs SET lease_until=0")
    second = projection.claim_job()
    claims = validated_claims(json.dumps([claim(message["content"], "River")]),
                              message=message["content"], prior=[], observed_at=None)
    assert projection.commit(first, message, claims, model="first", lease_token=first["lease_token"]) == 0
    projection.finish_job(first, model="first")
    assert projection.status("c")[0]["status"] == "running"
    assert projection.commit(second, message, claims, model="second", lease_token=second["lease_token"]) == 1
    projection.finish_job(second, model="second")
    assert projection.status("c")[0]["model"] == "second"


@pytest.mark.asyncio
async def test_local_extraction_disables_router_escalation(monkeypatch):
    from colony_sidecar.beliefs.source_claims import local_tier
    from colony_sidecar.router.router import LLMRouter
    from colony_sidecar.router.tiers import ModelTier
    router = LLMRouter(self_learner=SimpleNamespace())
    router._litellm_call = AsyncMock(side_effect=TimeoutError())
    router._fallback = SimpleNamespace(should_escalate=lambda *args: True,
                                       next_tier=lambda tier: ModelTier.MEDIUM)
    with pytest.raises(RuntimeError):
        await router.complete([{"role": "user", "content": "neutral"}], force_tier=ModelTier.SMALL,
                              context={"allow_fallback": False})
    assert router._litellm_call.await_count == 1
    monkeypatch.setenv("OPENAI_API_BASE", "http://127.0.0.1:8080/v1")
    assert local_tier(router) is None  # Default Anthropic must not inherit this endpoint.
    monkeypatch.setattr(router, "tier_config", lambda tier: SimpleNamespace(base_url=None, model_id="openai/local"))
    assert local_tier(router) is ModelTier.SMALL


def test_unicode_values_stay_distinct():
    from colony_sidecar.beliefs.source_claims import norm_value
    assert norm_value("東京") != norm_value("京都")
    assert norm_value("CAFÉ") == norm_value("Cafe\u0301")


def test_unsupported_time_range_is_not_silently_current():
    from colony_sidecar.beliefs.source_time import filter_unstructured
    for text in ("office last month", "office before 2026-03-12", "office between March 1, 2026 and March 5, 2026"):
        query = interpret_time_query(text, now=datetime.now(timezone.utc))
        assert query.mode == "unresolved_time"
        assert not query.accepts_claim({"valid_from": "2026-03-01T00:00:00+00:00"})
        assert filter_unstructured([{"content": "original quote"}], query)[0]["validity_status"] == "unknown"


def test_validity_clock_is_canonical_utc_and_event_date_is_not_validity():
    previous = {"id": "old", "subject_key": "speaker", "predicate": "office location"}
    text = "My office is now in Lake."
    row = validated_claims(json.dumps([claim(text, "Lake", operation="change", prior_claim_id="old")]),
        message=text, prior=[previous], observed_at="2026-03-12T08:30:00-04:00")[0]
    assert row["valid_from"] == "2026-03-12T12:30:00+00:00"
    assert row["validity_basis"] == "assertion_time"
    event = "Today I spotted a parcel."
    row = validated_claims(json.dumps([claim(event, "parcel", event_at_text="Today")]),
        message=event, prior=[], observed_at="2026-03-12T08:30:00-04:00")[0]
    assert row["event_at"] and row["valid_from"] is None and row["validity_basis"] == "unspecified"
