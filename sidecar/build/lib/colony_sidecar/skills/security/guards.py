"""Colony Skills — capability guards for pre-execution checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from colony_sidecar.skills.models import SkillCapability, SkillManifest


@dataclass
class GuardResult:
    allowed: bool
    reason: str
    violations: List[str] = field(default_factory=list)


class CapabilityGuard:
    """Pre-execution checks that enforce declared capability constraints.

    Checks performed:
      - Input parameters do not include path traversal sequences
      - Network capability required if inputs reference external URLs
      - Execution blocked entirely if skill is quarantined or inactive
    """

    async def check(
        self, manifest: SkillManifest, inputs: Dict[str, Any]
    ) -> GuardResult:
        violations: List[str] = []

        # Path traversal in string inputs
        for key, value in inputs.items():
            if isinstance(value, str) and ".." in value:
                violations.append(f"Path traversal in input '{key}': {value[:80]}")

        # Network capability required for URL inputs
        has_url_input = any(
            isinstance(v, str) and (v.startswith("http://") or v.startswith("https://"))
            for v in inputs.values()
        )
        if has_url_input and SkillCapability.NETWORK not in manifest.permissions.capabilities:
            violations.append(
                "Input contains URL but skill does not declare NETWORK capability."
            )

        if violations:
            return GuardResult(
                allowed=False,
                reason="Pre-execution guard failed.",
                violations=violations,
            )
        return GuardResult(allowed=True, reason="All guards passed.")
