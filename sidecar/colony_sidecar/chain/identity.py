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
    """Get the existing colony_id or create a new one.

    The colony_id is a UUID stored in {state_dir}/colony-id.
    It is generated once and never changes.
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
