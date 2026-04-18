"""Colony Contacts — configuration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContactsEnrichmentConfig:
    enabled: bool = True
    min_update_confidence: float = 0.70
    immutable_confidence_threshold: float = 0.95
    respect_privacy_restricted: bool = True


@dataclass
class ContactsImportConfig:
    normalize_phones: bool = True
    default_phone_region: str = "US"
    max_batch_size: int = 5000


@dataclass
class ContactsExportConfig:
    vcard_include_tier: bool = False
    vcard_include_tags: bool = True


@dataclass
class ContactsConfig:
    enabled: bool = True
    sqlite_path: str = ":memory:"
    default_inbound_tier: str = "unknown"
    unknown_tier_grace_hours: int = 48
    auto_merge_confidence_threshold: float = 0.95
    merge_proposal_threshold: float = 0.60
    soft_delete_retention_days: int = 30
    audit_retention_days: int = 365
    brief_on_tier_change: bool = True
    brief_on_merge_proposals: bool = True
    enrichment: ContactsEnrichmentConfig = field(default_factory=ContactsEnrichmentConfig)
    import_cfg: ContactsImportConfig = field(default_factory=ContactsImportConfig)
    export_cfg: ContactsExportConfig = field(default_factory=ContactsExportConfig)
