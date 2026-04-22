"""System keyring secrets backend (macOS Keychain, Linux SecretService, Windows Credential Manager)."""

from __future__ import annotations

import logging

from colony_sidecar.secrets.backends.base import SecretsBackend
from colony_sidecar.secrets.types import SecretType, ALL_SECRET_KEYS

logger = logging.getLogger(__name__)

_SERVICE = "colony-ai"


class KeyringBackend(SecretsBackend):
    """System keyring backend using the ``keyring`` library.

    macOS: stores in Keychain.
    Linux: uses SecretService (GNOME Keyring or KWallet).
    Windows: uses Windows Credential Manager.

    Requires: pip install keyring  (or colony[keyring] optional dep)
    """

    name = "keyring"

    def is_available(self) -> bool:
        try:
            import keyring
            import keyring.errors
        except ImportError:
            return False
        try:
            # Probe with a read to verify the backend is actually usable.
            # get_keyring() alone doesn't raise on headless systems; a real
            # read/write attempt is required to confirm a backend exists.
            keyring.get_password(_SERVICE, "_colony_probe_")
            return True
        except keyring.errors.NoKeyringError:
            return False
        except Exception:
            return False

    def get(self, key: str, default: str | None = None) -> str | None:
        try:
            import keyring
            value = keyring.get_password(_SERVICE, key)
            return value if value is not None else default
        except Exception:
            return default

    def set(self, key: str, value: str, *, secret_type: SecretType | None = None) -> None:
        import keyring
        keyring.set_password(_SERVICE, key, value)

    def delete(self, key: str) -> None:
        try:
            import keyring
            import keyring.errors
            keyring.delete_password(_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass  # Key didn't exist — idempotent delete
        except Exception as exc:
            logger.warning("KeyringBackend: failed to delete secret %r: %s", key, exc)

    def list(self) -> list[str]:
        """Return all configured keys by probing ALL_SECRET_KEYS.

        The keyring library does not support listing all keys for a service,
        so we iterate the known key set and check which ones are set.
        """
        return [k for k in sorted(ALL_SECRET_KEYS) if self.get(k) is not None]
