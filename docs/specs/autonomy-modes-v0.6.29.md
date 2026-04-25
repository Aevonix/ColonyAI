# Autonomy Modes Spec — v0.6.29

## Goal

Provide two autonomy modes for Colony:

1. **Reactive Mode (default)** — No timer-based execution. On-demand checking via skill/API.
2. **Proactive Mode (opt-in)** — Timer-based autonomy loop with quiet hours support.

This prevents unwanted token burn for users who prefer explicit control.

---

## Mode Comparison

| Aspect | Reactive (Default) | Proactive (Opt-in) |
|--------|-------------------|-------------------|
| Timer | None | Configurable (default 5 min) |
| Initiative generation | On-demand only | Automatic |
| Token burn | User-controlled | Timer-based |
| Quiet hours | N/A | Configurable with timezone |
| API endpoint | `/autonomy/cycle` (manual) | `/autonomy/cycle` (auto + manual) |
| Skill integration | `colony-check` skill | Optional skill for manual override |

---

## Part 1: Mode Configuration

### File: `autonomy/config.py`

### What Already Exists

```python
# Lines 13-29 — ALREADY HAS:
tick_interval_secs: float = 300.0
initiative_confidence_threshold: float = 0.7
max_actions_per_hour: int = 20
quiet_hours_start: str = "22:00"
quiet_hours_end: str = "07:00"
```

### What Needs to Be Added

```python
# autonomy/config.py — ADD at top of file:

from enum import Enum
from zoneinfo import ZoneInfo


class AutonomyMode(str, Enum):
    """Autonomy loop operating mode."""
    REACTIVE = "reactive"    # On-demand only (default)
    PROACTIVE = "proactive"  # Timer-based


# ADD to AutonomyConfig dataclass (after line 15):

@dataclass
class AutonomyConfig:
    """Configuration for the Colony autonomy loop."""

    # ADD THESE FIELDS FIRST (before tick_interval_secs):
    mode: AutonomyMode = AutonomyMode.REACTIVE
    timezone: str = "UTC"  # IANA timezone for quiet hours

    # Existing fields (unchanged):
    tick_interval_secs: float = 300.0
    # ... rest unchanged
```

### Update `from_colony_config()` (lines 48-107)

```python
@classmethod
def from_colony_config(cls, colony_cfg: object) -> "AutonomyConfig":
    """Construct config from a Colony config object or dict."""
    if colony_cfg is None:
        return cls()

    # Extract the autonomy sub-section
    autonomy_section = None
    if isinstance(colony_cfg, dict):
        autonomy_section = colony_cfg.get("autonomy")
    else:
        autonomy_section = getattr(colony_cfg, "autonomy", None)

    if autonomy_section is None:
        return cls()

    def _get(key: str, default):
        if isinstance(autonomy_section, dict):
            return autonomy_section.get(key, default)
        return getattr(autonomy_section, key, default)

    defaults = cls()

    # NEW: Mode and timezone
    mode_str = str(_get("mode", "reactive")).lower()
    mode = AutonomyMode(mode_str) if mode_str in [m.value for m in AutonomyMode] else AutonomyMode.REACTIVE
    timezone = str(_get("timezone", "UTC"))

    # Validate timezone
    try:
        ZoneInfo(timezone)
    except Exception:
        timezone = "UTC"

    return cls(
        mode=mode,                    # NEW
        timezone=timezone,            # NEW
        tick_interval_secs=float(_get("tick_interval_secs", defaults.tick_interval_secs)),
        # ... rest unchanged
    )
```

### Update `from_env()` (lines 110-172)

```python
@classmethod
def from_env(cls) -> "AutonomyConfig":
    """Construct config from environment variables."""
    import logging

    # NEW: Mode selection
    mode_str = os.environ.get("COLONY_AUTONOMY_MODE", "reactive").lower()
    mode = AutonomyMode(mode_str) if mode_str in [m.value for m in AutonomyMode] else AutonomyMode.REACTIVE

    # NEW: Timezone
    timezone = os.environ.get("COLONY_TIMEZONE", "UTC")
    try:
        ZoneInfo(timezone)
    except Exception:
        logging.getLogger(__name__).warning(
            "Invalid COLONY_TIMEZONE '%s', falling back to UTC", timezone
        )
        timezone = "UTC"

    # NEW: Legacy migration
    legacy_tick = os.environ.get("COLONY_AUTONOMY_TICK_INTERVAL_SECS")
    if legacy_tick and not os.environ.get("COLONY_AUTONOMY_MODE"):
        logging.getLogger(__name__).warning(
            "COLONY_AUTONOMY_TICK_INTERVAL_SECS set without COLONY_AUTONOMY_MODE. "
            "Defaulting to PROACTIVE mode to preserve existing behavior. "
            "Add COLONY_AUTONOMY_MODE=proactive to make this explicit."
        )
        mode = AutonomyMode.PROACTIVE

    # ... existing helper functions ...

    defaults = cls()
    return cls(
        mode=mode,
        timezone=timezone,
        tick_interval_secs=_float("COLONY_AUTONOMY_TICK_INTERVAL_SECS", defaults.tick_interval_secs),
        # ... rest unchanged
    )
```

---

## Part 2: Fix Quiet Hours Timezone Bug

### File: `autonomy/loop.py`

### Add Import (line ~20)

```python
# CHANGE:
from datetime import datetime, timezone

# TO:
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
```

### Update `_in_quiet_hours()` (lines 807-825)

```python
def _in_quiet_hours(self) -> bool:
    """Check if current time is within quiet hours (in configured timezone)."""
    try:
        # Use configured timezone, fallback to UTC
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
    except Exception:
        now = datetime.now(timezone.utc)

    try:
        start_h, start_m = map(int, self.config.quiet_hours_start.split(":"))
        end_h, end_m = map(int, self.config.quiet_hours_end.split(":"))
    except (ValueError, AttributeError):
        return False

    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    current_minutes = now.hour * 60 + now.minute

    # Disabled if both are 00:00
    if start_minutes == 0 and end_minutes == 0:
        return False

    # Handle overnight quiet hours (e.g., 22:00 - 07:00)
    if start_minutes > end_minutes:
        return current_minutes >= start_minutes or current_minutes < end_minutes
    return start_minutes <= current_minutes < end_minutes
```

---

## Part 3: Loop Behavior by Mode

### File: `autonomy/loop.py`

### Update `start()` (lines ~138-170)

```python
async def start(self) -> None:
    """Start the autonomy loop. Runs until stop() is called."""
    self._running = True
    self._stop_event.clear()

    # NEW: Check mode
    if self.config.mode == AutonomyMode.REACTIVE:
        logger.info(
            "Autonomy loop started in REACTIVE mode (on-demand only, tz=%s)",
            self.config.timezone,
        )
        # Don't start timer loop — just mark as running
        # Cycles happen via manual /autonomy/cycle calls
        return

    # EXISTING: Proactive mode — start timer loop
    logger.info(
        "Autonomy loop starting in PROACTIVE mode (tick=%.0fs, quiet=%s-%s %s)",
        self.config.tick_interval_secs,
        self.config.quiet_hours_start,
        self.config.quiet_hours_end,
        self.config.timezone,
    )

    self._wake_sub = self.events.subscribe(
        handler=self._on_wake_signal,
        event_types=[Event],
    )

    try:
        while not self._stop_event.is_set():
            await self._tick()
            # ... existing loop code unchanged
```

### Update `status()` (lines ~843-858)

```python
def status(self) -> dict:
    return {
        "running": self._running,
        "mode": self.config.mode.value,           # NEW
        "timezone": self.config.timezone,         # NEW
        "in_quiet_hours": self._in_quiet_hours(),
        "config": {
            "mode": self.config.mode.value,       # NEW
            "timezone": self.config.timezone,     # NEW
            "tick_interval_secs": self.config.tick_interval_secs,
            "initiative_confidence_threshold": self.config.initiative_confidence_threshold,
            "max_actions_per_hour": self.config.max_actions_per_hour,
            "quiet_hours_start": self.config.quiet_hours_start,
            "quiet_hours_end": self.config.quiet_hours_end,
        },
        "stats": self.stats.as_dict(),
    }
```

---

## Part 4: Schema Update

### File: `api/schemas/host.py`

### Update `AutonomyStatusResponse` (lines 797-809)

```python
class AutonomyStatusResponse(BaseModel):
    running: bool = False
    mode: str = "reactive"           # NEW
    timezone: str = "UTC"            # NEW
    in_quiet_hours: bool = False
    ticks: int = 0
    events_processed: int = 0
    goals_checked: int = 0
    initiatives_generated: int = 0
    actions_executed: int = 0
    errors: int = 0
    config: Optional[Dict[str, Any]] = None
```

---

## Part 5: API Router Update

### File: `api/routers/host.py`

### Update `autonomy_status()` (lines 3144-3161)

```python
@router.get("/autonomy/status", response_model=AutonomyStatusResponse)
async def autonomy_status() -> AutonomyStatusResponse:
    if _autonomy_loop is None:
        return AutonomyStatusResponse()
    try:
        s = _autonomy_loop.status()
        return AutonomyStatusResponse(
            running=s.get("running", False),
            mode=s.get("mode", "reactive"),              # NEW
            timezone=s.get("timezone", "UTC"),           # NEW
            in_quiet_hours=s.get("in_quiet_hours", False),
            ticks=s.get("stats", {}).get("ticks", 0),
            events_processed=s.get("stats", {}).get("events_processed", 0),
            goals_checked=s.get("stats", {}).get("goals_checked", 0),
            initiatives_generated=s.get("stats", {}).get("initiatives_generated", 0),
            actions_executed=s.get("stats", {}).get("actions_executed", 0),
            errors=s.get("stats", {}).get("errors", 0),
            config=s.get("config"),
        )
    except Exception as exc:
        logger.warning("autonomy_status failed: %s", exc)
        return AutonomyStatusResponse()
```

---

## Part 6: OpenClaw Skill for On-Demand Checking

### File: `harness_integration/skills.py`

### Add to file (after `COLONY_DIAGNOSTIC_SKILL`)

```python
COLONY_CHECK_SKILL = """---
name: colony-check
description: "On-demand Colony initiative checking. Use when user asks about blocked goals, neglected contacts, or pending initiatives."
---

# Colony Check Skill

Check Colony for blocked goals, neglected contacts, and pending initiatives on-demand.

## When to Use

✅ **USE when:**
- User asks "are there any blocked goals?"
- User asks "what needs attention?"
- Checking during heartbeats (add to HEARTBEAT.md)
- Surfacing potential actions without full autonomy

❌ **DON'T use when:**
- Colony sidecar is not running
- User hasn't asked about goals/initiatives

## Commands

### Trigger Autonomy Cycle

```bash
curl -X POST -H "Authorization: Bearer colony" \\
  "http://localhost:7777/v1/host/autonomy/cycle"
```

Returns:
```json
{
  "completed": true,
  "result": {
    "running": true,
    "mode": "reactive",
    "initiatives_generated": 2
  }
}
```

### Get Blocked Goals

```bash
curl -H "Authorization: Bearer colony" \\
  "http://localhost:7777/v1/host/goals?status=blocked"
```

### Get Pending Commitments

```bash
curl -H "Authorization: Bearer colony" \\
  "http://localhost:7777/v1/host/commitments?status=pending"
```

## Example Usage

**User:** "Is there anything blocked?"

**Agent:**
1. Calls `/autonomy/cycle`
2. Receives: `initiatives_generated: 1`
3. Responds: "You have 1 blocked goal. Want me to help unblock it?"

## Heartbeat Integration

Add to `HEARTBEAT.md`:

```markdown
# Heartbeat Checks
- [ ] Check Colony for blocked goals (if Colony sidecar configured)
```
"""


def write_colony_check_skill(workspace_dir: Path) -> bool:
    """Write colony-check skill to OpenClaw workspace skills directory.

    Args:
        workspace_dir: OpenClaw workspace directory

    Returns:
        True if written successfully, False otherwise
    """
    skill_dir = workspace_dir / "skills" / "colony-check"
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(COLONY_CHECK_SKILL)
        return True
    except Exception:
        return False
```

### File: `setup.py`

### Update OpenClaw plugin setup (around line 456)

```python
# CHANGE:
from colony_sidecar.harness_integration import write_colony_context, write_colony_skill

# TO:
from colony_sidecar.harness_integration import (
    write_colony_check_skill,
    write_colony_context,
    write_colony_skill,
)

# ADD after write_colony_skill call (around line 458):
write_colony_check_skill(workspace)
```

---

## Part 7: Environment Variables Summary

### New Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COLONY_AUTONOMY_MODE` | `reactive` | `reactive` or `proactive` |
| `COLONY_TIMEZONE` | `UTC` | IANA timezone for quiet hours |

### Existing Variables (unchanged)

| Variable | Default |
|----------|---------|
| `COLONY_AUTONOMY_TICK_INTERVAL_SECS` | `300` |
| `COLONY_AUTONOMY_QUIET_HOURS_START` | `22:00` |
| `COLONY_AUTONOMY_QUIET_HOURS_END` | `07:00` |
| `COLONY_AUTONOMY_INITIATIVE_CONFIDENCE_THRESHOLD` | `0.7` |

---

## Part 8: Testing Checklist

### Reactive Mode
- [ ] Loop starts without timer
- [ ] `mode` shows "reactive" in `/autonomy/status`
- [ ] `/autonomy/cycle` works on-demand
- [ ] No automatic initiative generation
- [ ] Skill can trigger cycle manually

### Proactive Mode
- [ ] Loop starts with timer
- [ ] `mode` shows "proactive" in `/autonomy/status`
- [ ] Quiet hours work in configured timezone (not UTC)
- [ ] Initiatives generated automatically
- [ ] Manual cycle still works

### Timezone Fix
- [ ] Set `COLONY_TIMEZONE=America/El_Salvador`
- [ ] Set `COLONY_AUTONOMY_QUIET_HOURS_START=22:00`
- [ ] Verify `in_quiet_hours=true` at 22:00 local time
- [ ] Verify `in_quiet_hours=false` at 21:59 local time

### Migration
- [ ] No mode + no tick interval → reactive
- [ ] No mode + tick interval set → proactive + warning
- [ ] Explicit mode → use that mode

### API Response
- [ ] `/autonomy/status` returns `mode` field
- [ ] `/autonomy/status` returns `timezone` field
- [ ] `config` dict includes `mode` and `timezone`

---

## Summary

| Feature | Implementation |
|---------|---------------|
| **Default mode** | Reactive (no timer, no token burn) |
| **Opt-in proactive** | `COLONY_AUTONOMY_MODE=proactive` |
| **Timezone support** | `COLONY_TIMEZONE=America/El_Salvador` |
| **Quiet hours fix** | Uses configured timezone (BUG FIX) |
| **On-demand checking** | `colony-check` skill + `/autonomy/cycle` |
| **Status response** | Includes `mode` + `timezone` |

### Files Modified

| File | Lines Changed |
|------|---------------|
| `autonomy/config.py` | ~40 lines (enum + 2 methods) |
| `autonomy/loop.py` | ~30 lines (import + start + status + _in_quiet_hours) |
| `api/schemas/host.py` | 2 lines (new fields) |
| `api/routers/host.py` | 5 lines (return new fields) |
| `harness_integration/skills.py` | ~60 lines (new skill) |
| `setup.py` | 3 lines (import + call) |

### Total: ~140 lines across 6 files
