"""GateConfig — full configuration for the Response Gate pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GateConfig:
    """Full configuration for the Response Gate pipeline."""

    # Sensitivity
    sensitivity: str = "standard"   # paranoid | standard | relaxed

    # Layer 3
    cross_context_lookback_hours: int = 4

    # Layer 6
    enable_secondary_review: bool = False   # off by default (requires LLM)
    secondary_review_model: str = "claude-haiku-4-5-20251001"

    # Layer 7
    send_delay_seconds: float = 0.0   # default 0 for tests; production uses 2.0

    # Rejection feedback loop
    max_retries: int = 3

    # Context loading
    context_token_limit: int = 80_000

    # Concurrent sessions
    max_concurrent_sessions: int = 20

    # Supervised mode
    require_send_approval: bool = False

    # Owner-shareable patterns (bypass Layer 2)
    owner_shareable_patterns: list = field(default_factory=list)

    # Per-contact overrides
    contact_overrides: list = field(default_factory=list)

    # Custom PII patterns
    custom_pii_patterns: list = field(default_factory=list)

    # Injection ruleset
    injection_ruleset_path: str = "colony/gate/rulesets/injection_v1.yaml"
    ruleset_hot_reload: bool = False

    # Audit
    audit_retention_days: int = 365

    # Workspace
    log_full_deliberation: bool = False
    workspace_memory_ttl_days: int = 90

    def is_owner_shareable(self, value: str, pattern_name: str) -> bool:
        for item in self.owner_shareable_patterns:
            if item.get("pattern_name") == pattern_name and item.get("value") == value:
                return True
        return False

    def get_contact_override(self, contact_id: str) -> Optional[dict]:
        for override in self.contact_overrides:
            if override.get("contact_id") == contact_id:
                return override
        return None
