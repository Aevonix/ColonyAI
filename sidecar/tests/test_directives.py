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


def test_guard_records_recent_blocks():
    g = _guard_with(Directive(subject="colony-web", polarity=Polarity.PROHIBIT,
                              raw_text="don't touch colony-web"))
    assert g.check(Action(kind="directed_action", text="refactor colony-web")).allowed is False
    blocks = g.recent_blocks()
    assert len(blocks) == 1
    assert "colony-web" in blocks[0]["subjects"]
    assert blocks[0]["directive_ids"] and blocks[0]["action_kind"] == "directed_action"
    # an allowed action is not recorded
    g.check(Action(kind="research", text="unrelated topic"))
    assert len(g.recent_blocks()) == 1


def test_guard_entity_scoped_match_by_alias():
    """A boundary on an entity blocks actions naming it by an ALIAS (2)."""
    # owner said "don't touch the guitar shop"; that shop's repo is 'gcs-repo'
    g = _guard_with(Directive(subject="the guitar shop", polarity=Polarity.PROHIBIT,
                              raw_text="don't touch the guitar shop"))
    g.set_entity_index({"we-shop-1": ["guitar shop", "gcs-repo"]})
    # keyword hit still works
    assert g.check(Action(kind="directed_action", text="update the guitar shop site")).allowed is False
    # entity-scoped: 'gcs-repo' shares no keyword with 'guitar shop' but is the
    # same entity, so it is blocked
    assert g.check(Action(kind="directed_action", text="open a PR in gcs-repo")).allowed is False
    # unrelated entity/action allowed
    assert g.check(Action(kind="directed_action", text="update the billing repo")).allowed is True


def test_guard_context_brief():
    g = _guard_with(
        Directive(subject="colony-web", polarity=Polarity.PROHIBIT, raw_text="don't touch colony-web"),
        Directive(subject="check before deploy", polarity=Polarity.REQUIRE, raw_text="always check before deploy"),
    )
    brief = g.context_brief()
    assert "MUST NOT" in brief and "colony-web" in brief
    assert "MUST" in brief and "deploy" in brief


# ---------------------------------------------------------------------------
# Tiered boundary semantics (ACT vs OBSERVE)
# ---------------------------------------------------------------------------

def test_act_boundary_allows_read_blocks_actions():
    """ACT: reads open; delegate/mutate/outbound blocked."""
    m = DirectiveManager(DirectiveStore(db_path=None))
    m.capture_from_message("leave the widget-api repo alone")
    d = m.store.active()[0]
    from colony_sidecar.directives.models import Level
    assert d.level == Level.ACT                              # default level
    # reads / perception stay OPEN
    assert m.check(Action(kind="repo_read", text="widget-api README")).allowed is True
    assert m.check(Action(kind="populate", text="widget-api entity")).allowed is True
    # actions are BLOCKED
    assert m.check(Action(kind="directed_action", text="refactor widget-api")).allowed is False
    assert m.check(Action(kind="deliver", text="message about widget-api")).allowed is False
    assert m.check(Action(kind="execute", text="run initiative on widget-api")).allowed is False


def test_observe_boundary_blocks_reads_too():
    """OBSERVE (perception-explicit): full blackout, reads blocked + recorded."""
    m = DirectiveManager(DirectiveStore(db_path=None))
    m.capture_from_message("don't look at the widget-api repo")
    d = m.store.active()[0]
    from colony_sidecar.directives.models import Level
    assert d.level == Level.OBSERVE
    v = m.check(Action(kind="repo_read", text="widget-api README"))
    assert v.allowed is False
    # the withheld read is recorded so introspection about the blindspot works
    blocks = m.guard.recent_blocks()
    assert blocks and blocks[0]["capability"] == "read"
    # actions blocked as well
    assert m.check(Action(kind="directed_action", text="refactor widget-api")).allowed is False


def test_echo_states_interpretation():
    m = DirectiveManager(DirectiveStore(db_path=None))
    cap = m.capture_from_message("leave the widget-api repo alone")
    assert "stop acting on" in cap.ack and "full blackout" in cap.ack  # ACT echo
    m2 = DirectiveManager(DirectiveStore(db_path=None))
    cap2 = m2.capture_from_message("don't look at the widget-api repo")
    assert "blackout" in cap2.ack and "not act on it or look at it" in cap2.ack


def test_critical_flag_fires_once_via_guarded_delivery():
    import asyncio
    m = DirectiveManager(DirectiveStore(db_path=None))
    m.capture_from_message("leave the billing-svc repo alone")
    delivered = []
    async def router(payload):
        delivered.append(payload); return True
    m.set_delivery_router(router)
    async def run():
        out1 = await m.flag_critical(
            "billing-svc repo", "an exposed credential is committed on main",
            severity=0.95)
        assert out1["flagged"] is True and out1["delivered"] is True
        # exactly ONE guarded delivery, clearly boundary-respecting
        assert len(delivered) == 1
        assert delivered[0]["type"] == "proposal"
        assert "you should know" in delivered[0]["description"].lower()
        # a second flag for the same boundary does NOT fire again
        out2 = await m.flag_critical(
            "billing-svc repo", "another critical thing", severity=0.95)
        assert out2["flagged"] is False and out2["reason"] == "already_flagged_once"
        assert len(delivered) == 1
        # below-threshold stays internal
        out3 = await m.flag_critical("billing-svc repo", "minor nit", severity=0.3)
        assert out3["flagged"] is False and out3["noted_internally"] is True
    asyncio.run(run())


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


def test_manager_revocation_requires_confirmation():
    """Asymmetric friction: a lift must be confirmed, never one-turn (1c)."""
    m = DirectiveManager(DirectiveStore(db_path=None))
    cap = m.capture_from_message("stop working on the acme-corp integration")
    assert m.store.count_active() == 1
    assert cap.ack and "stop acting on" in cap.ack  # confirmation echo (1a, tiered)
    # A revocation attempt does NOT lift immediately; it stages a confirmation.
    r = m.capture_from_message("actually you can work on acme-corp again")
    assert r.needs_confirmation is not None
    assert m.store.count_active() == 1  # still in place
    assert m.check(Action(kind="directed_action", text="acme-corp integration")).allowed is False
    # Only an explicit affirmation lifts it.
    c = m.capture_from_message("yes, confirm")
    assert c.revoked and c.ack
    assert m.store.count_active() == 0
    assert m.check(Action(kind="directed_action", text="acme-corp integration")).allowed is True


def test_manager_non_affirmation_does_not_lift():
    m = DirectiveManager(DirectiveStore(db_path=None))
    m.capture_from_message("don't touch the payments repo")
    m.capture_from_message("you can work on payments again")   # stages pending
    m.capture_from_message("what's the weather")               # not an affirmation
    assert m.store.count_active() == 1  # boundary held; stray text can't confirm


# ---------------------------------------------------------------------------
# 2026-07-05 self-poisoning incident regressions: fragments, canaries, and
# self-echo must never become boundaries; a surviving generic term must never
# match everything.
# ---------------------------------------------------------------------------

def _mgr():
    from colony_sidecar.directives.service import DirectiveManager
    from colony_sidecar.directives.store import DirectiveStore
    return DirectiveManager(DirectiveStore())


def test_anaphora_fragment_subjects_are_refused():
    m = _mgr()
    # Each of these was found LIVE in the poisoned store.
    for msg in (
        "please stop that and wipe it from colony",
        "don't attempt them",
        "you should never do Y and I hate it",
    ):
        m.capture_from_message(msg)
    subjects = [d.subject for d in m.store.active()]
    assert not any(s.lower().startswith(("that", "them", "it", "y ")) for s in subjects), subjects


def test_own_refusal_text_is_not_captured():
    m = _mgr()
    # The executor's boundary-refusal message fed back through turn sync.
    r = m.capture_from_message(
        "Those actions violate a standing boundary from the owner and were "
        "refused. Do not attempt them; summarise and stop.")
    assert r.captured == []


def test_system_origin_text_never_captures():
    m = _mgr()
    r = m.capture_from_message(
        "System note: the previous turn was interrupted. Never skip the "
        "review step when resuming.")
    assert r.captured == [] and r.revoked == []


def test_duplicate_captures_do_not_pile_up():
    m = _mgr()
    m.capture_from_message("don't touch the staging database")
    m.capture_from_message("don't touch the staging database")
    m.capture_from_message("don't touch the staging database")
    subs = [d.subject for d in m.store.active()]
    assert len(subs) == 1


def test_common_term_fragment_cannot_match_everything():
    from colony_sidecar.directives.guard import _terms_match
    from colony_sidecar.directives.models import normalize_terms
    # A fragment whose only surviving terms are generic + the product name
    # ("wipe", "colony") must not bind an unrelated internal job text.
    directive_terms = normalize_terms("that and wipe it from colony")
    action_terms = normalize_terms(
        "Observe the system domain through your own connections and report "
        "snapshots to Colony")
    assert _terms_match(directive_terms, action_terms) is False


def test_specific_subjects_still_match():
    from colony_sidecar.directives.guard import _terms_match
    from colony_sidecar.directives.models import normalize_terms
    # Real boundaries keep binding: distinctive token…
    assert _terms_match(normalize_terms("touching the payments-api repo"),
                        normalize_terms("open a PR against payments-api"))
    # …and short-but-complete subjects via the all-terms path.
    assert _terms_match(normalize_terms("leave the GLM cluster alone"),
                        normalize_terms("restart the glm cluster head node"))


def test_mid_generic_word_alone_is_not_distinctive():
    from colony_sidecar.directives.guard import _terms_match
    from colony_sidecar.directives.models import normalize_terms
    # "attempt them" -> ["attempt"]; a random action that happens to contain
    # "attempting" must not be blocked by the fragment.
    assert _terms_match(normalize_terms("attempt them"),
                        normalize_terms("attempting the calendar sync now")) is False


def test_short_word_is_not_a_stem_of_a_long_term():
    from colony_sidecar.directives.guard import _terms_match
    from colony_sidecar.directives.models import normalize_terms
    # "what" must not count as a morphological variant of "whatsapp": a
    # WhatsApp boundary cannot bind every sentence containing "what".
    assert _terms_match(
        normalize_terms("writing or implementing the WhatsApp bridge spec yourself"),
        normalize_terms("what must a server-side worker governor enforce at claim time"),
    ) is False
    # Real morphological variants still bind.
    assert _terms_match(normalize_terms("stop deploying the payments service"),
                        normalize_terms("deploy payments service to prod"))
    assert _terms_match(normalize_terms("leave the tls certs alone"),
                        normalize_terms("update the tls cert bundle"))
