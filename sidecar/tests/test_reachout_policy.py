"""Tests for reach-out delivery policy: sanitise, staleness, quiet-hours."""

from __future__ import annotations

from colony_sidecar.delivery import reachout_policy as rp


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------

def test_sanitize_strips_rcsctx_block():
    s = 'Follow up: send RCS <<RCSCTX conversation_id="6" is_group=true>> now'
    assert rp.sanitize_text(s) == "Follow up: send RCS now"


def test_sanitize_strips_unterminated_rcsctx():
    # titles are truncated so the closing >> is often missing
    s = 'Follow up on: hi there <<RCSCTX conversation_id="6" is_gr'
    assert rp.sanitize_text(s) == "Follow up on: hi there"


def test_sanitize_strips_bracket_directives():
    s = 'Follow up [IMPORTANT: The user invoked the "colony-operations" skill] please'
    assert rp.sanitize_text(s) == "Follow up please"


def test_sanitize_strips_control_chars():
    assert rp.sanitize_text("a\x07b\x00c\td") == "a b c d"


def test_sanitize_payload_cleans_fields_and_title_fallback():
    p = {
        "title": "<<RCSCTX x>>",
        "description": "Real intent here",
        "rationale": "note [SYSTEM: internal]",
        "suggested_action": "review_and_decide",
    }
    out = rp.sanitize_payload(p)
    assert out["description"] == "Real intent here"
    assert out["rationale"] == "note"
    assert out["suggested_action"] == "review_and_decide"
    # title was pure markup -> falls back to description
    assert out["title"] == "Real intent here"


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def test_aged_out_from_structured_days_pending(monkeypatch):
    monkeypatch.delenv("COLONY_REACHOUT_MAX_AGE_DAYS", raising=False)
    p = {"type": "follow_up", "context": {"blocked_goal": {"days_pending": 16.5}},
         "generated_at": "2026-07-04T00:00:00+00:00"}
    assert rp.reachout_age_days(p) >= 16.5
    assert rp.is_aged_out(p) is True


def test_not_aged_out_when_fresh(monkeypatch):
    monkeypatch.delenv("COLONY_REACHOUT_MAX_AGE_DAYS", raising=False)
    from datetime import datetime, timezone
    p = {"type": "follow_up", "context": {"blocked_goal": {"days_pending": 2.0}},
         "generated_at": datetime.now(timezone.utc).isoformat()}
    assert rp.is_aged_out(p) is False


def test_max_age_env_override(monkeypatch):
    monkeypatch.setenv("COLONY_REACHOUT_MAX_AGE_DAYS", "30")
    p = {"type": "follow_up", "context": {"days_pending": 16.5},
         "generated_at": "2026-07-04T00:00:00+00:00"}
    # with a 30-day threshold a 16-day item is NOT aged out
    assert rp.is_aged_out(p) is False


def test_contact_recency_is_not_treated_as_staleness(monkeypatch):
    monkeypatch.delenv("COLONY_REACHOUT_MAX_AGE_DAYS", raising=False)
    from datetime import datetime, timezone
    # days_since_contact is a REASON to reach out, not a disqualifier
    p = {"type": "relationship", "context": {"days_since_contact": 40},
         "generated_at": datetime.now(timezone.utc).isoformat()}
    assert rp.is_aged_out(p) is False


# ---------------------------------------------------------------------------
# Quiet-hours urgency
# ---------------------------------------------------------------------------

def test_urgency_capped_below_bypass(monkeypatch):
    monkeypatch.delenv("COLONY_REACHOUT_URGENCY_CAP", raising=False)
    # priority-derived 1.0 must be capped below the 0.9 quiet-hours bypass
    assert rp.quiet_hours_urgency({}, 1.0) < 0.9


def test_explicit_urgent_bypasses_cap():
    assert rp.quiet_hours_urgency({"urgent": True}, 1.0) == 1.0
    assert rp.quiet_hours_urgency({"context": {"urgent": True}}, 1.0) == 1.0


def test_low_urgency_passes_through():
    assert rp.quiet_hours_urgency({}, 0.4) == 0.4
