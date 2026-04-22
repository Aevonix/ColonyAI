"""CLI commands for ColonyChain management.

Usage:
    colony chain status                     # Show chain state summary
    colony chain genesis --name NAME        # Initialize genesis block
    colony chain info --block N             # Show block details
    colony chain tx --tx-id ID              # Show transaction details
    colony chain list-colonies              # List registered colonies
    colony chain list-sentinels             # List active sentinels
    colony chain verify                     # Verify chain integrity
    colony chain mempool                    # Show pending mempool transactions
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".colony" / "chain.db"


def _load_store(db_path: str | None = None) -> "ChainStore":
    from colony_sidecar.chain.storage import ChainStore
    path = Path(db_path) if db_path else _DEFAULT_DB
    return ChainStore(path)


def _load_state(db_path: str | None = None) -> "ChainState":
    from colony_sidecar.chain.state_machine import ChainStateMachine
    store = _load_store(db_path)
    sm = ChainStateMachine(store)
    return sm.get_current_state()


def cmd_chain_status(args) -> int:
    """Show chain state summary."""
    try:
        store = _load_store(getattr(args, "db", None))
        from colony_sidecar.chain.state_machine import ChainStateMachine
        sm = ChainStateMachine(store)
        state = sm.get_current_state()
        height = store.get_height()

        print("ColonyChain Status")
        print("==================")
        print(f"  Height:           {height}")
        print(f"  Last block hash:  {state.last_block_hash[:24]}...")
        print(f"  Network ID:       {state.network_id[:24]}..." if state.network_id else "  Network ID:       (none)")
        print(f"  Genesis admin:    {state.genesis_admin_id or '(none)'}")
        print(f"  Colonies:         {len(state.colony_registry)}")
        print(f"  Suspended:        {len(state.suspended_colonies)}")
        print(f"  Sentinels:        {len(state.active_sentinels())}")
        print(f"  Trust edges:      {len(state.trust_graph)}")
        print(f"  Mempool size:     {store.mempool_size()}")
        print(f"  Block interval:   {state.protocol_config.block_interval_secs}s")
        print(f"  Untrust threshold:{state.protocol_config.untrust_threshold}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_genesis(args) -> int:
    """Initialize a new ColonyChain with a genesis block."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption
    )
    from colony_sidecar.chain.genesis import GenesisConfig, create_genesis_block
    from colony_sidecar.chain.storage import ChainStore

    name = getattr(args, "name", None) or "genesis"
    db_path = getattr(args, "db", None)
    key_file = getattr(args, "key_file", None)

    db = Path(db_path) if db_path else _DEFAULT_DB
    db.parent.mkdir(parents=True, exist_ok=True)

    store = ChainStore(db)
    if store.get_height() >= 0:
        print("Error: chain already initialized (genesis block exists)", file=sys.stderr)
        return 1

    # Generate or load key
    if key_file and Path(key_file).exists():
        raw = bytes.fromhex(Path(key_file).read_text().strip())
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        print(f"Loaded key from {key_file}")
    else:
        priv = Ed25519PrivateKey.generate()
        if key_file:
            raw_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            Path(key_file).write_text(raw_bytes.hex())
            print(f"Generated new key → {key_file}")
        else:
            print("Generated ephemeral key (not saved). Use --key-file to persist.")

    pub = priv.public_key()
    pub_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    colony_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()

    def sign_fn(data: bytes) -> str:
        return priv.sign(data).hex()

    cfg = GenesisConfig(
        genesis_colony_name=name,
        genesis_colony_id=colony_id,
        genesis_pubkey_hex=pub_hex,
    )
    block = create_genesis_block(cfg, sign_fn=sign_fn)
    store.append_block(block)

    print(f"Genesis block created:")
    print(f"  Colony name:  {name}")
    print(f"  Colony ID:    {colony_id}")
    print(f"  Block hash:   {block.block_hash[:24]}...")
    print(f"  Network ID:   {block.metadata.get('network_id', '')[:24]}...")
    print(f"  DB path:      {db}")
    return 0


def cmd_chain_info(args) -> int:
    """Show block details."""
    block_index = getattr(args, "block", None)
    if block_index is None:
        print("Error: --block N required", file=sys.stderr)
        return 1
    try:
        block_index = int(block_index)
        store = _load_store(getattr(args, "db", None))
        block = store.get_block(block_index)
        if block is None:
            print(f"Block {block_index} not found", file=sys.stderr)
            return 1
        d = block.to_dict()
        # Truncate tx list for display
        tx_count = len(d["transactions"])
        d["transactions"] = f"<{tx_count} transactions>"
        print(json.dumps(d, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_tx(args) -> int:
    """Show transaction details."""
    tx_id = getattr(args, "tx_id", None)
    if not tx_id:
        print("Error: --tx-id required", file=sys.stderr)
        return 1
    try:
        store = _load_store(getattr(args, "db", None))
        txs = store.get_transactions_for_colony("")
        # search all by fetching blocks
        height = store.get_height()
        for idx in range(height + 1):
            block = store.get_block(idx)
            if block:
                for tx in block.transactions:
                    if tx.tx_id == tx_id:
                        d = tx.to_dict()
                        d["block_index"] = idx
                        print(json.dumps(d, indent=2, default=str))
                        return 0
        print(f"Transaction {tx_id!r} not found", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_list_colonies(args) -> int:
    """List all registered colonies."""
    try:
        state = _load_state(getattr(args, "db", None))
        if not state.colony_registry:
            print("No colonies registered.")
            return 0
        fmt = "{:<20} {:<12} {:<16} {}"
        print(fmt.format("NAME", "STATUS", "COLONY_ID (prefix)", "ENDPOINT"))
        print("-" * 80)
        for colony_id, record in sorted(state.colony_registry.items(), key=lambda x: x[1].name):
            flags = ""
            if record.is_genesis_admin:
                flags = " [genesis]"
            print(fmt.format(
                record.name + flags,
                record.status,
                colony_id[:16],
                record.endpoint or "(local)",
            ))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_list_sentinels(args) -> int:
    """List Sentinel validators."""
    try:
        state = _load_state(getattr(args, "db", None))
        if not state.sentinel_roster:
            print("No sentinels registered.")
            return 0
        fmt = "{:<36} {:<12} {:<20} {}"
        print(fmt.format("SENTINEL_ID", "STATUS", "HOST:PORT", "COLONY_ID (prefix)"))
        print("-" * 80)
        for sid, record in state.sentinel_roster.items():
            print(fmt.format(
                sid,
                record.status,
                f"{record.host}:{record.port}",
                record.colony_id[:16],
            ))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_verify(args) -> int:
    """Verify chain hash integrity from genesis."""
    try:
        store = _load_store(getattr(args, "db", None))
        height = store.get_height()
        if height < 0:
            print("Chain is empty (no blocks).")
            return 0

        errors = 0
        prev_hash = "0" * 64
        for idx in range(height + 1):
            block = store.get_block(idx)
            if block is None:
                print(f"  MISSING block {idx}", file=sys.stderr)
                errors += 1
                continue

            if idx == 0:
                if block.previous_hash != "0" * 64:
                    print(f"  FAIL block 0: previous_hash must be zeros")
                    errors += 1
            else:
                if block.previous_hash != prev_hash:
                    print(f"  FAIL block {idx}: previous_hash mismatch")
                    errors += 1

            from colony_sidecar.chain.block import build_merkle_root
            tx_ids = [tx.tx_id for tx in block.transactions]
            expected_merkle = build_merkle_root(tx_ids)
            if block.merkle_root != expected_merkle:
                print(f"  FAIL block {idx}: merkle_root mismatch")
                errors += 1

            prev_hash = block.block_hash

        if errors == 0:
            print(f"Chain verified OK: {height + 1} blocks, no errors.")
            return 0
        else:
            print(f"Chain verification FAILED: {errors} error(s).", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_mempool(args) -> int:
    """Show pending mempool transactions."""
    try:
        store = _load_store(getattr(args, "db", None))
        txs = store.get_mempool(limit=100)
        if not txs:
            print("Mempool is empty.")
            return 0
        print(f"Mempool: {len(txs)} pending transaction(s)")
        fmt = "{:<36} {:<24} {:<8} {}"
        print(fmt.format("TX_ID", "TYPE", "NONCE", "FROM (prefix)"))
        print("-" * 80)
        for tx in txs:
            print(fmt.format(
                tx.tx_id,
                tx.type.value if hasattr(tx.type, "value") else str(tx.type),
                str(tx.nonce),
                tx.from_colony_id[:16],
            ))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_state(args) -> int:
    """Dump current chain state summary."""
    try:
        state = _load_state(getattr(args, "db", None))
        import json as _json
        summary = {
            "height": state.height,
            "last_block_hash": state.last_block_hash,
            "network_id": state.network_id,
            "genesis_admin_id": state.genesis_admin_id,
            "colony_count": len(state.colony_registry),
            "suspended_count": len(state.suspended_colonies),
            "sentinel_count": len(state.active_sentinels()),
            "trust_edges": len(state.trust_graph),
            "protocol_config": {
                "block_interval_secs": state.protocol_config.block_interval_secs,
                "untrust_threshold": state.protocol_config.untrust_threshold,
                "min_sentinels": state.protocol_config.min_sentinels,
            },
            "upgrade_history_count": len(state.upgrade_history),
        }
        print(_json.dumps(summary, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_register(args) -> int:
    """Register this colony on-chain (colony_register transaction)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    from colony_sidecar.chain.transactions import TxType, Transaction
    import hashlib

    key_file = getattr(args, "key_file", None)
    name = getattr(args, "name", None)
    endpoint = getattr(args, "endpoint", "") or ""
    description = getattr(args, "description", "") or ""
    db_path = getattr(args, "db", None)

    if not name:
        print("Error: --name required", file=sys.stderr)
        return 1
    if not key_file:
        print("Error: --key-file required", file=sys.stderr)
        return 1
    if not Path(key_file).exists():
        print(f"Error: key file not found: {key_file}", file=sys.stderr)
        return 1

    try:
        raw = bytes.fromhex(Path(key_file).read_text().strip())
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        pub = priv.public_key()
        pub_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        colony_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()

        def sign_fn(data: bytes) -> str:
            return priv.sign(data).hex()

        store = _load_store(db_path)
        from colony_sidecar.chain.state_machine import ChainStateMachine
        sm = ChainStateMachine(store)
        state = sm.get_current_state()

        nonce_map = {}
        for cid, rec in state.colony_registry.items():
            pass
        last_nonce = 0
        for block_idx in range(store.get_height() + 1):
            block = store.get_block(block_idx)
            if block:
                for tx in block.transactions:
                    if tx.from_colony_id == colony_id and tx.nonce > last_nonce:
                        last_nonce = tx.nonce

        tx = Transaction.create(
            tx_type=TxType.COLONY_REGISTER,
            from_colony_id=colony_id,
            nonce=last_nonce + 1,
            payload={
                "name": name,
                "public_key_hex": pub_hex,
                "colony_id": colony_id,
                "endpoint": endpoint,
                "description": description[:256],
                "protocol_version": "1.0.0",
                "capabilities": ["task_delegation", "memory_sharing"],
                "genesis_admin": False,
                "metadata": {},
            },
            sign_fn=sign_fn,
        )
        store.add_to_mempool(tx)
        print(f"Submitted colony_register to mempool:")
        print(f"  tx_id:      {tx.tx_id}")
        print(f"  colony_id:  {colony_id}")
        print(f"  name:       {name}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_rotate_key(args) -> int:
    """Rotate this colony's Ed25519 key on-chain."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    from colony_sidecar.chain.transactions import TxType, Transaction
    import hashlib

    old_key_file = getattr(args, "old_key_file", None)
    new_key_file = getattr(args, "new_key_file", None)
    db_path = getattr(args, "db", None)

    if not old_key_file or not new_key_file:
        print("Error: --old-key-file and --new-key-file required", file=sys.stderr)
        return 1

    try:
        old_raw = bytes.fromhex(Path(old_key_file).read_text().strip())
        old_priv = Ed25519PrivateKey.from_private_bytes(old_raw)
        old_pub = old_priv.public_key()
        old_pub_hex = old_pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        colony_id = hashlib.sha256(bytes.fromhex(old_pub_hex)).hexdigest()

        if Path(new_key_file).exists():
            new_raw = bytes.fromhex(Path(new_key_file).read_text().strip())
            new_priv = Ed25519PrivateKey.from_private_bytes(new_raw)
        else:
            new_priv = Ed25519PrivateKey.generate()
            new_raw_bytes = new_priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            Path(new_key_file).write_text(new_raw_bytes.hex())
            print(f"Generated new key → {new_key_file}")

        new_pub = new_priv.public_key()
        new_pub_hex = new_pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

        def sign_fn(data: bytes) -> str:
            return old_priv.sign(data).hex()

        store = _load_store(db_path)
        last_nonce = 0
        for block_idx in range(store.get_height() + 1):
            block = store.get_block(block_idx)
            if block:
                for tx in block.transactions:
                    if tx.from_colony_id == colony_id and tx.nonce > last_nonce:
                        last_nonce = tx.nonce

        tx = Transaction.create(
            tx_type=TxType.COLONY_ROTATE_KEY,
            from_colony_id=colony_id,
            nonce=last_nonce + 1,
            payload={
                "new_public_key_hex": new_pub_hex,
                "old_public_key_hex": old_pub_hex,
                "reason": "scheduled_rotation",
            },
            sign_fn=sign_fn,
        )
        store.add_to_mempool(tx)
        print(f"Submitted colony_rotate_key to mempool:")
        print(f"  tx_id:      {tx.tx_id}")
        print(f"  colony_id:  {colony_id}")
        print(f"  new_pubkey: {new_pub_hex[:24]}...")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_trust(args) -> int:
    """Submit trust_attest for a peer colony."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from colony_sidecar.chain.transactions import TxType, Transaction
    import hashlib

    key_file = getattr(args, "key_file", None)
    target = getattr(args, "target", None) or getattr(args, "colony", None)
    level_raw = getattr(args, "level", None) or getattr(args, "trust_level", "1")
    db_path = getattr(args, "db", None)

    if not key_file or not target:
        print("Error: --key-file and --target (or positional colony name) required", file=sys.stderr)
        return 1

    try:
        level = int(level_raw)
        if not 0 <= level <= 4:
            print("Error: trust level must be 0-4", file=sys.stderr)
            return 1

        raw = bytes.fromhex(Path(key_file).read_text().strip())
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        pub = priv.public_key()
        pub_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        colony_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()

        def sign_fn(data: bytes) -> str:
            return priv.sign(data).hex()

        store = _load_store(db_path)
        state = _load_state(db_path)

        # Resolve target: may be a name or colony_id
        target_id = state.colony_id_for_name(target) or (target if target in state.colony_registry else None)
        if not target_id:
            print(f"Error: colony {target!r} not found in chain state", file=sys.stderr)
            return 1

        last_nonce = 0
        for block_idx in range(store.get_height() + 1):
            block = store.get_block(block_idx)
            if block:
                for tx in block.transactions:
                    if tx.from_colony_id == colony_id and tx.nonce > last_nonce:
                        last_nonce = tx.nonce

        tx = Transaction.create(
            tx_type=TxType.TRUST_ATTEST,
            from_colony_id=colony_id,
            nonce=last_nonce + 1,
            payload={
                "to_colony_id": target_id,
                "trust_level": level,
                "evidence_tx_ids": [],
            },
            sign_fn=sign_fn,
        )
        store.add_to_mempool(tx)
        print(f"Submitted trust_attest to mempool:")
        print(f"  tx_id:  {tx.tx_id}")
        print(f"  target: {target_id[:24]}... (level={level})")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_chain_untrust(args) -> int:
    """Submit untrust_attest for a peer colony."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from colony_sidecar.chain.transactions import TxType, Transaction
    import hashlib

    key_file = getattr(args, "key_file", None)
    target = getattr(args, "target", None) or getattr(args, "colony", None)
    db_path = getattr(args, "db", None)
    report_abuse = getattr(args, "report_abuse", False)

    if not key_file or not target:
        print("Error: --key-file and --target required", file=sys.stderr)
        return 1

    try:
        raw = bytes.fromhex(Path(key_file).read_text().strip())
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        pub = priv.public_key()
        pub_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        colony_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()

        def sign_fn(data: bytes) -> str:
            return priv.sign(data).hex()

        store = _load_store(db_path)
        state = _load_state(db_path)

        target_id = state.colony_id_for_name(target) or (target if target in state.colony_registry else None)
        if not target_id:
            print(f"Error: colony {target!r} not found", file=sys.stderr)
            return 1

        last_nonce = 0
        for block_idx in range(store.get_height() + 1):
            block = store.get_block(block_idx)
            if block:
                for tx in block.transactions:
                    if tx.from_colony_id == colony_id and tx.nonce > last_nonce:
                        last_nonce = tx.nonce

        tx = Transaction.create(
            tx_type=TxType.UNTRUST_ATTEST,
            from_colony_id=colony_id,
            nonce=last_nonce + 1,
            payload={
                "to_colony_id": target_id,
                "reason": getattr(args, "reason", "") or "",
                "report_abuse": bool(report_abuse),
                "evidence_tx_ids": [],
            },
            sign_fn=sign_fn,
        )
        store.add_to_mempool(tx)
        print(f"Submitted untrust_attest to mempool:")
        print(f"  tx_id:  {tx.tx_id}")
        print(f"  target: {target_id[:24]}...")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def run_chain_command(args: list) -> int:
    """Dispatch 'colony chain <subcommand>'."""
    if not args:
        _print_chain_help()
        return 1

    sub = args[0]
    remaining = args[1:]

    # Parse simple key=value style args from remaining
    parsed = _parse_args(remaining)

    class FakeArgs:
        pass

    fa = FakeArgs()
    for k, v in parsed.items():
        setattr(fa, k, v)

    dispatch = {
        "status": cmd_chain_status,
        "genesis": cmd_chain_genesis,
        "info": cmd_chain_info,
        "block": cmd_chain_info,
        "tx": cmd_chain_tx,
        "state": cmd_chain_state,
        "list-colonies": cmd_chain_list_colonies,
        "list_colonies": cmd_chain_list_colonies,
        "list-sentinels": cmd_chain_list_sentinels,
        "list_sentinels": cmd_chain_list_sentinels,
        "verify": cmd_chain_verify,
        "mempool": cmd_chain_mempool,
        "register": cmd_chain_register,
        "rotate-key": cmd_chain_rotate_key,
        "rotate_key": cmd_chain_rotate_key,
        "trust": cmd_chain_trust,
        "untrust": cmd_chain_untrust,
    }

    handler = dispatch.get(sub)
    if handler is None:
        print(f"Unknown chain subcommand: {sub!r}", file=sys.stderr)
        _print_chain_help()
        return 1

    return handler(fa)


def _parse_args(args: list) -> dict:
    """Parse --key value or --key=value pairs into a dict."""
    result: dict[str, Any] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            stripped = arg[2:]
            if "=" in stripped:
                raw_key, v = stripped.split("=", 1)
                k = raw_key.replace("-", "_")
                result[k] = v
            else:
                key = stripped.replace("-", "_")
                if i + 1 < len(args) and not args[i + 1].startswith("--"):
                    result[key] = args[i + 1]
                    i += 1
                else:
                    result[key] = True
        i += 1
    return result


def _print_chain_help() -> None:
    print("""Usage: colony chain <subcommand> [options]

Subcommands:
  status              Show chain state summary
  state               Dump full chain state as JSON
  genesis             Initialize genesis block
  info / block        Show block details (--block N)
  tx                  Show transaction (--tx-id ID)
  list-colonies       List registered colonies
  list-sentinels      List sentinel validators
  verify              Verify chain hash integrity
  mempool             Show pending mempool transactions
  register            Register this colony on-chain
  rotate-key          Rotate colony Ed25519 key on-chain
  trust               Submit trust_attest (--target NAME --level 0-4)
  untrust             Submit untrust_attest (--target NAME)

Options:
  --db PATH              SQLite database path (default: ~/.colony/chain.db)
  --name NAME            Colony name
  --key-file PATH        Ed25519 private key file (hex)
  --old-key-file PATH    Current key file (for rotate-key)
  --new-key-file PATH    New key file (for rotate-key)
  --block N              Block index for info command
  --tx-id ID             Transaction ID for tx command
  --target NAME          Target colony name or ID (trust/untrust)
  --level N              Trust level 0-4 (trust command)
  --endpoint URL         Colony endpoint URL (register)
  --description TEXT     Colony description (register)
""")
