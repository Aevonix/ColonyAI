"""Persistent local UUID/key identity and optional federation trust.

Local identity needs no federation or shared trust anchor. A deployment can
place a signed genesis.json in its private state directory and explicitly
select its verification key with PACOMIND_GENESIS_TRUST_PUBLIC_KEY. The public
package supplies neither a manifest nor a trusted owner's key.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

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


def set_genesis_manifest(manifest: Optional[dict]) -> None:
    """Retain a manifest only under this instance's explicit trust key."""
    global _GENESIS_MANIFEST
    _GENESIS_MANIFEST = manifest if manifest is not None and verify_genesis_manifest(manifest) else None


def get_genesis_manifest() -> Optional[dict]:
    """Get the Genesis manifest, if configured and verified."""
    if _GENESIS_MANIFEST is not None and verify_genesis_manifest(_GENESIS_MANIFEST):
        return _GENESIS_MANIFEST
    return None


GENESIS_MANIFEST_FILE = "genesis.json"


def resolve_genesis_manifest_path(state_dir: str | Path) -> Optional[Path]:
    """Locate only this instance's optional private federation manifest."""
    local = Path(state_dir) / GENESIS_MANIFEST_FILE
    if local.is_file():
        return local
    return None


def is_genesis(pacomind_id: str, public_key_hex: str) -> bool:
    """Check if a pacomind_id + public_key combination matches the verified Genesis manifest.

    The manifest must verify against the explicitly configured instance key.
    """
    manifest = get_genesis_manifest()
    if manifest is None:
        return False
    return (
        pacomind_id == manifest.get("pacomind_id")
        and public_key_hex == manifest.get("public_key_ed25519")
    )


def _manifest_signing_payload(manifest: dict) -> bytes:
    """Create the canonical signing payload from a manifest dict.

    This is the JSON-serialized content excluding the signature field itself,
    with sorted keys for deterministic ordering.
    """
    payload = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_genesis_manifest(manifest: dict) -> bool:
    """Verify a manifest against PACOMIND_GENESIS_TRUST_PUBLIC_KEY, with no default.

    Returns True if the signature is valid, False otherwise.
    A manifest without a signature is automatically invalid.
    """
    trust_key = os.environ.get("PACOMIND_GENESIS_TRUST_PUBLIC_KEY", "").strip()
    if not trust_key:
        return False
    signature = manifest.get("signature")
    if not signature:
        logger.warning("Genesis manifest has no signature — cannot verify")
        return False

    payload = _manifest_signing_payload(manifest)
    return _verify_ed25519_signature(trust_key, payload, signature)


def load_genesis_manifest(path: str | Path) -> Optional[dict]:
    """Load and verify a Genesis manifest from a JSON file.

    Missing trust configuration leaves local identity available without
    granting federation trust. Invalid or absent manifests clear loaded trust.
    """
    set_genesis_manifest(None)
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        # Validate required fields
        required = {"pacomind_id", "public_key_ed25519", "alias", "genesis_version", "signature"}
        if not required.issubset(set(data.keys())):
            missing = required - set(data.keys())
            logger.warning("Genesis manifest missing fields: %s", missing)
            return None

        # Verify against only the deployment's explicit trust configuration.
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
# PacoMind ID management
# ---------------------------------------------------------------------------

PACOMIND_ID_FILE = "pacomind-id"


def get_or_create_pacomind_id(state_dir: str | Path) -> str:
    """Get the existing pacomind_id or create a new random UUID.

    The pacomind_id is stored in {state_dir}/pacomind-id.
    It is generated once and never changes.
    For disaster recovery, use 'pacomind backup' / 'pacomind restore'.
    """
    id_path = Path(state_dir) / PACOMIND_ID_FILE
    if id_path.exists():
        pacomind_id = id_path.read_text().strip()
        if pacomind_id:
            return pacomind_id

    # Generate new
    pacomind_id = str(uuid.uuid4())
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text(pacomind_id)
    logger.info("Generated new pacomind_id: %s", pacomind_id)
    return pacomind_id


def create_genesis_manifest(
    pacomind_id: str,
    public_key_hex: str,
    output_path: str | Path,
    private_key_pem: Optional[str | bytes] = None,
    passphrase: Optional[bytes] = None,
) -> dict:
    """Create a signed Genesis manifest for the Genesis PacoMind.

    Signing creates a private federation manifest, not automatic trust. Each
    participating deployment must explicitly configure the verification key.
    Keep the manifest in private instance state; do not add it to public code.
    """
    manifest = {
        "pacomind_id": pacomind_id,
        "public_key_ed25519": public_key_hex,
        "alias": "genesis",
        "genesis_version": 1,
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "description": "Federation trust anchor for explicitly configured deployments",
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


def create_pacomind_manifest(
    pacomind_id: str,
    public_key_hex: str,
    output_path: str | Path,
) -> dict:
    """Create a standard (non-Genesis) pacomind manifest.

    Every PacoMind can generate a manifest to share its public identity.
    Other Colonies can load this to establish trust.
    """
    manifest = {
        "pacomind_id": pacomind_id,
        "public_key_ed25519": public_key_hex,
        "alias": "",  # User-settable nickname
        "genesis_version": 0,  # 0 = not Genesis
        "signed_at": datetime.now(timezone.utc).isoformat(),
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(manifest, indent=2) + "\n")
    logger.info("PacoMind manifest created at %s", output_path)
    return manifest


def backup_pacomind(state_dir: str | Path, passphrase: Optional[bytes] = None) -> dict:
    """Export pacomind identity as a portable backup.

    Returns a dict containing:
        - pacomind_id
        - public_key_ed25519
        - private_key_pem (optionally encrypted)
        - genesis_manifest (if this PacoMind is Genesis)
        - backup_version
        - created_at

    This is everything needed to restore a PacoMind on a new machine.
    """
    state_dir = Path(state_dir)

    # PacoMind ID
    id_path = state_dir / PACOMIND_ID_FILE
    if not id_path.exists():
        raise FileNotFoundError("No pacomind-id found — nothing to back up")
    pacomind_id = id_path.read_text().strip()

    # Keypair
    keys_dir = state_dir / "pacomind-keys"
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

    # Genesis manifest (if this PacoMind is Genesis)
    genesis_data = None
    genesis_path = state_dir / "genesis.json"
    if genesis_path.exists():
        genesis_data = json.loads(genesis_path.read_text())

    backup = {
        "backup_version": 1,
        "pacomind_id": pacomind_id,
        "public_key_ed25519": public_key_hex,
        "private_key_pem": private_pem.decode() if isinstance(private_pem, bytes) else private_pem,
        "encrypted": passphrase is not None,
        "genesis": genesis_data,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return backup


def restore_pacomind(
    state_dir: str | Path,
    backup_data: dict,
    passphrase: Optional[bytes] = None,
) -> str:
    """Restore a PacoMind from a backup.

    Writes pacomind-id, private key, public key, and optionally genesis manifest.
    Returns the restored pacomind_id.
    """
    state_dir = Path(state_dir)

    if backup_data.get("backup_version") != 1:
        raise ValueError(f"Unsupported backup version: {backup_data.get('backup_version')}")

    pacomind_id = backup_data["pacomind_id"]
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
    (state_dir / PACOMIND_ID_FILE).write_text(pacomind_id)

    keys_dir = state_dir / "pacomind-keys"
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

    logger.info("PacoMind restored: %s", pacomind_id)
    return pacomind_id
