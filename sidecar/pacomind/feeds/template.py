"""Render distill / digest / discovery agent prompts from a FeedSpec.

The structure is distilled from a battle-tested single-feed deployment:
actionable framing ("what changes because of this"), hard link requirements,
tiered promotion rules for human-shared links, a privacy block, and a
learning-loop hook that records source promotions so the registry evolves.
"""

from __future__ import annotations

from .spec import FeedSpec


def _archive_hint(spec: FeedSpec, suffix: str = "") -> str:
    tail = f"-{suffix}" if suffix else "-HHMM"
    return f"{spec.briefs_dir}/YYYY-MM-DD{tail}.md"


def _privacy_block(spec: FeedSpec) -> str:
    lines = ["PRIVACY (hard rules for anything you post):"]
    for item in spec.privacy.get("forbidden", []):
        lines.append(f"- Never mention {item}.")
    aliases = spec.privacy.get("aliases") or []
    if aliases:
        lines.append("- Reference hardware/platforms by public product name only "
                     f"({', '.join(aliases)}).")
    note = spec.privacy.get("note")
    if note:
        lines.append(f"- {note}")
    return "\n".join(lines)


def _registry_block(spec: FeedSpec) -> str:
    if not spec.registry:
        return ""
    by_tier: dict[str, list[str]] = {"P0": [], "P1": [], "P2": []}
    for e in spec.registry:
        label = e.get("handle") or e.get("name")
        cat = e.get("category", "")
        by_tier.setdefault(e.get("weight", "P1"), []).append(
            f"{label}" + (f" ({cat})" if cat else ""))
    lines = ["SOURCE REGISTRY (tiered; used by the promotion rules):"]
    tier_meaning = {"P0": "always include, even a bare link",
                    "P1": "include; promote to P0 when a curator flags it",
                    "P2": "background only"}
    for tier in ("P0", "P1", "P2"):
        if by_tier.get(tier):
            lines.append(f"- {tier} ({tier_meaning[tier]}): " + ", ".join(by_tier[tier]))
    return "\n".join(lines)


def _promotion_block(spec: FeedSpec) -> str:
    return """PROMOTION RULES for human-shared links (apply in order, first match wins):
1. Link's domain/author matches a P0 source AND content is a new SOTA / reproducible recipe / breakthrough -> P0
2. Link's domain/author matches a P0 source -> P0
3. Link's domain/author matches a P1 source AND a curator flagged it important/!!!/P0 -> P0
4. Link's domain/author matches a P1 source -> P1
5. Link's domain/author matches a P2 source -> P2
6. A curator flagged it important/!!! -> P0
7. A curator provided any context note -> P1
8. Bare link from an unknown source -> P2

Treat every human message as a link share and PRESERVE their context notes verbatim:
text before a URL is their note about it; text after is an annotation (e.g. "and the
first reply" means follow that reply too). Never summarize their notes away - they are
first-class signal."""


def _community_block(spec: FeedSpec) -> str:
    read_back = spec.destination.get("read_back", "")
    if spec.audience != "group" or not read_back:
        return ""
    return f"""Read the destination group's recent messages from human participants since the last
brief. Use: {read_back}

{_promotion_block(spec)}

COMMUNITY INPUT section: every link humans posted in this window, in order, each with
its priority and a one-line framing answering "{spec.brief['framing']}".

ABSOLUTE RULES for the group:
- The group is a one-way broadcast channel for you: read for intel, post briefs, nothing else.
- Do NOT react to, reply to, or engage with any message in the group, ever.
- If a participant asks you a direct question there, do not answer it in the group."""


def _delivery_block(spec: FeedSpec, what: str = "brief") -> str:
    kind = spec.destination.get("kind")
    if kind == "command":
        return (f"Post the {what} to its destination by piping it to:\n"
                f"  {spec.destination['send_command']}")
    if kind == "deliver":
        return (f"Your final response IS the {what}; the scheduler delivers it automatically. "
                "Do not send it yourself with any messaging tool.")
    return (f"Do NOT post the {what} anywhere; this feed is archive-only. "
            "Your final response should be the brief text.")


def _context_files_block(spec: FeedSpec) -> str:
    files = spec.brief.get("context_files") or []
    if not files:
        return ""
    lines = ["Also read these context files if they exist and fold relevant movement into the brief:"]
    for f in files:
        lines.append(f"- {f.get('label', 'context')}: {f['path']}")
    return "\n".join(lines)


def _learning_block(spec: FeedSpec) -> str:
    if not spec.learning_loop:
        return ""
    return f"""LEARNING LOOP HOOK: after archiving, whenever you promoted a link from an unknown
source to P0/P1 on content weight alone, append one line to {spec.deltas_log}:
  TIMESTAMP  new_source=domain-or-handle (reason)  promoted_to=P1  link=...
This is how the source registry evolves. Skip the log when nothing was promoted."""


def render_distill(spec: FeedSpec) -> str:
    parts = [
        f"You are the {spec.title} distillation agent.",
        f"TOPIC CHARTER: {spec.topic}",
        f"""Your job each run:
1. Read the feed queue at {spec.queue_path}
2. Distill the top items into a brief. Plain text, no markdown. Structure:
   - Header line with feed title and date/time (local timezone)
   - HIGH PRIORITY section (P0 and P1 items), each with: title, 1-2 sentence summary,
     a "why it matters" line, benchmark numbers if any, and SOURCE LINK
   - STANDARD section (P2 items): one line each with link, cap at the top
     {spec.brief['standard_cap']} and drop the rest silently
3. ALWAYS include a source link for every item so readers can click through.

OUTPUT FRAMING: for every HIGH PRIORITY item the "why it matters" line MUST answer:
  "{spec.brief['framing']}"
Not "what does this article say" - the framing has to be actionable.""",
    ]
    for block in (_context_files_block(spec), _community_block(spec),
                  _privacy_block(spec), _registry_block(spec)):
        if block:
            parts.append(block)
    parts.append(_delivery_block(spec))
    parts.append(f"Archive the brief at {_archive_hint(spec)}")
    lb = _learning_block(spec)
    if lb:
        parts.append(lb)
    extra = spec.brief.get("extra_instructions")
    if extra:
        parts.append(extra)
    parts.append('If the queue is empty and there is genuinely nothing to report, '
                 'respond with exactly "[SILENT]" and post nothing.')
    return "\n\n".join(parts)


def render_digest(spec: FeedSpec) -> str:
    parts = [
        f"You are the {spec.title} daily digest agent.",
        f"TOPIC CHARTER: {spec.topic}",
        f"""Your job: produce ONE daily rollup of the last 24h.
1. Read the feed queue at {spec.queue_path}
2. Read the briefs archived in the last 24h under {spec.briefs_dir}/
3. Synthesize: the day's 3-7 most consequential items (dedup against what briefs
   already said - lead with what CHANGED over the day, not a re-list), each with a
   "why it matters" line answering "{spec.brief['framing']}" and a SOURCE LINK.
4. Close with a 2-3 sentence trend read: where is this topic moving?""",
    ]
    for block in (_context_files_block(spec), _community_block(spec), _privacy_block(spec)):
        if block:
            parts.append(block)
    parts.append(_delivery_block(spec, what="digest"))
    parts.append(f"Archive the digest at {_archive_hint(spec, 'digest')}")
    parts.append('If nothing meaningful happened, respond with exactly "[SILENT]".')
    return "\n\n".join(parts)


def render_discovery(spec: FeedSpec) -> str:
    return f"""You are the source-discovery agent for the {spec.title}.

TOPIC CHARTER: {spec.topic}

Your job: find NEW media sources worth adding to this feed, qualify them yourself,
and propose only the best. Search broadly with web search and VARY your angles each
run: niche experts on X/social platforms, technical blogs and newsletters, active
GitHub authors/orgs, forums and communities, benchmark/evaluation sites, academic
groups publishing with code.

QUALIFY each candidate before proposing: original signal (not aggregation)?
Track record of real numbers/working code? Active at least weekly? Not already
covered by the registry? High signal-to-noise?

FILTER HARD: zero good proposals beats one bad one. Diminishing returns over time
are expected - be honest when a sweep comes up empty.

For each qualified source propose: name + link, 2-3 recent high-signal examples with
direct links, why it fits this feed's topic charter, and how to monitor it (account
watch, RSS URL, forum URL, keyword filter). The operator applies accepted proposals
to the feed's spec file (registry + sources sections).

{_privacy_block(spec)}

{_registry_block(spec) or 'SOURCE REGISTRY: (none yet - everything qualified is new)'}

Your final response is the proposal report (or a one-line "no qualified sources this
sweep"); the scheduler delivers it."""
