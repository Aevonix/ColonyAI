"""Optional event uncertainty cannot erase useful state or certify a condition."""
import json

import pytest

from apsimo.beliefs.source_claims import validated_claims
from apsimo.beliefs.source_time import source_event_time
from apsimo.intelligence.graph.recall import pack_memory_context, source_candidates
from test_source_claim_projection import claim


STAMP = "2026-03-10T09:14:30+00:00"


@pytest.mark.parametrize("phrase,status", [
    ("before sending this message", "bounded"),
    ("earlier this week", "unresolved"),
])
def test_supported_location_survives_optional_imprecise_event(phrase, status):
    text = f"My office is in Lake now. I moved {phrase}."
    proposal = claim(text, "Lake", valid_from_text="now", event_at_text=phrase)
    rows = validated_claims(json.dumps([proposal]), message=text, prior=[], observed_at=STAMP)
    assert len(rows) == 1
    row = rows[0]
    assert row["value"] == "Lake" and row["evidence"] == text
    assert row["valid_from"] == STAMP and row["event_at"] is None
    assert row["event_time"]["expression"] == phrase
    assert row["event_time"]["status"] == status
    if status == "bounded":
        assert row["event_time"]["before"] == STAMP
    else:
        assert set(row["event_time"]) == {"expression", "status"}


@pytest.mark.parametrize("field", ["valid_from_text", "valid_to_text"])
def test_unresolved_required_validity_is_not_removed(field):
    text = "My office will be in Lake after the inspection passes."
    proposal = claim(text, "Lake", **{field: "after the inspection passes"})
    assert validated_claims(json.dumps([proposal]), message=text, prior=[], observed_at=STAMP) == []


def test_invented_optional_date_is_still_a_contract_failure():
    text = "My office is in Lake."
    proposal = claim(text, "Lake", event_at_text="yesterday")
    assert validated_claims(json.dumps([proposal]), message=text, prior=[], observed_at=STAMP) == []


def test_event_day_preserves_precision_and_local_day_length():
    result = source_event_time("today", observed_at="2026-03-08T12:00:00+00:00",
                               timezone_name="America/New_York")
    assert result == {"expression": "today", "status": "resolved", "precision": "calendar_day",
                      "start": "2026-03-08T05:00:00+00:00", "end_exclusive": "2026-03-09T04:00:00+00:00"}
    assert source_event_time("before this message", observed_at=None)["status"] == "unresolved"
    assert source_event_time(None, observed_at=STAMP) == {"status": "unknown"}


def test_plain_source_packet_labels_report_clock_without_creating_event_time():
    rows = source_candidates([{"turn_id": "report", "role": "user", "content": "The tool is in the tin.",
                               "occurred_at": STAMP, "ingested_at": "2026-04-01T12:00:00Z"}])
    _, packet = pack_memory_context(rows)
    assert '"reported_at": "' + STAMP + '"' in packet
    assert '"event_time": "unprojected"' in packet
    assert "Report time is not event time." in packet
    assert '"occurred_at"' not in packet
