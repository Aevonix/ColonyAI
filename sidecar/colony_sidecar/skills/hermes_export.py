"""Colony Skills — render approved skills as Hermes SKILL.md documents (v0.18.0).

The Hermes↔Colony skills bridge, outbound half: when a captured DRAFT
skill is approved (DRAFT→ACTIVE), Colony can also publish it as an
*instructional* Hermes skill — a ``SKILL.md`` with YAML frontmatter
under ``~/.hermes/skills/colony/<skill-slug>/`` — so the agent host can
load the procedure as guidance, independent of Colony's sandboxed
executable skill.

Procedural-export heuristic (see :func:`is_procedural`):

* MCP-bridged skills (``manifest.origin == "mcp"``) are **never**
  exported — they wrap a remote tool endpoint; there is no procedure
  to teach.
* Skills whose pattern/source carries trace-derived steps — a
  non-empty ``step_sequence``, ``colony.tools.invoke`` replay calls in
  the synthesized source, ``Capture steps:`` in the docstring, or
  ``# Step:`` scaffold comments — are procedural and exported.
* Skills whose source is clearly pure computation (parsable Python
  with a ``run()`` and none of the procedural markers above) are
  skipped: rendering them as a step-by-step guide would be noise.
* Anything ambiguous (no source, unreadable skill dir, bare manifest)
  is exported anyway — emission is already double-gated by the
  ``COLONY_EMIT_HERMES_SKILLS`` env switch and human approval.

Everything here is best-effort: callers at the approval transition
must log-and-continue on failure, never block activation.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Author string stamped into every exported frontmatter.
HERMES_AUTHOR = "Colony (captured from agent work)"

#: Marker embedded in the frontmatter provenance comment block. A
#: pre-existing SKILL.md is only ever overwritten when its frontmatter
#: contains this marker — files authored by anyone else are sacred.
PROVENANCE_MARKER = "colony:provenance"

#: Env switch — exports are off unless this is truthy.
EMIT_ENV = "COLONY_EMIT_HERMES_SKILLS"

#: Env override for the export base directory.
BASE_DIR_ENV = "COLONY_HERMES_SKILLS_DIR"

_DEFAULT_BASE = Path("~/.hermes/skills/colony")

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_TOOL_INVOKE_RE = re.compile(r"colony\.tools\.invoke\(\s*['\"]([^'\"]+)['\"]")
_TRUTHY = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return slug or "unnamed-skill"


def _yaml_str(value: Any) -> str:
    """Quote a scalar for YAML. JSON string quoting is valid YAML."""
    return json.dumps(str(value if value is not None else ""))


def _get(manifest: Any, attr: str, default: Any = None) -> Any:
    """getattr that also tolerates plain dicts (test fakes)."""
    if isinstance(manifest, dict):
        return manifest.get(attr, default)
    return getattr(manifest, attr, default)


def hermes_export_enabled() -> bool:
    return os.environ.get(EMIT_ENV, "false").strip().lower() in _TRUTHY


def hermes_base_dir(base_dir: Optional[Path] = None) -> Path:
    if base_dir is not None:
        return Path(base_dir).expanduser()
    env = os.environ.get(BASE_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    # Track the sync/poller root (HERMES_SKILLS_DIR) so a deployment that
    # relocates the skills root doesn't silently split the export dir away
    # from where skills_sync/agent_bridge read (they'd never see exports).
    sync_root = os.environ.get("HERMES_SKILLS_DIR", "").strip()
    if sync_root:
        return (Path(sync_root) / "colony").expanduser()
    return _DEFAULT_BASE.expanduser()


# ---------------------------------------------------------------------------
# Pattern/source recovery
# ---------------------------------------------------------------------------

def load_body_source(manifest: Any) -> Dict[str, Any]:
    """Recover renderable pattern data from a packaged skill directory.

    The ``ExtractedPattern`` itself is not persisted; what survives is
    ``skill.py`` (synthesized source whose ``run()`` docstring carries
    the original task and whose body replays captured tool calls via
    ``colony.tools.invoke``). Returns a dict shaped like the pattern
    fields ``render_skill_md`` understands — empty when nothing can be
    recovered.
    """
    out: Dict[str, Any] = {}
    skill_dir = _get(manifest, "skill_dir")
    if not skill_dir:
        return out
    source_path = Path(str(skill_dir)) / "skill.py"
    try:
        source = source_path.read_text(encoding="utf-8")
    except OSError:
        return out
    out["source_code"] = source

    # run() docstring (or module docstring) → body prose.
    try:
        tree = ast.parse(source)
        doc = ast.get_docstring(tree) or ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "run":
                doc = ast.get_docstring(node) or doc
                break
        if doc:
            out["docstring"] = doc
    except SyntaxError:
        pass

    # Replayed tool calls → step sequence.
    steps = [f"Invoke the `{tool}` tool" for tool in _TOOL_INVOKE_RE.findall(source)]
    if steps:
        out["step_sequence"] = steps
    return out


# ---------------------------------------------------------------------------
# Procedural heuristic
# ---------------------------------------------------------------------------

def is_procedural(manifest: Any, pattern_or_source: Optional[Dict[str, Any]]) -> bool:
    """Decide whether a skill carries teachable *procedure*.

    See the module docstring for the full decision table. In short:
    MCP wrappers → no; trace-derived steps/tool calls → yes; clearly
    pure computation → no; ambiguous → yes (export anyway).
    """
    if _get(manifest, "origin") == "mcp":
        return False

    p = pattern_or_source or {}
    steps = p.get("step_sequence") or []
    source = p.get("source_code") or ""
    docstring = p.get("docstring") or ""

    if steps:
        return True
    if "colony.tools.invoke" in source:
        return True
    if "Capture steps:" in docstring or "# Step:" in source:
        return True

    # Clearly pure computation: real source with a run() and zero
    # procedural markers. (A NotImplementedError scaffold without
    # steps also lands here — there is nothing to teach yet.)
    if source:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return True  # unparseable → in doubt → export
        has_run = any(
            isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "run"
            for node in ast.walk(tree)
        )
        if has_run:
            return False

    # No usable pattern/source data at all → in doubt → export.
    return True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_frontmatter(manifest: Any, slug: str) -> str:
    description = str(_get(manifest, "description") or "").strip() or (
        f"Colony-captured skill {slug}"
    )
    version = str(_get(manifest, "version") or "1.0.0")
    tags: List[str] = [str(t) for t in (_get(manifest, "tags") or [])]

    lines = [
        "---",
        f"name: {slug}",
        f"description: {_yaml_str(description.splitlines()[0])}",
        f"version: {_yaml_str(version)}",
        f"author: {_yaml_str(HERMES_AUTHOR)}",
    ]
    if tags:
        lines.append("metadata:")
        lines.append("  hermes:")
        lines.append("    tags:")
        lines.extend(f"      - {_yaml_str(t)}" for t in tags)

    # Provenance comment block — the marker below is what allows a
    # future re-export to overwrite this file (and nothing else).
    lines.append(f"# {PROVENANCE_MARKER}")
    lines.append(f"#   colony_skill_id: {_yaml_str(_get(manifest, 'skill_id') or slug)}")
    origin_task_id = _get(manifest, "origin_task_id")
    if origin_task_id:
        lines.append(f"#   origin_task_id: {_yaml_str(origin_task_id)}")
    lines.append(
        f"#   exported_at: {_yaml_str(datetime.now(timezone.utc).isoformat())}"
    )
    lines.append("---")
    return "\n".join(lines)


def _schema_usage_notes(manifest: Any) -> List[str]:
    notes: List[str] = []
    input_schema = _get(manifest, "input_schema") or {}
    props = input_schema.get("properties") or {} if isinstance(input_schema, dict) else {}
    required = set(input_schema.get("required") or []) if isinstance(input_schema, dict) else set()
    for key, spec in props.items():
        typ = (spec or {}).get("type", "any") if isinstance(spec, dict) else "any"
        req = "required" if key in required else "optional"
        notes.append(f"- Input `{key}` ({typ}, {req})")
    output_schema = _get(manifest, "output_schema") or {}
    if isinstance(output_schema, dict) and output_schema.get("type"):
        notes.append(f"- Produces a {output_schema['type']} result")
    deps = _get(manifest, "dependencies") or []
    if deps:
        notes.append(f"- Depends on: {', '.join(str(d) for d in deps)}")
    return notes


def _docstring_steps(docstring: str) -> List[str]:
    """Pull ``1. tool(summary)`` style lines out of a captured docstring."""
    steps: List[str] = []
    for line in docstring.splitlines():
        m = re.match(r"\s*\d+\.\s+(.*\S)", line)
        if m:
            steps.append(m.group(1))
    return steps


def render_skill_md(manifest: Any, pattern_or_source: Dict[str, Any]) -> str:
    """Render a Hermes-faithful SKILL.md for an approved Colony skill.

    ``manifest`` is a ``SkillManifest`` (or anything duck-typed like
    one); ``pattern_or_source`` is a dict carrying whatever pattern
    data is available (``docstring``, ``step_sequence``, ``source_code``
    — see :func:`load_body_source`).
    """
    p = pattern_or_source or {}
    skill_id = str(_get(manifest, "skill_id") or _get(manifest, "name") or "unnamed")
    slug = _slugify(skill_id)
    name = str(_get(manifest, "name") or slug.replace("-", " ").title())
    description = str(_get(manifest, "description") or "").strip()
    docstring = str(p.get("docstring") or "").strip()
    tags = [str(t) for t in (_get(manifest, "tags") or [])]

    body: List[str] = ["", f"# {name}", ""]

    # --- What the skill does ---
    body.append("## What this skill does")
    body.append("")
    body.append(description or f"Procedure captured by Colony from agent task work ({skill_id}).")
    if docstring and docstring.splitlines()[0] != description:
        body.append("")
        body.append(docstring.splitlines()[0])
    body.append("")

    # --- When to use it ---
    body.append("## When to use this skill")
    body.append("")
    body.append(
        "Use this skill when the task at hand resembles the work it was "
        "captured from"
        + (f" — topics: {', '.join(tags)}." if tags else ".")
    )
    trigger_patterns = _get(manifest, "trigger_patterns") or []
    if trigger_patterns:
        body.append("")
        body.append("Trigger hints:")
        body.extend(f"- `{t}`" for t in trigger_patterns)
    body.append("")

    # --- Procedure ---
    steps: List[str] = [str(s) for s in (p.get("step_sequence") or [])]
    if not steps and docstring:
        steps = _docstring_steps(docstring)
    body.append("## Procedure")
    body.append("")
    if steps:
        body.extend(f"{i}. {step}" for i, step in enumerate(steps, start=1))
    else:
        body.append(
            "No step-by-step trace was captured; follow the description "
            "above and adapt to the task context."
        )
    body.append("")

    # --- Usage notes from the schemas ---
    notes = _schema_usage_notes(manifest)
    if notes:
        body.append("## Usage notes")
        body.append("")
        body.extend(notes)
        body.append("")

    body.append("---")
    body.append(
        f"*Captured by Colony from agent work (colony skill `{skill_id}`"
        + (
            f", origin task `{_get(manifest, 'origin_task_id')}`"
            if _get(manifest, "origin_task_id")
            else ""
        )
        + "). The executable form runs sandboxed inside Colony; this "
        "document is the human/agent-readable procedure.*"
    )
    body.append("")

    return _render_frontmatter(manifest, slug) + "\n".join(body)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _frontmatter_block(text: str) -> str:
    """Return the YAML frontmatter region of a SKILL.md ('' if none)."""
    stripped = text.lstrip("﻿\r\n ")
    if not stripped.startswith("---"):
        return ""
    end = stripped.find("\n---", 3)
    if end == -1:
        return ""
    return stripped[: end + 4]


def export_to_hermes(
    manifest: Any,
    body_source: Optional[Dict[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Write ``<base>/<skill-slug>/SKILL.md`` for an approved skill.

    Returns the written path, or ``None`` when emission is disabled
    (``COLONY_EMIT_HERMES_SKILLS`` defaults to off) or the target file
    exists and was not authored by Colony (no provenance marker in its
    frontmatter — never clobber a hand-written Hermes skill).

    The write is atomic: rendered to a temp file in the target
    directory, then ``os.replace``d into place.
    """
    if not hermes_export_enabled():
        logger.debug("Hermes export disabled (%s is not truthy)", EMIT_ENV)
        return None

    skill_id = str(_get(manifest, "skill_id") or _get(manifest, "name") or "unnamed")
    slug = _slugify(skill_id)
    target_dir = hermes_base_dir(base_dir) / slug
    target = target_dir / "SKILL.md"

    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Hermes export: cannot read existing %s: %s", target, exc)
            return None
        if PROVENANCE_MARKER not in _frontmatter_block(existing):
            logger.warning(
                "Hermes export: %s exists and is not colony-authored — refusing to overwrite",
                target,
            )
            return None

    rendered = render_skill_md(manifest, body_source or {})
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".skill-", suffix=".md.tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("Hermes export: wrote %s (skill %s)", target, skill_id)
    return target


def export_approved_skill(
    manifest: Any,
    pattern_or_source: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    """Approval-transition hook: export a freshly ACTIVE skill.

    Best-effort by contract — the caller (the DRAFT→ACTIVE transition)
    wraps this in try/except and never lets a failure block activation.
    Returns the written path or ``None`` (disabled, non-procedural, or
    overwrite-protected).
    """
    if not hermes_export_enabled():
        return None
    body = dict(pattern_or_source) if pattern_or_source else load_body_source(manifest)
    if not is_procedural(manifest, body):
        logger.info(
            "Hermes export: skill %s is not procedural — skipping",
            _get(manifest, "skill_id"),
        )
        return None
    return export_to_hermes(manifest, body)
