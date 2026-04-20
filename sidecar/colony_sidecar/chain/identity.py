"""Colony Identity — colony_id management and Genesis trust anchor.

Colony identity is decoupled from the signing keypair:
    colony_id   — permanent UUID, generated once at init, never changes
    keypair     — Ed25519, can rotate without changing colony_id
    public_key  — derived from keypair, published openly for verification

Genesis:
    The first Colony — the Genesis Colony — is the trust anchor for the network.
    The Genesis manifest is self-signed: it contains a signature created with
    the Genesis Colony's private key that verifies against a HARDCODED public key in this
    source file. This makes Genesis unforgeable even locally:

    - Editing genesis.json → signature fails verification → is_genesis: false
    - Editing the hardcoded key in source → creates a different trust anchor,
      other Colonies running the official release won't recognize it
    - Only the Genesis Colony can produce a valid signature (requires private key)

    This is the same model as CA root certificates in browsers, or the
    Bitcoin genesis block hash — open, verifiable, unfakeable.
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
# Genesis trust anchor — HARDCODED public key (the Genesis Colony)
# ---------------------------------------------------------------------------
# This is the root of trust. The private key never leaves the Genesis owner's machine.
# This public key is used to VERIFY the Genesis manifest signature.
# It cannot be used to CREATE signatures — that requires the private key.
#
# Even if someone edits their local genesis.json, the signature won't
# verify against this key. Even if they edit this source file, it only
# affects their own Colony — other Colonies run the official release.
# ---------------------------------------------------------------------------

GENESIS_TRUST_PUBLIC_KEY = "341065fd6cd26ca501c5786ed1517eedc448fec60aeaea8d047d07bf1a9cc351"


def _verify_ed25519_signature(public_key_hex: str, message: bytes, signature_hex: str) -> bool:
    """Verify an Ed25519 signature against a public key."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_bytes = bytes.fromhex(public_key_hex)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        sig_bytes = bytes.fromhex(signature_hex)
        pub_key.verify(sig_bytes, message)
        return True
    except Exception:
        return False


def _sign_with_key(private_key_pem: str | bytes, message: bytes, passphrase: Optional[bytes] = None) -> str:
    """Sign a message with an Ed25519 private key, returning hex signature."""
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(
        private_key_pem.encode() if isinstance(private_key_pem, str) else private_key_pem,
        password=passphrase,
    )
    signature = key.sign(message)
    return signature.hex()


# ---------------------------------------------------------------------------
# Genesis manifest
# ---------------------------------------------------------------------------

_GENESIS_MANIFEST: Optional[dict] = None


def set_genesis_manifest(manifest: dict) -> None:
    """Set the Genesis manifest (called after verification)."""
    global _GENESIS_MANIFEST
    _GENESIS_MANIFEST = manifest


def get_genesis_manifest() -> Optional[dict]:
    """Get the Genesis manifest, if configured and verified."""
    return _GENESIS_MANIFEST


def is_genesis(colony_id: str, public_key_hex: str) -> bool:
    """Check if a colony_id + public_key combination matches the verified Genesis manifest.

    The manifest must have been loaded with a valid signature against the
    hardcoded GENESIS_TRUST_PUBLIC_KEY. Simply matching fields is not enough —
    the signature proves it was created by the Genesis Colony's private key.
    """
    if _GENESIS_MANIFEST is None:
        return False
    return (
        colony_id == _GENESIS_MANIFEST.get("colony_id")
        and public_key_hex == _GENESIS_MANIFEST.get("public_key_ed25519")
    )


def _manifest_signing_payload(manifest: dict) -> bytes:
    """Create the canonical signing payload from a manifest dict.

    This is the JSON-serialized content excluding the signature field itself,
    with sorted keys for deterministic ordering.
    """
    payload = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_genesis_manifest(manifest: dict) -> bool:
    """Verify a Genesis manifest's signature against the hardcoded trust key.

    Returns True if the signature is valid, False otherwise.
    A manifest without a signature is automatically invalid.
    """
    signature = manifest.get("signature")
    if not signature:
        logger.warning("Genesis manifest has no signature — cannot verify")
        return False

    payload = _manifest_signing_payload(manifest)
    return _verify_ed25519_signature(GENESIS_TRUST_PUBLIC_KEY, payload, signature)


def load_genesis_manifest(path: str | Path) -> Optional[dict]:
    """Load and verify a Genesis manifest from a JSON file.

    Only accepts the manifest if its signature verifies against the
    hardcoded GENESIS_TRUST_PUBLIC_KEY. An unsigned or tampered manifest
    is rejected.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        # Validate required fields
        required = {"colony_id", "public_key_ed25519", "alias", "genesis_version", "signature"}
        if not required.issubset(set(data.keys())):
            missing = required - set(data.keys())
            logger.warning("Genesis manifest missing fields: %s", missing)
            return None

        # Verify signature against hardcoded trust key
        if not verify_genesis_manifest(data):
            logger.warning("Genesis manifest signature INVALID — rejecting (tampered or unsigned)")
            return None

        set_genesis_manifest(data)
        logger.info("Genesis manifest verified and loaded")
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
    private_key_pem: Optional[str | bytes] = None,
    passphrase: Optional[bytes] = None,
) -> dict:
    """Create a signed Genesis manifest for the Genesis Colony.

    The manifest is signed with the Genesis Colony's private key so that all Colonies
    can verify it against the hardcoded GENESIS_TRUST_PUBLIC_KEY.

    This should only be called once — when the Genesis Colony is first initialized.
    The manifest is saved to disk and should be committed to the repo.
    """
    manifest = {
        "colony_id": colony_id,
        "public_key_ed25519": public_key_hex,
        "alias": "genesis",
        "genesis_version": 1,
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "description": "The original Colony — trust anchor for the network",
    }

    # Sign the manifest with the private key
    if private_key_pem:
        payload = _manifest_signing_payload(manifest)
        signature = _sign_with_key(private_key_pem, payload, passphrase=passphrase)
        manifest["signature"] = signature
        logger.info("Genesis manifest signed")
    else:
        raise ValueError("Cannot create Genesis manifest without a private key — signing is required")

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

        existing_pass = None
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
    """
    state_dir = Path(state_dir)

    if backup_data.get("backup_version") != 1:
        raise ValueError(f"Unsupported backup version: {backup_data.get('backup_version')}")

    colony_id = backup_data["colony_id"]
    private_pem = backup_data["private_key_pem"]
    is_encrypted = backup_data.get("encrypted", False)

    load_password = passphrase if is_encrypted else None

    from cryptography.hazmat.primitives import serialization
    try:
        key_obj = serialization.load_pem_private_key(
            private_pem.encode() if isinstance(private_pem, str) else private_pem,
            password=load_password,
        )
    except (ValueError, TypeError) as e:
        raise ValueError(f"Failed to decrypt private key — wrong passphrase? {e}") from e

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / COLONY_ID_FILE).write_text(colony_id)

    keys_dir = state_dir / "colony-keys"
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv_path = keys_dir / "private.pem"
    pub_path = keys_dir / "public.pem"

    pem_bytes = private_pem.encode() if isinstance(private_pem, str) else private_pem
    priv_path.write_bytes(pem_bytes)
    import os
    os.chmod(priv_path, 0o600)

    pub_pem = key_obj.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_path.write_bytes(pub_pem)

    genesis_data = backup_data.get("genesis")
    if genesis_data:
        genesis_path = state_dir / "genesis.json"
        genesis_path.write_text(json.dumps(genesis_data, indent=2) + "\n")
        set_genesis_manifest(genesis_data)

    logger.info("Colony restored: %s", colony_id)
    return colony_id
