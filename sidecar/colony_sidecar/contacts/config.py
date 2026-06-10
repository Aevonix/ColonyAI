"""Colony Contacts — configuration."""

from __future__ import annotations

import os
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

    @classmethod
    def from_env(cls) -> "ContactsConfig":
        """Build config from the environment, persisting to the state dir.

        The bare default of ":memory:" exists for tests; a production
        sidecar must survive restarts (the IdentityResolver treats the
        contact store as the source of truth for the owner), so the
        server path resolves COLONY_CONTACTS_DB or falls back to
        ``$COLONY_STATE_DIR/colony-contacts.db``.
        """
        path = os.environ.get("COLONY_CONTACTS_DB")
        if not path:
            state_dir = os.environ.get("COLONY_STATE_DIR", ".")
            path = os.path.join(state_dir, "colony-contacts.db")
        return cls(sqlite_path=path)
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
