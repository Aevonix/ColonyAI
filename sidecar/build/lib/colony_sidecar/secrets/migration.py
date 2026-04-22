"""Auto-migration utilities for moving secrets between backends."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from colony_sidecar.secrets.backends.base import SecretsBackend
from colony_sidecar.secrets.types import infer_secret_type

logger = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    """Result summary from a secrets migration run."""

    migrated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    would_migrate: list[str] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return len(self.migrated)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def summary(self) -> str:
        parts = []
        if self.would_migrate:
            parts.append(f"Would migrate {len(self.would_migrate)} secrets (dry run)")
        if self.migrated:
            parts.append(f"Migrated {len(self.migrated)} secrets")
        if self.skipped:
            parts.append(f"Skipped {len(self.skipped)} (no value)")
        if self.failed:
            parts.append(f"Failed {len(self.failed)}")
        return ", ".join(parts) if parts else "Nothing to migrate"


class SecretsMigrator:
    """Detect existing secrets and migrate them between backends.

    Called by the onboarding wizard when a user changes their secrets backend,
    and optionally on first startup after a V2 upgrade if the user has a .env
    file but has selected 1Password or keyring as their backend.
    """

    def __init__(self, source: SecretsBackend, target: SecretsBackend) -> None:
        self._source = source
        self._target = target

    def preview(self) -> list[str]:
        """Return list of secret keys that would be migrated."""
        return self._source.list()

    def migrate(
        self,
        keys: list[str] | None = None,
        *,
        dry_run: bool = False,
        delete_from_source: bool = False,
    ) -> MigrationResult:
        """Migrate secrets from source to target backend.

        Args:
            keys: Specific keys to migrate.  None = migrate all from source.
            dry_run: If True, only report what would be migrated without writing.
            delete_from_source: If True, delete from source after verifying target.

        Returns:
            MigrationResult with counts of migrated, skipped, and failed keys.
        """
        to_migrate = keys if keys is not None else self._source.list()
        result = MigrationResult()

        for key in to_migrate:
            value = self._source.get(key)
            if value is None:
                result.skipped.append(key)
                continue
            if dry_run:
                result.would_migrate.append(key)
                continue
            try:
                secret_type = infer_secret_type(key)
                self._target.set(key, value, secret_type=secret_type)
                # Verify the write succeeded
                if self._target.get(key) != value:
                    raise RuntimeError("Read-back verification failed")
                result.migrated.append(key)
                if delete_from_source:
                    self._source.delete(key)
            except Exception as exc:
                logger.warning("Failed to migrate secret %s: %s", key, exc)
                result.failed.append((key, str(exc)))

        return result
