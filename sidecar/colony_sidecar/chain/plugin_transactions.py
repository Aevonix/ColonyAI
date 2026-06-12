"""Plugin security attestation transaction types and chain state for ColonyChain.

Defines the plugin attestation transaction set:
  - PluginPublishPayload
  - PluginAttestationPayload
  - PluginFlagPayload
  - PluginChainRecord (on-chain registry entry)

These extend the existing ColonyChain transaction set.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ScanResult(str, Enum):
    SAFE = "SAFE"
    FLAGGED = "FLAGGED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class FindingSeverity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingCategory(str, Enum):
    DANGEROUS_IMPORT = "dangerous_import"
    EXEC_DETECTED = "exec_detected"
    NETWORK_ACCESS = "network_access"
    FS_WRITE = "fs_write"
    SUBPROCESS = "subprocess"
    OBFUSCATION = "obfuscation"
    KNOWN_CVE = "known_cve"
    BEHAVIOR_ANOMALY = "behavior_anomaly"


class PluginChainStatus(str, Enum):
    PENDING_SCAN = "PENDING_SCAN"
    SAFE = "SAFE"
    FLAGGED = "FLAGGED"
    QUARANTINED = "QUARANTINED"
    DEPRECATED = "DEPRECATED"


# ---------------------------------------------------------------------------
# Supporting data classes
# ---------------------------------------------------------------------------

@dataclass
class ScanFinding:
    """A single finding from a scan stage."""
    finding_id: str = field(default_factory=lambda: str(uuid4()))
    stage: str = ""                 # static_ast | dependency_check | dynamic_analysis
    severity: FindingSeverity = FindingSeverity.INFO
    category: FindingCategory = FindingCategory.DANGEROUS_IMPORT
    description: str = ""
    location: str = ""
    evidence_snippet: str = ""
    cve_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "stage": self.stage,
            "severity": self.severity.value,
            "category": self.category.value,
            "description": self.description,
            "location": self.location,
            "evidence_snippet": self.evidence_snippet,
            "cve_id": self.cve_id,
        }


@dataclass
class PluginPermissionsPayload:
    """Permissions block embedded in plugin_publish payload."""
    allowed_domains: List[str] = field(default_factory=list)
    allowed_read_paths: List[str] = field(default_factory=list)
    allowed_write_paths: List[str] = field(default_factory=list)
    allowed_env_vars: List[str] = field(default_factory=list)
    max_memory_mb: int = 256
    max_duration_secs: int = 30

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_domains": self.allowed_domains,
            "allowed_read_paths": self.allowed_read_paths,
            "allowed_write_paths": self.allowed_write_paths,
            "allowed_env_vars": self.allowed_env_vars,
            "max_memory_mb": self.max_memory_mb,
            "max_duration_secs": self.max_duration_secs,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PluginPermissionsPayload":
        return cls(
            allowed_domains=d.get("allowed_domains", []),
            allowed_read_paths=d.get("allowed_read_paths", []),
            allowed_write_paths=d.get("allowed_write_paths", []),
            allowed_env_vars=d.get("allowed_env_vars", []),
            max_memory_mb=d.get("max_memory_mb", 256),
            max_duration_secs=d.get("max_duration_secs", 30),
        )


# ---------------------------------------------------------------------------
# Transaction payload builders
# ---------------------------------------------------------------------------

@dataclass
class PluginPublishPayload:
    """Payload for a plugin_publish chain transaction.

    The publisher_signature field signs the canonical_signing_dict() result.
    """
    name: str
    version: str
    plugin_hash: str                # SHA-256 hex of archive
    publisher_colony_id: str
    description: str
    capabilities: List[str]
    permissions: PluginPermissionsPayload
    dependencies: List[str]
    publisher_signature: str = ""   # filled by caller before submission
    min_colony_version: str = "0.1.0"
    tags: List[str] = field(default_factory=list)
    source_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def canonical_signing_dict(self) -> Dict[str, Any]:
        """Returns the dict that publisher_signature must cover.

        Sorted keys, no whitespace when serialised to JSON.
        """
        return {
            "capabilities": sorted(self.capabilities),
            "dependencies": sorted(self.dependencies),
            "name": self.name,
            "plugin_hash": self.plugin_hash,
            "publisher_colony_id": self.publisher_colony_id,
            "version": self.version,
        }

    def canonical_signing_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_signing_dict(), sort_keys=True, separators=(",", ":")
        ).encode()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "plugin_hash": self.plugin_hash,
            "publisher_colony_id": self.publisher_colony_id,
            "publisher_signature": self.publisher_signature,
            "description": self.description,
            "capabilities": self.capabilities,
            "permissions": self.permissions.to_dict(),
            "dependencies": self.dependencies,
            "min_colony_version": self.min_colony_version,
            "tags": self.tags,
            "source_url": self.source_url,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PluginPublishPayload":
        perms_raw = d.get("permissions", {})
        return cls(
            name=d["name"],
            version=d["version"],
            plugin_hash=d["plugin_hash"],
            publisher_colony_id=d["publisher_colony_id"],
            description=d.get("description", ""),
            capabilities=d.get("capabilities", []),
            permissions=PluginPermissionsPayload.from_dict(perms_raw),
            dependencies=d.get("dependencies", []),
            publisher_signature=d.get("publisher_signature", ""),
            min_colony_version=d.get("min_colony_version", "0.1.0"),
            tags=d.get("tags", []),
            source_url=d.get("source_url"),
            metadata=d.get("metadata", {}),
        )


@dataclass
class PluginAttestationPayload:
    """Payload for a plugin_attestation chain transaction."""
    scanner_colony_id: str
    plugin_hash: str
    plugin_name: str
    plugin_version: str
    scan_result: ScanResult
    findings: List[ScanFinding]
    scanner_version: str
    scan_policy_version: str
    scanned_at: datetime
    container_image: str
    stages_completed: List[str]
    scanner_signature: str = ""     # filled by caller before submission

    @property
    def findings_hash(self) -> str:
        """SHA-256 of canonical JSON of findings list."""
        findings_json = json.dumps(
            [f.to_dict() for f in self.findings],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(findings_json.encode()).hexdigest()

    def canonical_signing_dict(self) -> Dict[str, Any]:
        return {
            "findings_hash": self.findings_hash,
            "plugin_hash": self.plugin_hash,
            "scan_result": self.scan_result.value,
            "scanned_at": self.scanned_at.isoformat(),
            "scanner_colony_id": self.scanner_colony_id,
        }

    def canonical_signing_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_signing_dict(), sort_keys=True, separators=(",", ":")
        ).encode()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scanner_colony_id": self.scanner_colony_id,
            "plugin_hash": self.plugin_hash,
            "plugin_name": self.plugin_name,
            "plugin_version": self.plugin_version,
            "scan_result": self.scan_result.value,
            "findings": [f.to_dict() for f in self.findings],
            "findings_hash": self.findings_hash,
            "scanner_version": self.scanner_version,
            "scan_policy_version": self.scan_policy_version,
            "scanned_at": self.scanned_at.isoformat(),
            "container_image": self.container_image,
            "stages_completed": self.stages_completed,
            "scanner_signature": self.scanner_signature,
        }


@dataclass
class PluginFlagPayload:
    """Payload for a plugin_flag chain transaction."""
    reporter_colony_id: str
    plugin_hash: str
    plugin_name: str
    plugin_version: str
    vulnerability_description: str
    severity: FindingSeverity
    evidence_hash: str              # SHA-256 of evidence payload
    reporter_signature: str = ""    # filled by caller before submission
    evidence_url: Optional[str] = None
    cve_id: Optional[str] = None
    affects_versions: List[str] = field(default_factory=list)

    def canonical_signing_dict(self) -> Dict[str, Any]:
        return {
            "evidence_hash": self.evidence_hash,
            "plugin_hash": self.plugin_hash,
            "reporter_colony_id": self.reporter_colony_id,
            "severity": self.severity.value,
            "vulnerability_description": self.vulnerability_description,
        }

    def canonical_signing_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_signing_dict(), sort_keys=True, separators=(",", ":")
        ).encode()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reporter_colony_id": self.reporter_colony_id,
            "plugin_hash": self.plugin_hash,
            "plugin_name": self.plugin_name,
            "plugin_version": self.plugin_version,
            "vulnerability_description": self.vulnerability_description,
            "severity": self.severity.value,
            "evidence_hash": self.evidence_hash,
            "reporter_signature": self.reporter_signature,
            "evidence_url": self.evidence_url,
            "cve_id": self.cve_id,
            "affects_versions": self.affects_versions,
        }


# ---------------------------------------------------------------------------
# Chain state record
# ---------------------------------------------------------------------------

@dataclass
class PluginChainRecord:
    """The authoritative on-chain state for a plugin version.

    Maintained by ChainStateMachine as part of plugin_registry.
    """
    plugin_hash: str
    name: str
    version: str
    publisher_id: str
    published_at_height: int
    status: PluginChainStatus = PluginChainStatus.PENDING_SCAN
    attestations: List[Dict[str, Any]] = field(default_factory=list)
    flags: List[Dict[str, Any]] = field(default_factory=list)
    safe_consensus: float = 0.0
    flag_count_trusted: int = 0
    superseded_by: Optional[str] = None   # plugin_hash of patched version
    source_url: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)

    def recompute_consensus(
        self,
        scanner_weights: Dict[str, float],
    ) -> float:
        """Recompute safe_consensus from current attestations.

        Args:
            scanner_weights: Map of scanner_colony_id -> weight (float).
                Sentinels typically receive 2.0, trusted non-Sentinels 1.0,
                unknown colonies 0.25.

        Returns:
            Updated safe_consensus score (0.0–1.0).
        """
        total = 0.0
        safe = 0.0
        for att in self.attestations:
            w = scanner_weights.get(att["scanner_colony_id"], 0.25)
            total += w
            if att["scan_result"] == ScanResult.SAFE.value:
                safe += w
        self.safe_consensus = (safe / total) if total > 0 else 0.0
        return self.safe_consensus

    def has_sentinel_safe(self, sentinel_ids: frozenset) -> bool:
        """Return True if at least one Sentinel has attested SAFE."""
        for att in self.attestations:
            if (
                att["scanner_colony_id"] in sentinel_ids
                and att["scan_result"] == ScanResult.SAFE.value
            ):
                return True
        return False

    def has_flagged_from_trusted(self, trusted_ids: frozenset) -> bool:
        """Return True if any trusted colony has attested FLAGGED."""
        for att in self.attestations:
            if (
                att["scanner_colony_id"] in trusted_ids
                and att["scan_result"] == ScanResult.FLAGGED.value
            ):
                return True
        return False
