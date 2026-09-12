"""PACOMIND_AUTONOMY_PRESET -- one knob for the whole autonomy posture.

PacoMind's agency is spread across a dozen PACOMIND_*_MODE flags. Individually
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
    explicit env var  >  PACOMIND_AUTONOMY_PRESET default  >  built-in fallback
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PRESET_ENV = "PACOMIND_AUTONOMY_PRESET"

# Preset <-> loop-mode coupling (H4.1). The presets exist to make subsystems
# run on loop ticks, but a reactive loop never ticks — so historically a
# preset without an explicit PACOMIND_AUTONOMY_MODE=proactive was a PacoMind
# where everything LOOKED enabled and nothing ever ran. With coupling ON
# (the default), an active preset also supplies the loop mode default.
# Explicit PACOMIND_AUTONOMY_MODE always wins (the rollback path), and
# PACOMIND_PRESET_LOOP_COUPLING=off restores the old env-only resolution.
COUPLING_ENV = "PACOMIND_PRESET_LOOP_COUPLING"
LOOP_MODE_ENV = "PACOMIND_AUTONOMY_MODE"

PRESETS: Dict[str, Dict[str, str]] = {
    "passive": {
        "PACOMIND_AUTONOMY_MODE": "reactive",
        "PACOMIND_EXECUTOR_ENABLED": "false",
        "PACOMIND_COGNITION_ENABLED": "false",
        "PACOMIND_INTROSPECT_ENABLED": "false",
        "PACOMIND_THINKING_MODE": "off",
        "PACOMIND_PROJECTS_MODE": "off",
        "PACOMIND_BELIEFS_MODE": "off",
        "PACOMIND_WORLD_POPULATE_MODE": "off",
        "PACOMIND_WORLD_LLM_EXTRACT": "off",
        "PACOMIND_SKILLS_DISTILL": "off",
        "PACOMIND_ESCALATION_MINING": "off",
        "PACOMIND_CONNECTORS_MODE": "off",
        "PACOMIND_WORKERS_MODE": "off",
        "PACOMIND_DIRECTED_MODE": "off",
        "PACOMIND_SANDBOX_MODE": "off",
        "PACOMIND_EXPECTATIONS": "off",
        "PACOMIND_WORKSPACE": "off",
    },
    "calibration": {
        "PACOMIND_AUTONOMY_MODE": "proactive",
        "PACOMIND_EXECUTOR_ENABLED": "true",
        "PACOMIND_COGNITION_ENABLED": "true",
        "PACOMIND_INTROSPECT_ENABLED": "true",
        "PACOMIND_THINKING_MODE": "shadow",
        "PACOMIND_PROJECTS_MODE": "shadow",
        "PACOMIND_BELIEFS_MODE": "shadow",
        "PACOMIND_WORLD_POPULATE_MODE": "shadow",
        "PACOMIND_WORLD_LLM_EXTRACT": "shadow",
        "PACOMIND_SKILLS_DISTILL": "shadow",
        "PACOMIND_ESCALATION_MINING": "shadow",
        "PACOMIND_CONNECTORS_MODE": "shadow",
        "PACOMIND_WORKERS_MODE": "shadow",
        "PACOMIND_DIRECTED_MODE": "dry_run",
        "PACOMIND_SANDBOX_MODE": "dry_run",
        # Expectations are pure measurement (predictions scored against
        # reality -> the calibration signal the trust engine graduates on),
        # so calibration turns them fully on; the workspace runs shadow like
        # every other thinking subsystem.
        "PACOMIND_EXPECTATIONS": "on",
        "PACOMIND_WORKSPACE": "shadow",
    },
    "autonomous": {
        "PACOMIND_AUTONOMY_MODE": "proactive",
        "PACOMIND_EXECUTOR_ENABLED": "true",
        "PACOMIND_COGNITION_ENABLED": "true",
        "PACOMIND_INTROSPECT_ENABLED": "true",
        "PACOMIND_THINKING_MODE": "live",
        "PACOMIND_PROJECTS_MODE": "live",
        "PACOMIND_BELIEFS_MODE": "live",
        "PACOMIND_WORLD_POPULATE_MODE": "live",
        "PACOMIND_WORLD_LLM_EXTRACT": "live",
        "PACOMIND_SKILLS_DISTILL": "live",
        "PACOMIND_ESCALATION_MINING": "live",
        "PACOMIND_CONNECTORS_MODE": "live",
        "PACOMIND_WORKERS_MODE": "live",
        "PACOMIND_DIRECTED_MODE": "live",
        "PACOMIND_SANDBOX_MODE": "dry_run",  # live is explicit-only
        "PACOMIND_EXPECTATIONS": "on",       # binary: on/off (no shadow tier)
        "PACOMIND_WORKSPACE": "live",
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


def loop_coupling_enabled() -> bool:
    """Whether preset<->loop-mode coupling is on (default ON).

    Any error resolving the flag fails toward OFF, so the loop mode falls
    back to the pre-coupling env-only resolution (reactive by default) —
    coupling can only ever raise the posture deliberately, never by accident.
    """
    try:
        raw = os.environ.get(COUPLING_ENV, "on").strip().lower()
        return raw not in ("off", "false", "0", "no")
    except Exception:
        return False


def coupled_loop_mode() -> Optional[str]:
    """The active preset's loop mode when coupling is on; None otherwise.

    Returns "reactive"/"proactive" only when PACOMIND_PRESET_LOOP_COUPLING is
    on AND a known preset is active AND the preset table carries a valid
    mode. Never raises — every failure path yields None (i.e. reactive via
    the caller's default), so coupling errors always fail safe.
    """
    try:
        if not loop_coupling_enabled():
            return None
        preset = preset_name()
        if not preset:
            return None
        v = PRESETS[preset].get(LOOP_MODE_ENV, "")
        return v if v in ("reactive", "proactive") else None
    except Exception:
        return None


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
        "PACOMIND_EXECUTOR_ENABLED": (("true", "false"), "false"),
        "PACOMIND_COGNITION_ENABLED": (("true", "false"), "false"),
        "PACOMIND_INTROSPECT_ENABLED": (("true", "false"), "false"),
        "PACOMIND_THINKING_MODE": (("off", "shadow", "live"), "off"),
        "PACOMIND_PROJECTS_MODE": (("off", "shadow", "live"), "shadow"),
        "PACOMIND_BELIEFS_MODE": (("off", "shadow", "live"), "shadow"),
        "PACOMIND_WORLD_POPULATE_MODE": (("off", "shadow", "live"), "shadow"),
        "PACOMIND_WORLD_LLM_EXTRACT": (("off", "shadow", "live"), "off"),
        "PACOMIND_SKILLS_DISTILL": (("off", "shadow", "live"), "shadow"),
        "PACOMIND_ESCALATION_MINING": (("off", "shadow", "live"), "shadow"),
        "PACOMIND_CONNECTORS_MODE": (("off", "shadow", "live"), "off"),
        "PACOMIND_WORKERS_MODE": (("off", "shadow", "live"), "shadow"),
        "PACOMIND_DIRECTED_MODE": (("off", "dry_run", "live"), "dry_run"),
        "PACOMIND_SANDBOX_MODE": (("off", "dry_run", "live"), "off"),
        "PACOMIND_EXPECTATIONS": (("off", "on", "shadow", "live"), "off"),
        "PACOMIND_WORKSPACE": (("off", "shadow", "live"), "off"),
    }
    for env_name, (valid, fallback) in domains.items():
        if valid == ("true", "false"):
            out[env_name] = str(resolve_bool(env_name, fallback == "true")).lower()
        else:
            out[env_name] = resolve(env_name, valid, fallback)
    # Loop mode is coupling-aware (H4.1): the preset only supplies it while
    # PACOMIND_PRESET_LOOP_COUPLING is on; explicit env always wins. The
    # posture endpoint overrides this with the RUNNING loop's mode when a
    # loop exists, so this is the boot-time resolution view.
    out[COUPLING_ENV] = "on" if loop_coupling_enabled() else "off"
    raw = (os.environ.get(LOOP_MODE_ENV) or "").strip().lower()
    if raw in ("reactive", "proactive"):
        out[LOOP_MODE_ENV] = raw
    else:
        out[LOOP_MODE_ENV] = coupled_loop_mode() or "reactive"
    return out
