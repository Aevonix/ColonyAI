"""bootstrap_self_check skill entry point.

Registered in the Colony skill registry as 'bootstrap-self-check'.
Runs the 16-point self-check matrix and returns a verification report.
"""

from __future__ import annotations

from typing import Any, Optional


async def run(colony_id: Optional[str] = None, mode: str = "full") -> dict:
    """Run the Colony Identity Bootstrap self-check.

    Args:
        colony_id: Override colony_id (default: read from running instance).
        mode: "full" to run all checks, "quick" to skip slow subsystem checks.

    Returns:
        Dict with verified_systems, failed_systems, anomalies, success.
    """
    from colony_sidecar.identity_bootstrap.runner import IdentityBootstrap

    bootstrap = IdentityBootstrap()
    report = await bootstrap.verify_only()

    return {
        "colony_id": report.colony_id,
        "mode": report.mode,
        "verified_systems": report.verified_systems,
        "failed_systems": report.failed_systems,
        "anomalies": [a.to_dict() for a in report.anomalies],
        "success": report.success,
        "corpus_version": report.corpus_version,
        "started_at": report.started_at,
        "completed_at": report.completed_at,
    }
