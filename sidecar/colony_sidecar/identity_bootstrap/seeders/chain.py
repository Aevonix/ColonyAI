"""Chain seeder — submits a COLONY_REGISTER transaction on first boot."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ChainSeeder:
    name = "chain"

    def __init__(self, chain_manager: Optional[Any] = None) -> None:
        self._chain_manager = chain_manager

    async def seed(self, corpus: Any) -> None:
        manager = self._chain_manager
        if manager is None:
            try:
                from colony_sidecar.chain.manager import ChainManager
                manager = ChainManager.get_instance()
            except Exception as exc:
                logger.debug("chain: ChainManager unavailable: %s", exc)
                return

        try:
            from colony_sidecar.chain.transactions import Transaction, TxType
        except ImportError:
            logger.debug("chain: transactions module not importable — skipping")
            return

        tx = Transaction(
            tx_id=f"bootstrap-{corpus.colony_id[:8]}-{uuid.uuid4().hex[:8]}",
            type=TxType.COLONY_REGISTER,
            from_colony_id="system",
            timestamp=datetime.now(timezone.utc).isoformat(),
            nonce=0,
            payload={
                "colony_id": corpus.colony_id,
                "colony_name": corpus.colony_name,
                "colony_version": corpus.colony_version,
                "network_id": corpus.network_id,
                "public_key_hex": corpus.public_key_hex,
                "bootstrap": True,
                "corpus_version": corpus.corpus_version,
            },
            signature="bootstrap",
        )

        try:
            result = await manager.submit_transaction(tx)
            logger.info("chain: bootstrap transaction submitted (result=%s)", result)
        except Exception as exc:
            logger.warning("chain: submit_transaction failed: %s", exc)
