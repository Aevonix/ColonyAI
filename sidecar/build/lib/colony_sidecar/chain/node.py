"""Colony Node Identity — per-device identity within a Colony.

Each physical device running a Colony instance is a Node. Nodes have:
    node_id   — unique UUID per device, generated on first `colony start`
    node keypair — Ed25519, independent from Colony keypair
    node cert — signed by Colony private key, proving membership

The Colony is the logical identity. Nodes are the physical instances.
One Colony can run on many devices — each gets its own node_id.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NODE_ID_FILE = "node-id"
NODE_KEYS_DIR = "node-keys"
NODE_CERT_FILE = "node-cert.json"


def get_or_create_node_id(state_dir: str | Path) -> str:
    """Get or create the node_id for this device."""
    id_path = Path(state_dir) / NODE_ID_FILE
    if id_path.exists():
        node_id = id_path.read_text().strip()
        if node_id:
            return node_id

    node_id = str(uuid.uuid4())
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text(node_id)
    logger.info("Generated new node_id: %s", node_id)
    return node_id


def ensure_node_keypair(state_dir: str | Path) -> "LocalKeyManager":
    """Ensure this device has a node keypair. Generate if missing.

    Returns a LocalKeyManager for the node keys.
    """
    from colony_sidecar.chain.local_keys import LocalKeyManager

    node_id = get_or_create_node_id(state_dir)
    keys_dir = Path(state_dir) / NODE_KEYS_DIR
    keys_dir.mkdir(parents=True, exist_ok=True)

    if (keys_dir / "private.pem").exists():
        return LocalKeyManager(keys_dir=keys_dir, colony_id=node_id)

    km = LocalKeyManager.generate(keys_dir=keys_dir, colony_id=node_id)
    logger.info("Generated node keypair for node %s", node_id)
    return km


def create_node_certificate(
    state_dir: str | Path,
    colony_key_manager: Optional["LocalKeyManager"] = None,
) -> dict:
    """Create a node certificate signed by the Colony's private key.

    The certificate binds: colony_id, node_id, node_public_key, issued_at.
    It proves this device belongs to the Colony.
    """
    from colony_sidecar.chain.local_keys import LocalKeyManager
    from colony_sidecar.chain.identity import get_or_create_colony_id

    state_dir = Path(state_dir)
    colony_id = get_or_create_colony_id(state_dir)
    node_id = get_or_create_node_id(state_dir)
    node_km = ensure_node_keypair(state_dir)
    node_pubkey = node_km.public_key_hex()

    cert = {
        "colony_id": colony_id,
        "node_id": node_id,
        "node_public_key_ed25519": node_pubkey,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }

    # Sign with Colony private key
    if colony_key_manager is None:
        colony_keys_dir = state_dir / "colony-keys"
        passphrase = None  # Caller should handle passphrase
        if (colony_keys_dir / "private.pem").exists():
            colony_key_manager = LocalKeyManager(keys_dir=colony_keys_dir, colony_id=colony_id, passphrase=passphrase)

    if colony_key_manager is not None:
        # Deterministic payload for signing (exclude signature field)
        payload = json.dumps(cert, sort_keys=True, separators=(",", ":")).encode("utf-8")
        cert["signature"] = colony_key_manager.sign(payload)
        logger.info("Node certificate signed by Colony key")
    else:
        logger.warning("No Colony key manager available — node certificate is unsigned")

    cert_path = state_dir / NODE_CERT_FILE
    cert_path.write_text(json.dumps(cert, indent=2) + "\n")
    return cert


def verify_node_certificate(cert: dict, colony_public_key_hex: str) -> bool:
    """Verify a node certificate's signature against a Colony public key."""
    from colony_sidecar.chain.identity import _verify_ed25519_signature

    signature = cert.get("signature")
    if not signature:
        return False

    payload = {k: v for k, v in cert.items() if k != "signature"}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _verify_ed25519_signature(colony_public_key_hex, payload_bytes, signature)


def load_node_certificate(state_dir: str | Path) -> Optional[dict]:
    """Load the node certificate from disk."""
    cert_path = Path(state_dir) / NODE_CERT_FILE
    if not cert_path.exists():
        return None
    try:
        return json.loads(cert_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def get_node_info(state_dir: str | Path) -> dict:
    """Get full node identity info: node_id, public key, cert status."""
    state_dir = Path(state_dir)

    node_id = None
    node_pubkey = None
    certified = False
    issued_at = None

    node_id_path = state_dir / NODE_ID_FILE
    if node_id_path.exists():
        node_id = node_id_path.read_text().strip() or None

    node_keys_dir = state_dir / NODE_KEYS_DIR
    if (node_keys_dir / "private.pem").exists():
        try:
            from colony_sidecar.chain.local_keys import LocalKeyManager
            km = LocalKeyManager(keys_dir=node_keys_dir, colony_id=node_id or "unknown")
            node_pubkey = km.public_key_hex()
        except Exception:
            pass

    cert = load_node_certificate(state_dir)
    if cert and cert.get("signature"):
        certified = True
        issued_at = cert.get("issued_at")

    return {
        "node_id": node_id,
        "node_public_key": node_pubkey,
        "certified": certified,
        "issued_at": issued_at,
    }
