"""One-command global pause (Amendment 1.5): the kill switch, end to end."""

from __future__ import annotations

from colony_sidecar.directives import Action, DirectiveManager, DirectiveStore
from colony_sidecar.directives.extractor import extract_directives
from colony_sidecar.directives.models import GLOBAL_PAUSE_TERM


def _mgr():
    return DirectiveManager(DirectiveStore())


def _act(text="research market trends"):
    return Action(kind="research", text=text, high_risk=True)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def test_pause_phrasings_captured_as_global():
    for msg in ("stop acting", "pause autonomy", "stand down",
                "please pause your autonomy for now", "halt autonomous work",
                "stop taking actions", "stop doing things for a while"):
        found = extract_directives(msg)
        assert len(found) == 1, msg
        assert GLOBAL_PAUSE_TERM in found[0].match_terms, msg


def test_normal_prohibition_not_global():
    found = extract_directives("stop researching competitors")
    assert found and GLOBAL_PAUSE_TERM not in found[0].match_terms


# ---------------------------------------------------------------------------
# Enforcement is immediate and total (for acts)
# ---------------------------------------------------------------------------

def test_pause_blocks_every_act_immediately():
    mgr = _mgr()
    assert mgr.check(_act()).allowed          # before: open
    result = mgr.capture_from_message("stop acting")
    assert result.captured and "GLOBAL PAUSE" in (result.ack or "")
    # instantly binding, arbitrary unrelated subjects included
    for text in ("research market trends", "send the weekly summary",
                 "update the project plan", "anything at all"):
        v = mgr.check(Action(kind="deliver", text=text, high_risk=True))
        assert not v.allowed
        assert v.reason == "global_pause_active"
    # and it shows up in recent blocks for introspection
    assert mgr.guard.recent_blocks()


def test_pause_leaves_reads_open():
    mgr = _mgr()
    mgr.capture_from_message("stop acting")
    v = mgr.check(Action(kind="repo_read", text="read the config file"))
    assert v.allowed                          # ACT-level: perception stays


# ---------------------------------------------------------------------------
# Lift via staged confirmation (asymmetric friction preserved)
# ---------------------------------------------------------------------------

def test_resume_requires_confirmation_then_lifts():
    mgr = _mgr()
    mgr.capture_from_message("stop acting")
    assert not mgr.check(_act()).allowed

    result = mgr.capture_from_message("resume autonomy")
    assert result.needs_confirmation          # staged, not lifted yet
    assert not mgr.check(_act()).allowed      # still paused

    result = mgr.capture_from_message("yes")
    assert result.revoked                     # confirmed lift
    assert mgr.check(_act()).allowed          # autonomy restored


def test_unrelated_message_does_not_lift():
    mgr = _mgr()
    mgr.capture_from_message("stop acting")
    mgr.capture_from_message("resume autonomy")
    mgr.capture_from_message("what's the weather like")   # not an affirmation
    assert not mgr.check(_act()).allowed      # pause survives
