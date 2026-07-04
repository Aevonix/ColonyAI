"""DirectiveExtractor -- capture standing directives from owner messages.

Deterministic, cue-driven detection of prohibitions ("don't / stop / avoid /
leave X alone"), requirements ("always / from now on / make sure to X"), and
revocations ("you can X again / disregard that"). Owner-gated by the caller.

Deliberately biased toward RECALL on prohibitions: it is safer to capture a
boundary the owner stated (and let the guard's specificity avoid over-blocking)
than to silently miss "don't touch X". Pure communication-STYLE directives
("be concise", "no emoji") are left to the PreferenceLearner and skipped here.
"""

from __future__ import annotations

import re
from typing import List, Optional

from colony_sidecar.directives.models import (
    Directive, Polarity, normalize_terms,
)

# Prohibition openers -> capture the subject that follows.
_PROHIBIT_PATTERNS = [
    re.compile(r"\b(?:do\s*not|don'?t|never|no\s+longer)\s+(?P<subj>.+)", re.I),
    re.compile(r"\b(?:stop|avoid|quit|cease|drop|skip)\s+(?P<subj>.+)", re.I),
    re.compile(r"\b(?:steer\s+clear\s+of|stay\s+away\s+from|lay\s+off|hands\s+off)\s+(?P<subj>.+)", re.I),
    re.compile(r"\bleave\s+(?P<subj>.+?)\s+alone\b", re.I),
    re.compile(r"\b(?:ignore|forget\s+about)\s+(?P<subj>.+)", re.I),
]

# Requirement openers -> capture the required behavior.
_REQUIRE_PATTERNS = [
    re.compile(r"\b(?:from\s+now\s+on|going\s+forward|always|make\s+sure(?:\s+to)?|be\s+sure\s+to|remember\s+to|you\s+must)\s+(?P<subj>.+)", re.I),
]

# Revocation openers -> the owner is lifting a prior boundary.
_REVOKE_PATTERNS = [
    re.compile(r"\b(?:you\s+can\s+now|go\s+ahead\s+and|actually,?\s+(?:you\s+can|go\s+ahead)|nevermind|never\s+mind|disregard(?:\s+(?:that|what\s+i\s+said))?|forget\s+what\s+i\s+said)\s*(?P<subj>.*)", re.I),
]

# Clause terminators: cut the subject at the first of these.
_CLAUSE_END = re.compile(r"[.;,!?\n]| but | and then | because | since | unless | so that ", re.I)

# Words that mark a pure communication-style directive (handled elsewhere).
_STYLE_ONLY = frozenset({
    "concise", "verbose", "brief", "shorter", "longer", "emoji", "emojis",
    "formal", "casual", "tone", "bullets", "bullet", "markdown", "wordy",
    "replies", "reply", "responses", "response", "language", "words", "word",
    # fillers that commonly accompany a pure style directive
    "using", "use", "being", "sound", "sounding", "talking", "writing",
})


def _clean_subject(subj: str) -> str:
    subj = _CLAUSE_END.split(subj, 1)[0].strip()
    # strip a leading gerund/verb that adds no discriminating value is NOT done
    # here: keeping "researching competitors" preserves intent; the guard's
    # loose stem matching handles research/researching.
    return subj.strip(" '\"")


def _is_style_only(subj: str) -> bool:
    terms = set(normalize_terms(subj))
    return bool(terms) and terms.issubset(_STYLE_ONLY)


def extract_directives(message: str, *, source: str = "owner_explicit") -> List[Directive]:
    """Extract zero or more directives from a single owner message."""
    if not message or not message.strip():
        return []
    text = message.strip()
    out: List[Directive] = []
    seen_subjects = set()

    def _emit(subj: str, polarity: Polarity) -> None:
        subj = _clean_subject(subj)
        if not subj or len(subj) < 2:
            return
        if _is_style_only(subj):
            return
        terms = normalize_terms(subj)
        if not terms:
            return
        key = (polarity, tuple(sorted(terms)))
        if key in seen_subjects:
            return
        seen_subjects.add(key)
        out.append(Directive(
            subject=subj, polarity=polarity, raw_text=text,
            match_terms=terms, source=source,
            confidence=0.9 if source == "owner_explicit" else 0.6,
        ))

    # Revocations first (so "actually you can X" is not read as a prohibition).
    revoked = False
    for pat in _REVOKE_PATTERNS:
        m = pat.search(text)
        if m:
            revoked = True
            # A revocation subject is returned as a PREFER 'allow' marker so the
            # caller can match+revoke an existing PROHIBIT; not itself a boundary.
            subj = _clean_subject(m.group("subj") or "")
            if subj and not _is_style_only(subj):
                d = Directive(subject=subj, polarity=Polarity.PREFER,
                              raw_text=text, source=source)
                d.__dict__["_revocation"] = True  # caller hint
                out.append(d)
            break
    if revoked:
        return out

    for pat in _PROHIBIT_PATTERNS:
        m = pat.search(text)
        if m:
            _emit(m.group("subj"), Polarity.PROHIBIT)
    for pat in _REQUIRE_PATTERNS:
        m = pat.search(text)
        if m:
            _emit(m.group("subj"), Polarity.REQUIRE)
    return out


def is_revocation(directive: Directive) -> bool:
    return bool(directive.__dict__.get("_revocation"))
