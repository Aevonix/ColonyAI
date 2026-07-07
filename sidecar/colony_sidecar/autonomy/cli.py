"""CLI entry point for Colony autonomy loop management.

Usage:
    colony autonomy status     # live loop stats from the running sidecar
    colony autonomy cycle      # wake the loop / run one cycle now

The loop itself lives inside the sidecar process and starts with it — there
is no standalone "start" mode (running a second loop against the same state
would double-fire every phase). `status` and `cycle` talk to the running
sidecar over HTTP, resolved the same way the doctor does (COLONY_URL /
COLONY_SIDECAR_HOST/PORT, COLONY_API_KEY).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def _request(path: str, method: str = "GET") -> dict:
    from colony_sidecar.doctor import default_colony_url

    url = default_colony_url().rstrip("/") + path
    key = os.environ.get("COLONY_API_KEY", "")
    req = urllib.request.Request(
        url,
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _print_config_defaults() -> None:
    from colony_sidecar.autonomy.config import AutonomyConfig

    cfg = AutonomyConfig.from_env()
    print(f"  tick_interval_secs:              {cfg.tick_interval_secs}")
    print(f"  initiative_confidence_threshold: {cfg.initiative_confidence_threshold}")
    print(f"  max_actions_per_hour:            {cfg.max_actions_per_hour}")
    print(f"  quiet_hours:                     {cfg.quiet_hours_start} – {cfg.quiet_hours_end}")
    print(f"  anomaly_severity_threshold:      {cfg.anomaly_severity_threshold}")
    print(f"  prediction_confidence_threshold: {cfg.prediction_confidence_threshold}")
    print(f"  goal_stale_threshold_hours:      {cfg.goal_stale_threshold_hours}")


def run_autonomy_status(loop_instance=None) -> int:
    """Print live autonomy status.

    A provided loop_instance (in-process embedding) is used directly;
    otherwise the running sidecar is queried over HTTP. Only when no sidecar
    is reachable do we fall back to printing config defaults, clearly
    labelled as such.
    """
    if loop_instance is not None:
        print(json.dumps(loop_instance.status(), indent=2))
        return 0

    try:
        status = _request("/v1/host/autonomy/status")
        print(json.dumps(status, indent=2))
        return 0
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Autonomy Loop — sidecar unreachable ({exc}); env config defaults:")
        _print_config_defaults()
        return 1


def run_autonomy_cycle() -> int:
    """Wake the running loop (proactive mode) or run one cycle (reactive)."""
    try:
        out = _request("/v1/host/autonomy/cycle", method="POST")
        print(json.dumps(out, indent=2))
        return 0
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"cycle failed: sidecar unreachable ({exc})", file=sys.stderr)
        return 1


def run_autonomy_command(args: list) -> int:
    """Dispatch 'colony autonomy <subcommand>'."""
    if not args:
        print("Usage: colony autonomy <status|cycle>", file=sys.stderr)
        return 1

    sub = args[0]

    if sub == "status":
        return run_autonomy_status()

    if sub == "cycle":
        return run_autonomy_cycle()

    if sub == "start":
        # The loop starts with the sidecar; a second standalone loop against
        # the same state would double-fire every phase.
        print("The autonomy loop runs inside the sidecar and starts with it "
              "(colony serve). Use 'colony autonomy status' to inspect it or "
              "'colony autonomy cycle' to wake it.", file=sys.stderr)
        return 1

    print(f"Unknown autonomy subcommand: {sub!r}", file=sys.stderr)
    return 1
