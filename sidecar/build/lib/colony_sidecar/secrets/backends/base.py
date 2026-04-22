"""Abstract base class for Colony secrets backends."""

from __future__ import annotations

from abc import ABC, abstractmethod

from colony_sidecar.secrets.types import SecretType


class SecretsBackend(ABC):
    """Abstract secrets storage backend.

    All backends must implement get/set/delete/list and is_available.
    Backends are selected during onboarding and persisted in config.yaml.
    """

    name: str = "abstract"

    @abstractmethod
    def get(self, key: str, default: str | None = None) -> str | None:
        """Retrieve a secret by key. Returns default if not found."""
        ...

    @abstractmethod
    def set(self, key: str, value: str, *, secret_type: SecretType | None = None) -> None:
        """Store a secret."""
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove a secret."""
        ...

    @abstractmethod
    def list(self) -> list[str]:
        """List all known secret keys (not values)."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this backend can be used on the current system."""
        ...
