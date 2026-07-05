"""COLONY_AUTONOMY_PRESET -- one knob for the whole autonomy posture.

Colony's agency is spread across a dozen COLONY_*_MODE flags. Individually
they are the right override surface, but a fresh install should not need to
know them all to get a coherent posture. The preset supplies DEFAULTS for
every autonomy flag at once; an explicitly set env var always wins, and the
hardcoded per-subsystem fallback still applies when no preset is set.

Presets:
    passive     -- observe and remember only; nothing thinks, acts, or writes.
    calibration -- everything runs in shadow/dry_run and earns autonomy
                   through the trust engine (the recommended starting point).
    autonomous  -- subsystems run live, still bounded by DirectiveGuard,
                   approval tiering, the immutable floor, and trust gating.

The sandbox never goes live from a preset (code execution is an explicit,
per-deployment decision); "autonomous" caps it at dry_run.

Resolution order for every flag:
    explicit env var  >  COLONY_AUTONOMY_PRESET default  >  built-in fallback
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PRESET_ENV = "COLONY_AUTONOMY_PRESET"

PRESETS: Dict[str, Dict[str, str]] = {
    "passive": {
        "COLONY_EXECUTOR_ENABLED": "false",
        "COLONY_COGNITION_ENABLED": "false",
        "COLONY_INTROSPECT_ENABLED": "false",
        "COLONY_THINKING_MODE": "off",
        "COLONY_PROJECTS_MODE": "off",
        "COLONY_BELIEFS_MODE": "off",
        "COLONY_WORLD_POPULATE_MODE": "off",
        "COLONY_WORLD_LLM_EXTRACT": "off",
        "COLONY_SKILLS_DISTILL": "off",
        "COLONY_ESCALATION_MINING": "off",
        "COLONY_CONNECTORS_MODE": "off",
        "COLONY_WORKERS_MODE": "off",
        "COLONY_DIRECTED_MODE": "off",
        "COLONY_SANDBOX_MODE": "off",
    },
    "calibration": {
        "COLONY_EXECUTOR_ENABLED": "true",
        "COLONY_COGNITION_ENABLED": "true",
        "COLONY_INTROSPECT_ENABLED": "true",
        "COLONY_THINKING_MODE": "shadow",
        "COLONY_PROJECTS_MODE": "shadow",
        "COLONY_BELIEFS_MODE": "shadow",
        "COLONY_WORLD_POPULATE_MODE": "shadow",
        "COLONY_WORLD_LLM_EXTRACT": "shadow",
        "COLONY_SKILLS_DISTILL": "shadow",
        "COLONY_ESCALATION_MINING": "shadow",
        "COLONY_CONNECTORS_MODE": "shadow",
        "COLONY_WORKERS_MODE": "shadow",
        "COLONY_DIRECTED_MODE": "dry_run",
        "COLONY_SANDBOX_MODE": "dry_run",
    },
    "autonomous": {
        "COLONY_EXECUTOR_ENABLED": "true",
        "COLONY_COGNITION_ENABLED": "true",
        "COLONY_INTROSPECT_ENABLED": "true",
        "COLONY_THINKING_MODE": "live",
        "COLONY_PROJECTS_MODE": "live",
        "COLONY_BELIEFS_MODE": "live",
        "COLONY_WORLD_POPULATE_MODE": "live",
        "COLONY_WORLD_LLM_EXTRACT": "live",
        "COLONY_SKILLS_DISTILL": "live",
        "COLONY_ESCALATION_MINING": "live",
        "COLONY_CONNECTORS_MODE": "live",
        "COLONY_WORKERS_MODE": "live",
        "COLONY_DIRECTED_MODE": "live",
        "COLONY_SANDBOX_MODE": "dry_run",  # live is explicit-only
    },
}


def preset_name() -> str:
    """The active preset name, or "" when unset/unknown."""
    name = os.environ.get(PRESET_ENV, "").strip().lower()
    if name and name not in PRESETS:
        logger.warning("%s=%r is not a known preset (%s); ignoring",
                       PRESET_ENV, name, "/".join(sorted(PRESETS)))
        return ""
    return name if name in PRESETS else ""


def resolve(env_name: str, valid: tuple, fallback: str) -> str:
    """Resolve a mode flag: explicit env > preset default > fallback.

    An explicitly-set-but-invalid env value falls back exactly as the
    legacy per-subsystem readers did (to ``fallback``), preserving their
    behavior; the preset only fills the UNSET case.
    """
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        v = raw.strip().lower()
        return v if v in valid else fallback
    preset = preset_name()
    if preset:
        v = PRESETS[preset].get(env_name, "")
        if v in valid:
            return v
    return fallback


def resolve_bool(env_name: str, fallback: bool = False) -> bool:
    """Boolean twin of :func:`resolve` (true/1/yes are truthy)."""
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip().lower() in ("true", "1", "yes")
    preset = preset_name()
    if preset:
        v = PRESETS[preset].get(env_name, "")
        if v:
            return v in ("true", "1", "yes")
    return fallback


def snapshot() -> Dict[str, str]:
    """Effective value of every preset-managed flag (for doctor/status)."""
    out: Dict[str, str] = {"preset": preset_name() or "(none)"}
    domains = {
        "COLONY_EXECUTOR_ENABLED": (("true", "false"), "false"),
        "COLONY_COGNITION_ENABLED": (("true", "false"), "false"),
        "COLONY_INTROSPECT_ENABLED": (("true", "false"), "false"),
        "COLONY_THINKING_MODE": (("off", "shadow", "live"), "off"),
        "COLONY_PROJECTS_MODE": (("off", "shadow", "live"), "shadow"),
        "COLONY_BELIEFS_MODE": (("off", "shadow", "live"), "shadow"),
        "COLONY_WORLD_POPULATE_MODE": (("off", "shadow", "live"), "shadow"),
        "COLONY_WORLD_LLM_EXTRACT": (("off", "shadow", "live"), "off"),
        "COLONY_SKILLS_DISTILL": (("off", "shadow", "live"), "shadow"),
        "COLONY_ESCALATION_MINING": (("off", "shadow", "live"), "shadow"),
        "COLONY_CONNECTORS_MODE": (("off", "shadow", "live"), "off"),
        "COLONY_WORKERS_MODE": (("off", "shadow", "live"), "shadow"),
        "COLONY_DIRECTED_MODE": (("off", "dry_run", "live"), "dry_run"),
        "COLONY_SANDBOX_MODE": (("off", "dry_run", "live"), "off"),
    }
    for env_name, (valid, fallback) in domains.items():
        if valid == ("true", "false"):
            out[env_name] = str(resolve_bool(env_name, fallback == "true")).lower()
        else:
            out[env_name] = resolve(env_name, valid, fallback)
    return out
