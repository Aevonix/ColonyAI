"""Colony Local Key Manager — PEM-based Ed25519 signing for Phase 1/2.

Provides the same interface as ColonyKeyManager (sign, public_key_hex, rotate)
but stores the key as a simple PEM file. Designed to be swapped out for the
Shamir-backed ColonyKeyManager when Phase 3/4/5 ship.

Key storage:
    {state_dir}/colony-keys/
        private.pem    — Ed25519 private key (optionally encrypted)
        public.pem     — Ed25519 public key (always readable)

Migration path:
    Phase 1/2: LocalKeyManager (PEM, optional passphrase)
    Phase 3:   LocalKeyManager with passphrase (colony key set-passphrase)
    Phase 4/5: ColonyKeyManager (Shamir-split, colony key migrate-to-shamir)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class LocalKeyManager:
    """PEM-based Ed25519 key manager for single-node Colony deployments."""

    def __init__(
        self,
        keys_dir: str | Path,
        colony_id: str,
        passphrase: Optional[bytes] = None,
    ) -> None:
        self._keys_dir = Path(keys_dir)
        self.colony_id = colony_id
        self._passphrase = passphrase
        self._keys_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface (matches ColonyKeyManager)
    # ------------------------------------------------------------------

    def sign(self, payload: bytes) -> str:
        """Sign payload bytes, returning hex-encoded Ed25519 signature."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = self._load_private_key()
        signature = private_key.sign(payload)
        return signature.hex()

    def public_key_hex(self) -> str:
        """Return the hex-encoded raw Ed25519 public key."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = self._load_private_key()
        pub = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return pub.hex()

    def public_key_pem(self) -> str:
        """Return the PEM-encoded public key."""
        pub_path = self._keys_dir / "public.pem"
        if pub_path.exists():
            return pub_path.read_text()
        # Generate from private key
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = self._load_private_key()
        pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pub_path.write_bytes(pem)
        return pem.decode()

    def rotate(self, new_passphrase: Optional[bytes] = None) -> None:
        """Generate a new Ed25519 keypair, replacing the existing one.

        The colony_id does NOT change — only the signing key rotates.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # Backup old key
        priv_path = self._keys_dir / "private.pem"
        if priv_path.exists():
            backup = self._keys_dir / "private.pem.backup"
            backup.write_bytes(priv_path.read_bytes())

        # Generate new keypair
        private_key = Ed25519PrivateKey.generate()
        self._save_keypair(private_key, new_passphrase or self._passphrase)
        logger.info("Key rotated for colony %s", self.colony_id)

    def set_passphrase(self, new_passphrase: bytes) -> None:
        """Re-encrypt the private key with a new passphrase."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = self._load_private_key()
        self._save_keypair(private_key, new_passphrase)
        self._passphrase = new_passphrase
        logger.info("Passphrase updated for colony %s", self.colony_id)

    @property
    def has_keys(self) -> bool:
        """Check if keypair exists on disk."""
        return (self._keys_dir / "private.pem").exists()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_private_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        priv_path = self._keys_dir / "private.pem"
        if not priv_path.exists():
            raise FileNotFoundError(
                f"No private key found at {priv_path}. Run 'colony init' or 'colony key generate'."
            )

        pem_data = priv_path.read_bytes()
        try:
            return serialization.load_pem_private_key(
                pem_data,
                password=self._passphrase,
            )
        except (ValueError, TypeError):
            # Wrong passphrase or unencrypted key with passphrase given
            if self._passphrase:
                raise ValueError("Wrong passphrase for private key") from None
            # Try without passphrase
            return serialization.load_pem_private_key(pem_data, password=None)

    def _save_keypair(self, private_key, passphrase: Optional[bytes] = None) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # Save private key
        encryption: Any = serialization.NoEncryption()
        if passphrase:
            encryption = serialization.BestAvailableEncryption(passphrase)

        priv_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=encryption,
        )
        (self._keys_dir / "private.pem").write_bytes(priv_pem)

        # Save public key
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        (self._keys_dir / "public.pem").write_bytes(pub_pem)

        # Restrict permissions
        os.chmod(self._keys_dir / "private.pem", 0o600)

    @classmethod
    def generate(
        cls,
        keys_dir: str | Path,
        colony_id: str,
        passphrase: Optional[bytes] = None,
    ) -> "LocalKeyManager":
        """Generate a new Ed25519 keypair and return a LocalKeyManager."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        mgr = cls(keys_dir=keys_dir, colony_id=colony_id, passphrase=passphrase)
        private_key = Ed25519PrivateKey.generate()
        mgr._save_keypair(private_key, passphrase)
        logger.info("Generated new Ed25519 keypair for colony %s", colony_id)
        return mgr


# Fix type annotation for Python 3.11 compat
from typing import Any
