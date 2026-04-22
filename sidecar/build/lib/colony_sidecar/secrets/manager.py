"""SecretsManager — unified secrets access layer for Colony.

All Colony code MUST use SecretsManager to access secrets.
Direct reads from os.environ or .env files are DEPRECATED in V2
and will be removed in V3.

Usage::

    sm = SecretsManager()
    api_key = sm.get("OPENAI_API_KEY")
    sm.set("OPENAI_API_KEY", "sk-...")
    keys = sm.list()

The backend is loaded from ~/.colony/config.yaml on first instantiation
and cached for the process lifetime.  Override ``COLONY_SECRETS_BACKEND``
env var to ``env``, ``keyring``, or ``1password`` for testing.
"""

from __future__ import annotations

import logging
import os
from colony_sidecar.secrets.backends.base import SecretsBackend
from colony_sidecar.secrets.types import SecretType

logger = logging.getLogger(__name__)

_BACKEND_REGISTRY: dict[str, type[SecretsBackend]] = {}


def _register_backends() -> None:
    """Lazy-load and register all known backends."""
    global _BACKEND_REGISTRY
    if _BACKEND_REGISTRY:
        return
    from colony_sidecar.secrets.backends.env import EnvBackend
    from colony_sidecar.secrets.backends.keyring import KeyringBackend
    from colony_sidecar.secrets.backends.onepassword import OnePasswordBackend

    _BACKEND_REGISTRY = {
        "env": EnvBackend,
        "keyring": KeyringBackend,
        "1password": OnePasswordBackend,
    }


class SecretsManager:
    """Unified secrets access layer for Colony.

    Singleton — one instance per process, backend determined from config.
    Use ``SecretsManager.reset()`` in tests to clear cached state.
    """

    _instance: "SecretsManager | None" = None

    def __new__(cls) -> "SecretsManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._backend = cls._load_backend()
        return cls._instance

    @classmethod
    def _load_backend(cls) -> SecretsBackend:
        _register_backends()

        # Allow override via env var (useful for testing and CI)
        backend_name = os.environ.get("COLONY_SECRETS_BACKEND")

        if not backend_name:
            backend_name = cls._read_config_backend()

        if not backend_name:
            backend_name = "env"

        if backend_name not in _BACKEND_REGISTRY:
            logger.warning(
                "Unknown secrets backend %r — falling back to 'env'", backend_name
            )
            backend_name = "env"

        backend_cls = _BACKEND_REGISTRY[backend_name]
        backend = backend_cls()

        if not backend.is_available():
            logger.warning(
                "Secrets backend %r is not available on this system — "
                "falling back to 'env'. Run `colony secrets status` for details.",
                backend_name,
            )
            backend = _BACKEND_REGISTRY["env"]()

        return backend

    @classmethod
    def _read_config_backend(cls) -> str | None:
        """Read secrets.backend from ~/.colony/config.yaml."""
        try:
            import yaml
            from pathlib import Path

            colony_home = Path(os.environ.get("COLONY_HOME", Path.home() / ".colony"))
            config_path = colony_home / "config.yaml"
            if not config_path.exists():
                return None
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("secrets", {}).get("backend")
        except Exception as exc:
            logger.debug("Could not read secrets backend from config: %s", exc)
            return None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        """Retrieve a secret by key. Returns default if not found."""
        return self._backend.get(key, default)

    def get_required(self, key: str) -> str:
        """Retrieve a secret by key. Raises KeyError if not found."""
        value = self._backend.get(key)
        if value is None:
            raise KeyError(
                f"Required secret '{key}' not found in {self._backend.name} backend. "
                f"Run `colony secrets set {key} <value>` to configure it."
            )
        return value

    def set(self, key: str, value: str, *, secret_type: SecretType | None = None) -> None:
        """Store a secret."""
        self._backend.set(key, value, secret_type=secret_type)

    def delete(self, key: str) -> None:
        """Remove a secret."""
        self._backend.delete(key)

    def list(self) -> list[str]:
        """List all known secret keys (not values)."""
        return self._backend.list()

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def backend(self) -> SecretsBackend:
        return self._backend

    @classmethod
    def reset(cls) -> None:
        """Clear the singleton — for testing only."""
        cls._instance = None

    @classmethod
    def with_backend(cls, backend: SecretsBackend) -> "SecretsManager":
        """Create a SecretsManager instance with a specific backend — for testing."""
        instance = object.__new__(cls)
        instance._backend = backend
        return instance
