"""CLI entry point for Colony autonomy loop management.

Usage:
    colony autonomy status     # show loop stats and config
    colony autonomy start      # start the autonomy loop (blocking)
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional


def run_autonomy_status(loop_instance=None) -> int:
    """Print current autonomy loop status.

    If loop_instance is provided, prints live stats.
    Otherwise prints config defaults from environment.
    """
    from colony_sidecar.autonomy.config import AutonomyConfig

    if loop_instance is not None:
        status = loop_instance.status()
        print(json.dumps(status, indent=2))
        return 0

    # No live loop — show config from environment
    cfg = AutonomyConfig.from_env()
    print("Autonomy Loop — not running (no live instance provided)")
    print(f"  tick_interval_secs:              {cfg.tick_interval_secs}")
    print(f"  initiative_confidence_threshold: {cfg.initiative_confidence_threshold}")
    print(f"  max_actions_per_hour:            {cfg.max_actions_per_hour}")
    print(f"  quiet_hours:                     {cfg.quiet_hours_start} – {cfg.quiet_hours_end}")
    print(f"  anomaly_severity_threshold:      {cfg.anomaly_severity_threshold}")
    print(f"  prediction_confidence_threshold: {cfg.prediction_confidence_threshold}")
    print(f"  goal_stale_threshold_hours:      {cfg.goal_stale_threshold_hours}")
    return 0


def run_autonomy_command(args: list) -> int:
    """Dispatch 'colony autonomy <subcommand>'."""
    if not args:
        print("Usage: colony autonomy <status|start>", file=sys.stderr)
        return 1

    sub = args[0]

    if sub == "status":
        return run_autonomy_status()

    if sub == "start":
        print("colony autonomy start: wire AutonomyLoop at application startup.", file=sys.stderr)
        print("See colony/autonomy/loop.py for integration instructions.", file=sys.stderr)
        return 1

    print(f"Unknown autonomy subcommand: {sub!r}", file=sys.stderr)
    return 1
