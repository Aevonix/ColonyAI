"""Directive intake: owner NL directive -> deterministic ScopedTask.

Rule-based scoping keyed on a fixed operation vocabulary and the set of KNOWN
targets (configured repo mirrors + world-model entities). The output is pure
data; an optional LLM assist may PROPOSE a scope, but whatever it proposes is
re-validated through the same deterministic constructor, so prose can never
widen a scope.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from colony_sidecar.directed.models import (
    ScopedTask, ScopeLimits, READ_OPS, MUTATE_OPS,
)

# Operation cues -> vocabulary ops. Order matters: mutating cues are checked
# explicitly; everything defaults to read-only analysis.
_OP_CUES: List[Tuple[re.Pattern, List[str]]] = [
    (re.compile(r"\b(?:fix|refactor|implement|change|update|modify|patch|rewrite|clean\s*up|add)\b", re.I),
     ["analyze", "read", "search", "modify_files", "commit", "push_branch"]),
    (re.compile(r"\b(?:open|create|raise|submit)\s+(?:a\s+)?(?:pr|pull\s+request|merge\s+request)\b", re.I),
     ["open_pr"]),
    (re.compile(r"\b(?:run|execute)\s+(?:the\s+)?tests?\b", re.I), ["run_tests"]),
    (re.compile(r"\b(?:look\s+(?:at|into|over)|review|audit|analy[sz]e|investigate|check|summari[sz]e|read|inspect|assess|report\s+on)\b", re.I),
     ["analyze", "read", "search"]),
]

_COMMIT_CAP = re.compile(r"\b(?:at\s+most|max(?:imum)?|no\s+more\s+than|up\s+to)\s+(\d{1,2})\s+commits?\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def resolve_targets(text: str, known_targets: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Match the directive text against KNOWN targets (repos/entities).

    known_targets: [{kind, name, ref?}]. Matching is by name token (exact or
    normalised containment) so 'the colony repo' matches a repo named
    'ColonyAI' only if the owner-designated alias says so -- never fuzzy-wide.
    """
    low = (text or "").lower()
    norm = _norm(text)
    out: List[Dict[str, str]] = []
    seen = set()
    for t in known_targets or []:
        name = t.get("name", "")
        if not name or name.lower() in seen:
            continue
        aliases = [name] + [a for a in (t.get("aliases") or "").split("|") if a]
        for alias in aliases:
            al = alias.lower()
            if (al in low) or (len(_norm(alias)) >= 4 and _norm(alias) in norm):
                out.append({"kind": t.get("kind", "repo"), "name": name,
                            "ref": t.get("ref", "")})
                seen.add(name.lower())
                break
    return out


def scope_from_directive(
    directive_text: str,
    known_targets: List[Dict[str, str]],
    default_limits: Optional[ScopeLimits] = None,
) -> ScopedTask:
    """Deterministically construct a ScopedTask from an owner directive."""
    text = (directive_text or "").strip()
    ops: List[str] = []
    for pat, cue_ops in _OP_CUES:
        if pat.search(text):
            for op in cue_ops:
                if op not in ops:
                    ops.append(op)
    if not ops:
        ops = ["analyze", "read", "search"]

    limits = default_limits or ScopeLimits()
    m = _COMMIT_CAP.search(text)
    if m:
        limits.max_commits = max(1, min(20, int(m.group(1))))

    # Objective: the directive minus filler openers.
    objective = re.sub(r"^\s*(?:please|hey|can you|could you|i want you to|go)\s+",
                       "", text, flags=re.I).strip()

    return ScopedTask(
        directive_text=text,
        objective=objective[:300],
        targets=resolve_targets(text, known_targets),
        allowed_ops=ops,
        limits=limits,
    )
