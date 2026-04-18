"""Colony Identity Bootstrap — IdentityBootstrapBuilder.

Assembles a SelfKnowledgeCorpus at runtime by reading colony_id, version,
network info, and public key from the running system.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from colony_sidecar.identity_bootstrap.corpus import (
    CORPUS_VERSION,
    GATE_LAYERS,
    INFERENCE_TIERS,
    API_ENDPOINTS,
    COGNITION_PHASES,
    LAYERS,
    InferenceTier,
    SelfKnowledgeCorpus,
)

logger = logging.getLogger(__name__)


def _colony_version() -> str:
    """Return Colony version from canonical source."""
    # Canonical: colony_cli.__init__ (full_version_string returns "0.3.0-h6")
    try:
        from colony_cli import full_version_string
        return full_version_string()
    except Exception:
        pass
    # Fallback: importlib metadata (reads pyproject.toml version)
    try:
        from importlib.metadata import version
        return version("colony")
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("colony-ai")
    except Exception:
        pass
    return "0.3.0-h6"


def _read_colony_id(chain_manager: Optional[Any]) -> str:
    """Read colony_id from ChainManager, chain.db, or environment.

    Priority order:
    1. ChainManager.colony_id (if ChainManager is initialized)
    2. COLONY_ID environment variable
    3. Genesis block genesis_colony_id from chain.db (crypto identity)
    4. Hostname-based fallback (last resort)
    """
    if chain_manager is not None:
        try:
            cid = chain_manager.colony_id
            if cid and cid != "local":
                return str(cid)
        except Exception as exc:
            logger.debug("ChainManager.colony_id unavailable: %s", exc)

    env_id = os.environ.get("COLONY_ID", "")
    if env_id:
        return env_id

    # Read genesis_colony_id directly from chain.db — this is the
    # cryptographic identity (sha256 of Ed25519 pubkey) that proves
    # genesis admin authority.  Avoids the hostname fallback when the
    # ChainManager singleton hasn't been constructed yet.
    import json
    import sqlite3
    colony_home = os.environ.get(
        "COLONY_HOME",
        os.path.join(os.path.expanduser("~"), ".colony"),
    )
    chain_db = os.path.join(colony_home, "chain.db")
    if os.path.exists(chain_db):
        try:
            conn = sqlite3.connect(chain_db)
            row = conn.execute(
                "SELECT raw_json FROM chain_blocks WHERE block_index = 0"
            ).fetchone()
            conn.close()
            if row:
                genesis_data = json.loads(row[0])
                cid = genesis_data.get("metadata", {}).get("genesis_colony_id")
                if cid:
                    return cid
        except Exception as exc:
            logger.debug("_read_colony_id chain.db fallback failed: %s", exc)

    # Last resort: hostname-based stable ID
    import socket
    import hashlib
    raw = f"{socket.gethostname()}-colony"
    return "col-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def _read_network_id(chain_manager: Optional[Any]) -> str:
    try:
        if chain_manager is not None:
            state = getattr(chain_manager, "state", None)
            if state is not None:
                nid = getattr(state, "network_id", None) or getattr(state, "genesis_hash", None)
                if nid:
                    return str(nid)
    except Exception:
        pass
    return os.environ.get("COLONY_NETWORK_ID", "local")


def _read_public_key(chain_manager: Optional[Any]) -> str:
    try:
        if chain_manager is not None:
            keys = getattr(chain_manager, "keys", None)
            if keys is not None:
                pub = getattr(keys, "public_key_hex", None) or getattr(keys, "public_key", None)
                if pub:
                    return str(pub)
    except Exception:
        pass
    return "unknown"


def _resolve_inference_tiers() -> list[InferenceTier]:
    """Return INFERENCE_TIERS with model names populated from the live LLMRouter config.

    The static corpus stores no provider-specific model names.  This function
    reads them from ``colony.router.tiers.DEFAULT_TIERS`` at runtime so the
    corpus always reflects whichever models are actually configured.
    Falls back to the static definitions (empty model lists) if the router
    module is unavailable.
    """
    # Tier index → ModelTier enum value in the router
    try:
        from colony_sidecar.router.tiers import DEFAULT_TIERS, ModelTier  # type: ignore[import]

        tier_index_to_router: dict[int, ModelTier] = {
            0: ModelTier.SMALL,
            1: ModelTier.MEDIUM,
            2: ModelTier.LARGE,
        }
        resolved: list[InferenceTier] = []
        for tier in INFERENCE_TIERS:
            router_tier = tier_index_to_router.get(tier.tier_index)
            if router_tier is not None and router_tier in DEFAULT_TIERS:
                model_id = DEFAULT_TIERS[router_tier].model_id
                resolved.append(
                    InferenceTier(
                        name=tier.name,
                        description=tier.description,
                        complexity_range=tier.complexity_range,
                        models=[model_id],
                        tier_index=tier.tier_index,
                    )
                )
            else:
                resolved.append(tier)
        return resolved
    except Exception as exc:
        logger.debug("Could not resolve inference tiers from router config: %s", exc)
        return list(INFERENCE_TIERS)


class IdentityBootstrapBuilder:
    """Builds a SelfKnowledgeCorpus by introspecting the running Colony instance."""

    def __init__(self, chain_manager: Optional[Any] = None) -> None:
        self._chain_manager = chain_manager

    def build(self) -> SelfKnowledgeCorpus:
        colony_id = _read_colony_id(self._chain_manager)
        colony_name = os.environ.get("COLONY_NAME", f"colony-{colony_id[:8]}")
        colony_version = _colony_version()
        network_id = _read_network_id(self._chain_manager)
        public_key_hex = _read_public_key(self._chain_manager)

        corpus = SelfKnowledgeCorpus(
            colony_id=colony_id,
            colony_name=colony_name,
            colony_version=colony_version,
            network_id=network_id,
            public_key_hex=public_key_hex,
            layers=list(LAYERS),
            api_endpoints=list(API_ENDPOINTS),
            cognition_phases=list(COGNITION_PHASES),
            gate_layers=list(GATE_LAYERS),
            inference_tiers=_resolve_inference_tiers(),
            corpus_version=CORPUS_VERSION,
            properties={
                "colony_home": os.environ.get(
                    "COLONY_HOME",
                    os.path.join(os.path.expanduser("~"), ".colony"),
                ),
                "python_version": _python_version(),
            },
        )
        return corpus


def _python_version() -> str:
    import sys
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"
