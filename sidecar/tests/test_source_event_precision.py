"""Optional event uncertainty cannot erase useful state or certify a condition."""
import json

import pytest

from pacomind.beliefs.source_claims import validated_claims
from pacomind.beliefs.source_time import parse_source_date, source_event_time
from pacomind.memory.recall import pack_memory_context, source_candidates
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


@pytest.mark.parametrize("expression,zone,expected", [
    ("18 September 2026", "UTC", "2026-09-18T00:00:00+00:00"),
    ("September 18, 2026", "UTC", "2026-09-18T00:00:00+00:00"),
    ("16:30 UTC on 18 September 2026", "America/New_York", "2026-09-18T16:30:00+00:00"),
    ("17:15:30 utc on September 18, 2026", "Asia/Tokyo", "2026-09-18T17:15:30+00:00"),
    ("00:30 on 18 September 2026", "Asia/Tokyo", "2026-09-17T15:30:00+00:00"),
    ("01:30 UTC on 1 November 2026", "America/New_York", "2026-11-01T01:30:00+00:00"),
])
def test_literal_english_dates_and_clocks_keep_the_source_timezone(expression, zone, expected):
    # These are literal source-format controls, not captured extractor output.
    assert parse_source_date(expression, observed_at=None, timezone_name=zone) == expected
    result = source_event_time(expression, observed_at=None, timezone_name=zone)
    if " on " in expression:
        assert result == {"expression": expression, "status": "resolved",
                          "precision": "instant", "at": expected}
    else:
        assert result["precision"] == "calendar_day" and result["start"] == expected


@pytest.mark.parametrize("expression", [
    "31 September 2026", "29 February 2026", "18 September", "09/10/2026",
    "24:00 UTC on 18 September 2026", "17:60 UTC on 18 September 2026",
    "17:15 EST on 18 September 2026", "1:30 PM on 18 September 2026",
    "01:30 on 1 November 2026", "02:30 on 8 March 2026",
])
def test_unresolved_or_ambiguous_source_clocks_stay_unresolved(expression):
    assert parse_source_date(expression, observed_at=STAMP, timezone_name="America/New_York") is None
    assert source_event_time(expression, observed_at=STAMP, timezone_name="America/New_York")["status"] == "unresolved"


def test_day_first_source_date_keeps_calendar_precision_across_dst():
    assert source_event_time("8 March 2026", observed_at=None, timezone_name="America/New_York") == {
        "expression": "8 March 2026", "status": "resolved", "precision": "calendar_day",
        "start": "2026-03-08T05:00:00+00:00", "end_exclusive": "2026-03-09T04:00:00+00:00"}
