"""DirectiveExtractor -- capture standing directives from owner messages.

Deterministic, cue-driven detection of prohibitions ("don't / stop / avoid /
leave X alone"), requirements ("always / from now on / make sure to X"), and
revocations ("you can X again / disregard that"). Owner-gated by the caller.

Only explicitly lasting clauses become standing rules. Ordinary task limits
stay in their source conversation. Pure communication-STYLE directives
("be concise", "no emoji") are left to the PreferenceLearner and skipped here.
"""

from __future__ import annotations

import re
from typing import List

from apsimo.directives.models import (
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

# Revocation openers -> the owner is lifting a prior boundary.
_REVOKE_PATTERNS = [
    re.compile(r"\b(?:you\s+can\s+now|go\s+ahead\s+and|actually,?\s+(?:you\s+can|go\s+ahead)|nevermind|never\s+mind|disregard(?:\s+(?:that|what\s+i\s+said))?|forget\s+what\s+i\s+said)\s*(?P<subj>.*)", re.I),
    re.compile(r"\b(?:resume|unpause|restart)\s+(?P<subj>.+)", re.I),
]

# One-command global pause (Amendment 1.5): the owner's kill switch. Any of
# these phrasings becomes an immediate GLOBAL ACT-level boundary: every
# autonomous act is refused until the owner lifts it (staged confirmation,
# like any boundary lift). Perception/reads stay open (ACT semantics).
_GLOBAL_PAUSE_PATTERNS = [
    re.compile(r"\b(?:stop|pause|halt|freeze|suspend)\s+(?:(?:all|your|the|any)\s+)*"
               r"(?:acting|autonomy|autonomous\s+(?:actions?|work|mode)|"
               r"taking\s+actions?|doing\s+things)\b", re.I),
    re.compile(r"\bstand\s+down\b", re.I),
    re.compile(r"\bstop\s+acting\s+on\s+your\s+own\b", re.I),
]


def make_global_pause_directive(raw_text: str = "",
                                source: str = "owner_explicit") -> Directive:
    """The global ACT-level pause boundary (kill switch)."""
    from apsimo.directives.models import GLOBAL_PAUSE_TERM, Level
    return Directive(
        subject="all autonomous actions (global pause)",
        polarity=Polarity.PROHIBIT,
        raw_text=raw_text or "stop acting",
        # GLOBAL_PAUSE_TERM makes the guard refuse every act-capability
        # action; the plain terms let "resume autonomy/acting" find and lift
        # this directive through the normal staged confirmation.
        match_terms=[GLOBAL_PAUSE_TERM, "autonomy", "acting", "autonomous"],
        source=source, confidence=1.0, level=Level.ACT,
    )

# Clause terminators: cut the subject at the first of these.
_CLAUSE_END = re.compile(r"\.(?=\s|$)|[;,!?\n]| but | and then | because | since | unless | so that ", re.I)

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


# Durable admission requires positive lasting scope. Task instructions remain
# canonical evidence, even when they contain imperatives such as "never".
_TASK_SCOPE = re.compile(
    r"\b(?:for|during) (?:this|the current) (?:task|request|phase|run|example)\b"
    r"|\b(?:this is|these are) (?:an? |the )?(?:temporary|one[- ]off) (?:task |standing )?(?:instruction|requirement|rule|constraint)s?\b"
    r"|(?:^|[.!?;\n]\s*)(?:temporary|one[- ]off) (?:task )?(?:instruction|requirement|rule|constraint)s?\s*[:.!?;\n]"
    r"|\bnot (?:a |my )?(?:personal preference|standing (?:rule|instruction|boundary))\b"
    r"|\b(?:for now|until (?:this|the) (?:task|phase|run) (?:ends|finishes|completes))\b", re.I)
_STANDING = re.compile(r"^(?:please\s+)?(?:(?:from now on|going forward|as a standing rule|as a permanent rule|permanently)[,:]?\s+|(?:always|never)\s+)", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|[;\n]+")


def standing_clauses(message: str) -> List[str]:
    """Bounded, exact clauses with explicit lasting intent, not inferred intent."""
    text = (message or '').strip()
    if not text or _TASK_SCOPE.search(text):
        return []
    admitted = []
    for candidate in _SENTENCES.split(text):
        clause = candidate.strip()
        prefix = _STANDING.match(clause)
        if prefix is None:
            continue
        # The keyword guard cannot enforce exceptions or contextual conditions.
        # Keep these instructions in the conversation instead of broadening
        # them into an unconditional standing prohibition.
        if re.search(r'\b(?:unless|until|except|if|when|without)\b', clause, re.I):
            continue
        # Keep the exact rule clause, excluding explanatory or unrelated text.
        ending = _CLAUSE_END.search(clause, prefix.end())
        if ending:
            end = ending.start() + (ending[0] in '.!?')
            clause = clause[:end].strip()
        if 0 < len(clause) <= 500:
            if clause not in admitted:
                admitted.append(clause)
    return admitted


def extract_directives(message: str, *, source: str = "owner_explicit") -> List[Directive]:
    """Extract explicit standing owner rules; one-off work is not a rule."""
    from apsimo.directives.models import Level
    text = (message or '').strip()
    if not text:
        return []
    # The standalone pause command retains its immediate, explicit meaning.
    command = re.sub(r'^please\s+', '', text.rstrip('.!?'), flags=re.I)
    command = re.sub(r'\s+(?:for now|for a while)$', '', command, flags=re.I)
    if any(pattern.fullmatch(command) for pattern in _GLOBAL_PAUSE_PATTERNS):
        return [make_global_pause_directive(text, source=source)]
    # Lifting a rule is explicit interaction with an existing rule, not new
    # durable learning. Keep the existing confirmation behavior unchanged.
    for pattern in _REVOKE_PATTERNS:
        match = pattern.match(text)
        if match and not _TASK_SCOPE.search(text):
            subject = _clean_subject(match.group('subj') or '')
            if subject and not _is_style_only(subject):
                directive = Directive(subject=subject, polarity=Polarity.PREFER,
                                      raw_text=text, source=source)
                directive.__dict__['_revocation'] = True
                return [directive]
            return []
    result = []
    for clause in standing_clauses(text):
        prefix = _STANDING.match(clause)
        body = clause[prefix.end():].strip()
        # Never/always themselves are the lasting imperative.
        if re.search(r'\bnever\s+$', prefix[0], re.I):
            body = 'never '+body
        polarity, subject = Polarity.REQUIRE, body
        for pattern in _PROHIBIT_PATTERNS:
            match = pattern.match(body)
            if match:
                polarity, subject = Polarity.PROHIBIT, match.group('subj')
                break
        if polarity == Polarity.PROHIBIT and re.search(r'\b(?:before|after|once)\b', body, re.I):
            continue
        subject = re.sub(r'^always\s+', '', _clean_subject(subject), flags=re.I)
        if polarity == Polarity.PROHIBIT and re.match(
                r'^(?:(?:forget|fail|neglect|refuse)\s+to\b|stop\s+\w+ing\b)', subject, re.I):
            continue  # Negated omissions do not prohibit the underlying action.
        if (not subject or len(subject) < 2 or _is_style_only(subject)
                or re.match(r'^(?:(?:I|we|he|she|they|my|your|did|had|has|was|were|is|am|are)\b|have\s+(?:I|we|they)\b)', subject, re.I)
                or re.match(r'^(?:(?:again|ever|really|actually|previously|once)\s+)*(?:did|do|does|have|has|had|am|is|are|was|were)\s+(?:I|we|he|she|they|you)\b', subject, re.I)
                or polarity == Polarity.REQUIRE and not re.match(
                    r'^(?:ask|check|confirm|consult|contact|keep|leave|look|make|maintain|notify|obtain|read|remember|report|request|require|respect|review|run|save|seek|send|show|tell|test|track|use|verify|wait)\b', subject, re.I)):
            continue
        # Only the prohibited action determines observation versus action.
        # Unrelated "read the documentation" elsewhere cannot escalate it.
        perception = re.match(r'^(?:even\s+)?(?:look(?:ing)?\s+at|read(?:ing)?\b|watch(?:ing)?\b|monitor(?:ing)?\b|track(?:ing)?\b|stay\s+out\s+of|snoop|peek|observ(?:e|ing))', subject, re.I)
        result.append(Directive(subject=subject, polarity=polarity, raw_text=clause,
            match_terms=normalize_terms(subject), source=source,
            confidence=0.9 if source == 'owner_explicit' else 0.6,
            level=Level.OBSERVE if polarity == Polarity.PROHIBIT and perception else Level.ACT))
    return result


def is_revocation(directive: Directive) -> bool:
    return bool(directive.__dict__.get("_revocation"))
