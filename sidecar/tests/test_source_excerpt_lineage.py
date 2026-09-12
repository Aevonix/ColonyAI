"""Lexical excerpts must inherit evidence from their own canonical message."""
import json

from httpx import ASGITransport, AsyncClient
import pytest

from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.idempotency import source_message_hash
from pacomind.turns.source_vectors import merge_source_hits
from pacomind.turns.source_read import read
from pacomind.memory.recall import source_candidates
from test_source_audio import message as audio_message
from test_turn_source_evidence import source_app, recalled


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_first", [False, True])
async def test_mixed_checkpoint_recall_keeps_typed_and_audio_lineage_separate(
    source_app, tmp_path, monkeypatch, audio_first,
):
    monkeypatch.setenv("PACOMIND_RECALL_RERANK", "off")
    typed = {"role": "user", "content": "The orrery is in the wooden cabinet."}
    audio = audio_message()
    messages = [audio, typed] if audio_first else [typed, audio]
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    ledger.record_source("mixed", contact_id="contact-a", session_id="checkpoint",
        scope="session", messages=messages, derive_claims=False)
    with ledger._connect() as conn:
        before = conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='mixed'").fetchone()[0]

    reopened = TurnIdempotencyLedger(ledger.db_path)
    typed_hits = reopened.search_sources("orrery", contact_id="contact-a", session_id="checkpoint")
    assert len(typed_hits) == 1
    assert "source_modality" not in typed_hits[0]
    assert typed_hits[0]["source_message_hash"] == source_message_hash("checkpoint", typed)
    audio_hits = reopened.search_sources("violet", contact_id="contact-a", session_id="checkpoint")
    assert len(audio_hits) == 1
    assert audio_hits[0]["source_modality"] == "audio_transcript"
    assert audio_hits[0]["source_message_hash"] == source_message_hash("checkpoint", audio)
    reference = reopened.source_references(["mixed"], contact_id="contact-a", session_id="checkpoint")[0]
    opened = read(reopened, contact_id="contact-a", session_id="checkpoint", **reference)
    assert opened["complete"] and opened["source_refs"] == [reference]
    full = json.loads(opened["content"])
    assert typed in full["messages"]
    assert any(source_message_hash("checkpoint", item) == audio_hits[0]["source_message_hash"]
               for item in full["messages"])
    assert full["reported_at"] is None

    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        typed_packet = await recalled(client, session="checkpoint", query="orrery")
        audio_packet = await recalled(client, session="checkpoint", query="violet")
        assert '"state": "quotation"' in typed_packet
        assert '"source_modality": "audio_transcript"' not in typed_packet
        assert typed_hits[0]["source_message_hash"] in typed_packet
        assert '"state": "derived_unverified"' in audio_packet
        assert audio_hits[0]["source_message_hash"] in audio_packet
        assert not await recalled(client, session="another-session", query="orrery")
        assert not await recalled(client, contact="another-person", session="checkpoint", query="orrery")
    with reopened._connect() as conn:
        assert conn.execute("SELECT messages_json FROM turn_sources WHERE turn_id='mixed'").fetchone()[0] == before
    reopened.erase_sources(contact_id="contact-a", turn_ids=["mixed"])
    assert not reopened.search_sources("orrery violet", contact_id="contact-a", session_id="checkpoint")
    with pytest.raises(ValueError, match="source_unavailable_or_changed"):
        read(reopened, contact_id="contact-a", session_id="checkpoint", **reference)


def test_identical_rendered_chunks_keep_distinct_message_owners_and_merge_exact_hits(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "sources.db")
    text = "The astrolabe is stored beside the brass compass."
    messages = [{"role": "user", "content": text},
                {"role": "user", "content": [{"type": "text", "text": text}]}]
    ledger.record_source("same-rendering", contact_id="person", session_id="work",
                         messages=messages, derive_claims=False)
    hits = ledger.search_sources("astrolabe", contact_id="person", session_id="later")
    expected = {source_message_hash("work", message) for message in messages}
    assert {hit["source_message_hash"] for hit in hits} == expected
    assert len(hits) == 2
    merged = merge_source_hits(hits, [dict(hits[0], retrieval_method="semantic")])
    assert len(merged) == 2
    assert {hit["source_message_hash"] for hit in merged} == expected
    assert len({row["id"] for row in source_candidates(merged)}) == 2


def test_lexical_hydration_marks_truncation_and_ignores_unowned_index_rows(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "sources.db")
    message = {"role": "user", "content": "Inspect the sextant carefully. " * 150}
    ledger.record_source("long", contact_id="person", session_id="work",
                         messages=[message], derive_claims=False)
    with ledger._connect() as conn:
        conn.execute("INSERT INTO turn_source_search(turn_id,role,content) VALUES (?,?,?)",
                     ("long", "user", "Unowned sextant projection."))
        conn.commit()
    hits = ledger.search_sources("sextant", contact_id="person", session_id="later", limit=10)
    assert hits
    assert all(hit["content"] in message["content"] for hit in hits)
    assert all(hit["excerpt_truncated"] for hit in hits)
    assert all(hit["source_message_hash"] == source_message_hash("work", message) for hit in hits)


def test_containing_message_does_not_own_another_messages_indexed_excerpt(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "sources.db")
    short = {"role": "user", "content": "The phrase is violet"}
    longer = {"role": "user", "content": "Preface: The phrase is violet"}
    other = {"role": "user", "content": "A violet lantern stands beside the courtyard entrance."}
    ledger.record_source("nested", contact_id="person", session_id="work",
                         messages=[short, longer], derive_claims=False)
    ledger.record_source("other", contact_id="person", session_id="work",
                         messages=[other], derive_claims=False)
    hits = ledger.search_sources("violet", contact_id="person", session_id="later", limit=3)
    assert {(hit["content"], hit["source_message_hash"]) for hit in hits} == {
        (message["content"], source_message_hash("work", message))
        for message in (short, longer, other)}
    assert len(hits) == 3


def test_stale_projection_rows_do_not_exhaust_the_canonical_result_limit(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "sources.db")
    message = {"role": "user", "content": "The violet marker is here. " + "Ordinary filler. " * 80}
    ledger.record_source("valid", contact_id="person", session_id="work",
                         messages=[message], derive_claims=False)
    with ledger._connect() as conn, conn:
        conn.executemany("INSERT INTO turn_source_search(turn_id,role,content) VALUES (?,?,?)",
                         [("valid", "user", "violet " * 80 + f"stale {index}") for index in range(10)])
    hits = ledger.search_sources("violet", contact_id="person", session_id="later", limit=5)
    assert len(hits) == 1
    assert hits[0]["content"] == message["content"]
    assert hits[0]["source_message_hash"] == source_message_hash("work", message)


def test_duplicate_projection_rows_do_not_hide_other_canonical_messages(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "sources.db")
    short = {"role": "user", "content": "Violet marker."}
    other = {"role": "user", "content": "The violet marker beside the garden is ready."}
    for turn, message in (("first", short), ("second", other)):
        ledger.record_source(turn, contact_id="person", session_id="work",
                             messages=[message], derive_claims=False)
    with ledger._connect() as conn, conn:
        conn.executemany("INSERT INTO turn_source_search(turn_id,role,content) VALUES (?,?,?)",
                         [("first", "user", short["content"])] * 12)
    hits = ledger.search_sources("violet", contact_id="person", session_id="later", limit=2)
    assert {hit["turn_id"] for hit in hits} == {"first", "second"}
    assert len(hits) == 2


def test_ownership_reconstruction_only_reads_scoped_matches_and_hashes_each_message_once(tmp_path, monkeypatch):
    from pacomind.turns import idempotency
    ledger = TurnIdempotencyLedger(tmp_path / "sources.db")
    retained = {"role": "user", "content": "Violet marker. " * 400}
    ledger.record_source("kept", contact_id="person", session_id="work",
                         messages=[retained], derive_claims=False)
    ledger.record_source("foreign", contact_id="other", session_id="work",
                         messages=[{"role": "user", "content": "Violet foreign marker."}], derive_claims=False)
    ledger.record_source("private", contact_id="person", session_id="private", scope="session",
                         messages=[{"role": "user", "content": "Violet private marker."}], derive_claims=False)
    ledger.record_source("unmatched", contact_id="person", session_id="work",
                         messages=[{"role": "user", "content": "A brass compass."}], derive_claims=False)
    observed = []
    original_hash = idempotency.source_message_hash

    def record_hash(session, message):
        observed.append((session, message))
        return original_hash(session, message)

    monkeypatch.setattr(idempotency, "source_message_hash", record_hash)
    hits = ledger.search_sources("violet", contact_id="person", session_id="later", limit=5)
    assert hits and {hit["turn_id"] for hit in hits} == {"kept"}
    assert observed == [("work", retained)]


def test_shared_exact_prefix_chunks_keep_both_canonical_owners(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / "sources.db")
    common = "Violet prefix. " + "Ordinary padding. " * 150
    messages = [{"role": "user", "content": common + ending}
                for ending in ("First ending.", "Second ending.")]
    ledger.record_source("shared-prefix", contact_id="person", session_id="work",
                         messages=messages, derive_claims=False)
    hits = ledger.search_sources("violet", contact_id="person", session_id="later", limit=2)
    assert len(hits) == 2
    assert {hit["source_message_hash"] for hit in hits} == {
        source_message_hash("work", message) for message in messages}
    assert all(hit["content"] == common[:2000] and hit["excerpt_truncated"] for hit in hits)
