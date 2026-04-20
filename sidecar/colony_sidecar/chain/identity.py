"""Colony Identity — colony_id management and Genesis trust anchor.

Colony identity is decoupled from the signing keypair:
    colony_id   — permanent UUID, generated once at init, never changes
    keypair     — Ed25519, can rotate without changing colony_id
    public_key  — derived from keypair, published openly for verification

Genesis:
    The first Colony — Marc's Colony — is the trust anchor for the network.
    Its colony_id and public_key are hardcoded so any Colony can recognize it.
    This is like SSH known_hosts or a CA root cert: the public key is safe to
    share (that's what public keys are for), and other Colonies use it to
    verify that a message actually came from Genesis.

    Only one Colony can ever claim Genesis — the one whose colony_id and
    public_key match the hardcoded manifest. No other Colony can impersonate
    it because they don't have the private key.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Genesis manifest — the trust anchor for the entire Colony network
# ---------------------------------------------------------------------------
# This will be populated when Marc's Colony is first initialized.
# The public_key here is safe to hardcode — it's public. The private key
# never leaves Marc's machine.
#
# Format: { colony_id, public_key_ed25519, alias, genesis_version, signed_at }
# ---------------------------------------------------------------------------

_GENESIS_MANIFEST: Optional[dict] = None  # Set after Marc's first init


def set_genesis_manifest(manifest: dict) -> None:
    """Set the Genesis manifest (called after Marc's Colony is initialized)."""
    global _GENESIS_MANIFEST
    _GENESIS_MANIFEST = manifest


def get_genesis_manifest() -> Optional[dict]:
    """Get the Genesis manifest, if configured."""
    return _GENESIS_MANIFEST


def is_genesis(colony_id: str, public_key_hex: str) -> bool:
    """Check if a colony_id + public_key combination matches Genesis.

    Both must match — you can't claim Genesis with just the colony_id
    or just the public key. The private key is required to actually
    sign anything as Genesis, which only Marc has.
    """
    if _GENESIS_MANIFEST is None:
        return False
    return (
        colony_id == _GENESIS_MANIFEST.get("colony_id")
        and public_key_hex == _GENESIS_MANIFEST.get("public_key_ed25519")
    )


def load_genesis_manifest(path: str | Path) -> Optional[dict]:
    """Load a Genesis manifest from a JSON file."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        # Validate required fields
        required = {"colony_id", "public_key_ed25519", "alias", "genesis_version"}
        if not required.issubset(set(data.keys())):
            logger.warning("Genesis manifest missing fields: %s", required - set(data.keys()))
            return None
        set_genesis_manifest(data)
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load Genesis manifest: %s", e)
        return None


# ---------------------------------------------------------------------------
# Colony ID management
# ---------------------------------------------------------------------------

COLONY_ID_FILE = "colony-id"


def get_or_create_colony_id(state_dir: str | Path) -> str:
    """Get the existing colony_id or create a new random UUID.

    The colony_id is stored in {state_dir}/colony-id.
    It is generated once and never changes.
    For disaster recovery, use 'colony backup' / 'colony restore'.
    """
    id_path = Path(state_dir) / COLONY_ID_FILE
    if id_path.exists():
        colony_id = id_path.read_text().strip()
        if colony_id:
            return colony_id

    # Generate new
    colony_id = str(uuid.uuid4())
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text(colony_id)
    logger.info("Generated new colony_id: %s", colony_id)
    return colony_id


def create_genesis_manifest(
    colony_id: str,
    public_key_hex: str,
    output_path: str | Path,
) -> dict:
    """Create a Genesis manifest for Marc's Colony.

    This should only be called once — when Marc's Colony is first initialized.
    The manifest is saved to disk and should be committed to the repo so all
    Colonies can recognize Genesis.
    """
    manifest = {
        "colony_id": colony_id,
        "public_key_ed25519": public_key_hex,
        "alias": "genesis",
        "genesis_version": 1,
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "description": "The original Colony — trust anchor for the network",
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(manifest, indent=2) + "\n")
    logger.info("Genesis manifest created at %s", output_path)
    set_genesis_manifest(manifest)
    return manifest


def create_colony_manifest(
    colony_id: str,
    public_key_hex: str,
    output_path: str | Path,
) -> dict:
    """Create a standard (non-Genesis) colony manifest.

    Every Colony can generate a manifest to share its public identity.
    Other Colonies can load this to establish trust.
    """
    manifest = {
        "colony_id": colony_id,
        "public_key_ed25519": public_key_hex,
        "alias": "",  # User-settable nickname
        "genesis_version": 0,  # 0 = not Genesis
        "signed_at": datetime.now(timezone.utc).isoformat(),
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(manifest, indent=2) + "\n")
    logger.info("Colony manifest created at %s", output_path)
    return manifest


def backup_colony(state_dir: str | Path, passphrase: Optional[bytes] = None) -> dict:
    """Export colony identity as a portable backup.

    Returns a dict containing:
        - colony_id
        - public_key_ed25519
        - private_key_pem (optionally encrypted)
        - genesis_manifest (if this Colony is Genesis)
        - backup_version
        - created_at

    This is everything needed to restore a Colony on a new machine.
    """
    state_dir = Path(state_dir)

    # Colony ID
    id_path = state_dir / COLONY_ID_FILE
    if not id_path.exists():
        raise FileNotFoundError("No colony-id found — nothing to back up")
    colony_id = id_path.read_text().strip()

    # Keypair
    keys_dir = state_dir / "colony-keys"
    priv_path = keys_dir / "private.pem"
    if not priv_path.exists():
        raise FileNotFoundError("No private key found — nothing to back up")

    # Read private key PEM
    private_pem = priv_path.read_bytes()

    # If a backup passphrase is given, encrypt the private key
    if passphrase:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        # Load the key (may or may not be encrypted already)
        existing_pass = None  # We read raw PEM; let the caller handle decryption first
        key_obj = serialization.load_pem_private_key(private_pem, password=existing_pass)
        private_pem = key_obj.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(passphrase),
        )

    # Public key
    pub_path = keys_dir / "public.pem"
    public_key_hex = ""
    if pub_path.exists():
        # Derive hex from PEM
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_pem = pub_path.read_bytes()
        pub_key = serialization.load_pem_public_key(pub_pem)
        public_key_hex = pub_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

    # Genesis manifest (if this Colony is Genesis)
    genesis_data = None
    genesis_path = state_dir / "genesis.json"
    if genesis_path.exists():
        genesis_data = json.loads(genesis_path.read_text())

    backup = {
        "backup_version": 1,
        "colony_id": colony_id,
        "public_key_ed25519": public_key_hex,
        "private_key_pem": private_pem.decode() if isinstance(private_pem, bytes) else private_pem,
        "encrypted": passphrase is not None,
        "genesis": genesis_data,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return backup


def restore_colony(
    state_dir: str | Path,
    backup_data: dict,
    passphrase: Optional[bytes] = None,
) -> str:
    """Restore a Colony from a backup.

    Writes colony-id, private key, public key, and optionally genesis manifest.
    Returns the restored colony_id.

    Parameters
    ----------
    state_dir : path
        Target state directory.
    backup_data : dict
        Backup data from backup_colony().
    passphrase : bytes, optional
        Passphrase to decrypt the private key in the backup.
    """
    state_dir = Path(state_dir)

    # Validate backup
    if backup_data.get("backup_version") != 1:
        raise ValueError(f"Unsupported backup version: {backup_data.get('backup_version')}")

    colony_id = backup_data["colony_id"]
    private_pem = backup_data["private_key_pem"]
    is_encrypted = backup_data.get("encrypted", False)

    # If backup is encrypted and no passphrase given, try loading with None
    # (it might be encrypted with empty passphrase)
    load_password = passphrase if is_encrypted else None

    # Verify the private key loads
    from cryptography.hazmat.primitives import serialization
    try:
        key_obj = serialization.load_pem_private_key(
            private_pem.encode() if isinstance(private_pem, str) else private_pem,
            password=load_password,
        )
    except (ValueError, TypeError) as e:
        raise ValueError(f"Failed to decrypt private key — wrong passphrase? {e}") from e

    # Write colony-id
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / COLONY_ID_FILE).write_text(colony_id)

    # Write keypair
    keys_dir = state_dir / "colony-keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv_path = keys_dir / "private.pem"
    pub_path = keys_dir / "public.pem"

    # Store private key as-is (already encrypted if passphrase was given)
    pem_bytes = private_pem.encode() if isinstance(private_pem, str) else private_pem
    priv_path.write_bytes(pem_bytes)
    import os
    os.chmod(priv_path, 0o600)

    # Write public key PEM
    pub_pem = key_obj.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_path.write_bytes(pub_pem)

    # Restore Genesis manifest if present
    genesis_data = backup_data.get("genesis")
    if genesis_data:
        genesis_path = state_dir / "genesis.json"
        genesis_path.write_text(json.dumps(genesis_data, indent=2) + "\n")
        set_genesis_manifest(genesis_data)

    logger.info("Colony restored: %s", colony_id)
    return colony_id
