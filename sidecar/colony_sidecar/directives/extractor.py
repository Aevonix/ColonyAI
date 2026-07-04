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
    from colony_sidecar.directives.models import GLOBAL_PAUSE_TERM, Level
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

    # Global pause first (Amendment 1.5): "stop acting" must never be diluted
    # into a keyword boundary; it is THE kill switch and stands alone.
    for pat in _GLOBAL_PAUSE_PATTERNS:
        if pat.search(text):
            return [make_global_pause_directive(text, source=source)]

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


# ---------------------------------------------------------------------------
# Optional LLM-assisted extraction (behind the deterministic pass) -- 1b
# ---------------------------------------------------------------------------

def llm_assist_enabled() -> bool:
    import os
    return os.environ.get("COLONY_DIRECTIVE_LLM_ASSIST", "false").strip().lower() == "true"


_LLM_SYS = (
    "You extract STANDING directives from an owner message: lasting instructions "
    "to DO or AVOID something (not one-off requests, not writing-style tweaks). "
    "Reply ONLY with JSON: {\"polarity\":\"prohibit|require|none\",\"subject\":\"...\"}. "
    "Use none unless the message clearly sets a lasting boundary or rule."
)


async def llm_extract_directives(text: str) -> List[Directive]:
    """A cheap classifier for turns the regex missed. Default OFF; only runs when
    COLONY_DIRECTIVE_LLM_ASSIST=true and an introspection endpoint is configured.
    Inferred directives are stored at lower confidence + source 'inferred' so the
    owner can correct them via the acknowledgment echo."""
    import os, json as _json
    if not text or not llm_assist_enabled():
        return []
    base = os.environ.get("COLONY_INTROSPECT_BASE_URL", "").rstrip("/")
    model = os.environ.get("COLONY_INTROSPECT_MODEL", "")
    if not base or not model:
        return []
    try:
        import aiohttp
    except ImportError:
        return []
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("COLONY_INTROSPECT_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model, "temperature": 0,
        "max_tokens": 120,
        "messages": [{"role": "system", "content": _LLM_SYS},
                     {"role": "user", "content": text[:800]}],
    }
    try:
        timeout = aiohttp.ClientTimeout(total=float(os.environ.get("COLONY_INTROSPECT_TIMEOUT", "20")))
        async with aiohttp.ClientSession() as s:
            async with s.post(base + "/chat/completions", json=payload,
                              headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        content = data["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", content, re.DOTALL)
        obj = _json.loads(m.group(0) if m else content)
    except Exception:
        return []
    pol = str(obj.get("polarity", "none")).strip().lower()
    subj = str(obj.get("subject", "")).strip()
    if pol not in ("prohibit", "require") or not subj or len(subj) < 2:
        return []
    terms = normalize_terms(subj)
    if not terms:
        return []
    return [Directive(subject=subj, polarity=Polarity(pol), raw_text=text,
                      match_terms=terms, source="inferred", confidence=0.55)]
