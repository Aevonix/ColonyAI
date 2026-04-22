"""Colony Identity Bootstrap — report and anomaly models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional


@dataclass
class BootstrapAnomaly:
    """A discrepancy detected during self-check."""
    system: str
    check: str
    expected: str
    actual: str
    severity: Literal["CRITICAL", "WARNING"]

    def to_dict(self) -> dict:
        return {
            "system": self.system,
            "check": self.check,
            "expected": self.expected,
            "actual": self.actual,
            "severity": self.severity,
        }


@dataclass
class BootstrapReport:
    """Summary of a completed bootstrap run."""
    colony_id: str
    mode: Literal["FIRST_BOOT", "REGEN", "VERIFY"]
    started_at: str
    completed_at: str
    seeded_systems: List[str] = field(default_factory=list)
    verified_systems: List[str] = field(default_factory=list)
    failed_systems: List[str] = field(default_factory=list)
    anomalies: List[BootstrapAnomaly] = field(default_factory=list)
    corpus_version: str = "1.0.0"
    success: bool = True

    def to_dict(self) -> dict:
        return {
            "colony_id": self.colony_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "seeded_systems": self.seeded_systems,
            "verified_systems": self.verified_systems,
            "failed_systems": self.failed_systems,
            "anomalies": [a.to_dict() for a in self.anomalies],
            "corpus_version": self.corpus_version,
            "success": self.success,
        }
