"""Tests for the boundary/directive memory subsystem."""

from __future__ import annotations

from colony_sidecar.directives import (
    DirectiveStore, DirectiveManager, DirectiveGuard, Action, Directive, Polarity,
)
from colony_sidecar.directives.extractor import extract_directives, is_revocation


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extract_prohibition_dont_touch_repo():
    d = extract_directives("Don't touch the colony-web repo, it is a side project")
    assert len(d) == 1
    assert d[0].polarity == Polarity.PROHIBIT
    assert "colony-web" in d[0].match_terms


def test_extract_prohibition_variants():
    for msg in ["stop researching competitors", "avoid the acme-corp account",
                "leave the SSL cert stuff alone", "never message my ex",
                "ignore the legacy billing system"]:
        d = extract_directives(msg)
        assert d and d[0].polarity == Polarity.PROHIBIT, msg


def test_extract_requirement():
    d = extract_directives("from now on always check with me before deploying")
    assert d and d[0].polarity == Polarity.REQUIRE


def test_extract_skips_pure_style():
    # communication style is the PreferenceLearner's job, not a boundary
    assert extract_directives("please be more concise") == []
    assert extract_directives("stop using emoji") == []


def test_extract_revocation():
    d = extract_directives("actually you can go ahead and work on colony-web again")
    assert d and is_revocation(d[0])


def test_extract_nothing_on_ordinary_message():
    assert extract_directives("how's the deploy going?") == []
    assert extract_directives("thanks, that looks great") == []


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_store_add_list_active_revoke():
    s = DirectiveStore(db_path=None)
    d = Directive(subject="repo colony-web", polarity=Polarity.PROHIBIT)
    s.add(d)
    assert s.count_active() == 1
    assert s.get(d.id).subject == "repo colony-web"
    assert s.revoke(d.id) is True
    assert s.count_active() == 0
    # still listable by status
    assert len(s.list(status="revoked")) == 1


def test_store_expiry():
    import time
    s = DirectiveStore(db_path=None)
    s.add(Directive(subject="x-thing", polarity=Polarity.PROHIBIT, expires_at=time.time() - 1))
    assert s.count_active() == 0  # expired -> not active


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

def _guard_with(*directives) -> DirectiveGuard:
    s = DirectiveStore(db_path=None)
    for d in directives:
        s.add(d)
    return DirectiveGuard(s)


def test_guard_blocks_specific_subject():
    g = _guard_with(Directive(subject="colony-web repo", polarity=Polarity.PROHIBIT,
                              raw_text="don't touch colony-web"))
    v = g.check(Action(kind="directed_action", text="clone and refactor the colony-web repo",
                       high_risk=True))
    assert v.allowed is False
    assert "colony-web" in v.reason


def test_guard_allows_unrelated_action():
    g = _guard_with(Directive(subject="colony-web repo", polarity=Polarity.PROHIBIT))
    v = g.check(Action(kind="directed_action", text="update the billing dashboard"))
    assert v.allowed is True


def test_guard_no_false_block_on_generic_word():
    # a generic single word must not block everything containing it
    g = _guard_with(Directive(subject="the report", polarity=Polarity.PROHIBIT,
                              match_terms=["report"]))
    # 'report' alone is not distinctive -> requires whole-subject match; a lone
    # generic hit should not block an unrelated 'research' action
    v = g.check(Action(kind="research", text="research competitor pricing"))
    assert v.allowed is True


def test_guard_stem_match_research():
    g = _guard_with(Directive(subject="researching competitors", polarity=Polarity.PROHIBIT))
    v = g.check(Action(kind="research", text="start research on competitors and pricing"))
    assert v.allowed is False


def test_guard_entity_id_match():
    g = _guard_with(Directive(subject="that person", polarity=Polarity.PROHIBIT,
                              entity_ids=["cid-abc"]))
    v = g.check(Action(kind="deliver", text="follow up", entity_id="cid-abc", high_risk=True))
    assert v.allowed is False


def test_guard_action_kind_scope():
    d = Directive(subject="acme-corp", polarity=Polarity.PROHIBIT, action_kinds=["deliver"])
    g = _guard_with(d)
    # only 'deliver' actions are scoped; a research action on acme-corp is allowed
    assert g.check(Action(kind="research", text="acme-corp market")).allowed is True
    assert g.check(Action(kind="deliver", text="message acme-corp")).allowed is False


def test_guard_context_brief():
    g = _guard_with(
        Directive(subject="colony-web", polarity=Polarity.PROHIBIT, raw_text="don't touch colony-web"),
        Directive(subject="check before deploy", polarity=Polarity.REQUIRE, raw_text="always check before deploy"),
    )
    brief = g.context_brief()
    assert "MUST NOT" in brief and "colony-web" in brief
    assert "MUST" in brief and "deploy" in brief


# ---------------------------------------------------------------------------
# Manager end-to-end
# ---------------------------------------------------------------------------

def test_manager_capture_then_enforce():
    m = DirectiveManager(DirectiveStore(db_path=None))
    m.capture_from_message("Don't touch the colony-web repo")
    assert m.store.count_active() == 1
    # an autonomous action on colony-web is now refused
    v = m.check(Action(kind="directed_action", text="open a PR in colony-web", high_risk=True))
    assert v.allowed is False


def test_manager_revocation_lifts_boundary():
    m = DirectiveManager(DirectiveStore(db_path=None))
    m.capture_from_message("stop working on the acme-corp integration")
    assert m.store.count_active() == 1
    m.capture_from_message("actually you can work on acme-corp again")
    assert m.store.count_active() == 0  # boundary lifted
    assert m.check(Action(kind="directed_action", text="acme-corp integration")).allowed is True
