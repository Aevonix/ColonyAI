"""Owner preference learning — the explicit-directive lane.

PreferenceLearner captures the owner's *stated* communication directives ("be
concise", "use bullets", "no emoji") at high confidence and persists them, and
the host surfaces them back — but only for the owner. This is distinct from the
EngagementStore, which passively infers a contact's style. These tests cover the
store behaviour and the host wiring (learn endpoint, get endpoint, and the
owner-only context section).
"""

import pytest

from colony_sidecar.intelligence.components.preference_learner import PreferenceLearner


# ---------------------------------------------------------------------------
# Directive detection + learning (store level)
# ---------------------------------------------------------------------------

def test_detect_directive_matches_real_directives():
    pl = PreferenceLearner()
    assert pl.detect_directive("be more concise") == ("communication_style", "length", "short")
    assert pl.detect_directive("use bullet points please") == ("communication_style", "format", "bullet_points")
    assert pl.detect_directive("stop using emoji") == ("communication_style", "emoji", "off")
    assert pl.detect_directive("keep your replies formal") == ("communication_style", "style", "formal")


def test_detect_directive_ignores_ordinary_messages():
    pl = PreferenceLearner()
    # Style word but no directive cue.
    assert pl.detect_directive("I took a short walk this morning") is None
    # No style word at all.
    assert pl.detect_directive("what's the weather today") is None
    # Empty.
    assert pl.detect_directive("") is None


@pytest.mark.asyncio
async def test_learn_directive_captures_multiple_in_one_message():
    pl = PreferenceLearner()
    primary = await pl.learn_directive("from now on be concise and use bullet points and skip the emoji")
    # A directive was recognised (the exact "primary" hit is informational).
    assert primary is not None and primary[0] == "communication_style"
    # All three directives in the one message are captured.
    brief = pl.build_brief().lower()
    assert "short" in brief
    assert "bullet" in brief
    assert "emoji" in brief


@pytest.mark.asyncio
async def test_learn_directive_returns_none_for_non_directive():
    pl = PreferenceLearner()
    assert await pl.learn_directive("how are you doing today") is None
    assert pl.build_brief() == ""


@pytest.mark.asyncio
async def test_directives_persist_across_restart(tmp_path):
    db = str(tmp_path / "prefs.db")
    pl = PreferenceLearner(db_path=db)
    await pl.learn_directive("keep replies short")
    await pl.learn_directive("no emoji please")

    reopened = PreferenceLearner(db_path=db)
    brief = reopened.build_brief().lower()
    assert "short" in brief
    assert "emoji" in brief


@pytest.mark.asyncio
async def test_later_directive_overrides_earlier(tmp_path):
    db = str(tmp_path / "prefs.db")
    pl = PreferenceLearner(db_path=db)
    await pl.learn_directive("be brief")
    await pl.learn_directive("actually be detailed and thorough")
    brief = pl.build_brief().lower()
    assert "thorough" in brief or "detailed" in brief
    # Only one length line survives (last write wins on the same key).
    assert brief.count("replies") <= 2


def test_legacy_constructor_still_accepts_graph_positional():
    # The original signature was PreferenceLearner(graph_client); keep it working.
    pl = PreferenceLearner("fake-graph-client")
    assert pl.graph == "fake-graph-client"


# ---------------------------------------------------------------------------
# Host wiring: endpoints + owner-only context section
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_host_endpoints_and_owner_only_surfacing(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_OWNER_CONTACT_ID", "owner:test")
    import colony_sidecar.api.routers.host as host
    from colony_sidecar.api.schemas.host import (
        ContextAssembleRequest, HostIdentity, HostTurnContext, HostMessage,
    )

    pl = PreferenceLearner(db_path=str(tmp_path / "prefs.db"))
    host.set_preference_learner(pl)
    try:
        # learn endpoint — a real directive is captured
        out = await host.learn_owner_preference(
            {"text": "from now on be concise and use bullet points"}
        )
        assert out["learned"] is not None
        assert "short" in out["brief"].lower()

        # learn endpoint — a non-directive is ignored
        out2 = await host.learn_owner_preference({"text": "what's up"})
        assert out2["learned"] is None

        # get endpoint
        got = await host.get_owner_preferences()
        assert got["available"] is True
        assert len(got["preferences"]) >= 2

        # context_assemble: section present for the owner...
        def _req(cid):
            return ContextAssembleRequest(
                identity=HostIdentity(host_id="hermes"),
                context=HostTurnContext(contact_id=cid, session_id="s1"),
                incoming_message=HostMessage(role="user", content="hi"),
            )

        owner_sections = (await host.context_assemble(_req("owner:test"))).sections
        other_sections = (await host.context_assemble(_req("contact:other"))).sections

        def _has_pref(sections):
            return any(getattr(s, "id", "") == "colony-owner-preferences" for s in sections)

        assert _has_pref(owner_sections) is True
        # ...and absent for everyone else.
        assert _has_pref(other_sections) is False
    finally:
        host.set_preference_learner(None)
