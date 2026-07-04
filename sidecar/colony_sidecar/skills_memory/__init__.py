"""Compounding learning: reusable procedure memory (item 3).

Distills a compact Skill (situation, steps, gotchas) from non-trivial
successes -- success after a retry, or a novel diagnosis -- and retrieves the
relevant ones into executor/planner prompts so the second encounter with a
problem class starts from the first one's solution. Failures update a short
per-domain strategy note (post-mortem memory).

Named ``skills_memory`` to avoid clashing with the existing ``skills/``
executor-skill registry (packaged executable skills). These are prompt-level
procedure memories for the sidecar's own reasoning loops; they inform
reasoning and never act, so retrieval is safe to run live
(COLONY_SKILLS_ENABLED, default true). Distillation costs one LLM call per
qualifying completion and starts in shadow (COLONY_SKILLS_DISTILL).
"""

from colony_sidecar.skills_memory.models import Skill, signature_overlap, situation_signature
from colony_sidecar.skills_memory.store import SkillStore, skills_enabled, skills_distill_mode
from colony_sidecar.skills_memory.distill import should_distill, distill_from_completion
from colony_sidecar.skills_memory.retrieve import relevant_skills, format_block

__all__ = [
    "Skill", "SkillStore", "signature_overlap", "situation_signature",
    "should_distill", "distill_from_completion", "relevant_skills",
    "format_block", "skills_enabled", "skills_distill_mode",
]
