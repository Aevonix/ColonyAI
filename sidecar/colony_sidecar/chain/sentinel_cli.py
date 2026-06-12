"""CLI commands for Sentinel management.

Usage:
    colony sentinel register   --key-file PATH --sentinel-id ID --colony-id ID
                               --host HOST --port PORT [--sentinel-key-file PATH]
    colony sentinel deregister --key-file PATH --sentinel-id ID
    colony sentinel list       Show all registered Sentinels
    colony sentinel status     Show this Sentinel's on-chain status
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path.home() / ".colony" / "chain.db"


def _load_store(db_path: str | None = None):
    from colony_sidecar.chain.storage import ChainStore
    path = Path(db_path) if db_path else _DEFAULT_DB
    return ChainStore(path)


def _load_state(db_path: str | None = None):
    from colony_sidecar.chain.state_machine import ChainStateMachine
    store = _load_store(db_path)
    sm = ChainStateMachine(store)
    return sm.get_current_state()


def _get_last_nonce(store, colony_id: str) -> int:
    last = 0
    for idx in range(store.get_height() + 1):
        block = store.get_block(idx)
        if block:
            for tx in block.transactions:
                if tx.from_colony_id == colony_id and tx.nonce > last:
                    last = tx.nonce
    return last


def cmd_sentinel_register(args) -> int:
    """Register this node as a Sentinel validator."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption
    )
    from colony_sidecar.chain.transactions import TxType, Transaction
    import hashlib

    key_file = getattr(args, "key_file", None)
    sentinel_key_file = getattr(args, "sentinel_key_file", None) or key_file
    sentinel_id = getattr(args, "sentinel_id", None) or str(uuid.uuid4())
    colony_id_arg = getattr(args, "colony_id", None)
    host = getattr(args, "host", "localhost") or "localhost"
    port = int(getattr(args, "port", 7744) or 7744)
    db_path = getattr(args, "db", None)

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
        colony_id = colony_id_arg or hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()

        # Sentinel public key (may be a separate key)
        if sentinel_key_file and Path(sentinel_key_file).exists():
            s_raw = bytes.fromhex(Path(sentinel_key_file).read_text().strip())
            s_priv = Ed25519PrivateKey.from_private_bytes(s_raw)
            sentinel_pub_hex = s_priv.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            ).hex()
        else:
            sentinel_pub_hex = pub_hex

        def sign_fn(data: bytes) -> str:
            return priv.sign(data).hex()

        store = _load_store(db_path)
        last_nonce = _get_last_nonce(store, colony_id)

        tx = Transaction.create(
            tx_type=TxType.SENTINEL_REGISTER,
            from_colony_id=colony_id,
            nonce=last_nonce + 1,
            payload={
                "sentinel_id": sentinel_id,
                "colony_id": colony_id,
                "host": host,
                "port": port,
                "public_key_hex": sentinel_pub_hex,
                "uptime_proof": {
                    "period_days": 30,
                    "uptime_percent": 100.0,
                    "attestation_tx_ids": [],
                },
                "approver_signatures": [],
                "genesis_approved": False,
            },
            sign_fn=sign_fn,
        )
        store.add_to_mempool(tx)
        print(f"Submitted sentinel_register to mempool:")
        print(f"  tx_id:       {tx.tx_id}")
        print(f"  sentinel_id: {sentinel_id}")
        print(f"  colony_id:   {colony_id[:24]}...")
        print(f"  endpoint:    {host}:{port}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_sentinel_deregister(args) -> int:
    """Step down as Sentinel."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from colony_sidecar.chain.transactions import TxType, Transaction
    import hashlib

    key_file = getattr(args, "key_file", None)
    sentinel_id = getattr(args, "sentinel_id", None)
    db_path = getattr(args, "db", None)

    if not key_file or not sentinel_id:
        print("Error: --key-file and --sentinel-id required", file=sys.stderr)
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
        last_nonce = _get_last_nonce(store, colony_id)

        tx = Transaction.create(
            tx_type=TxType.SENTINEL_DEREGISTER,
            from_colony_id=colony_id,
            nonce=last_nonce + 1,
            payload={
                "sentinel_id": sentinel_id,
                "reason": "voluntary_deregister",
                "successor_sentinel_id": None,
            },
            sign_fn=sign_fn,
        )
        store.add_to_mempool(tx)
        print(f"Submitted sentinel_deregister to mempool:")
        print(f"  tx_id:       {tx.tx_id}")
        print(f"  sentinel_id: {sentinel_id}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_sentinel_list(args) -> int:
    """Show all registered Sentinels."""
    try:
        state = _load_state(getattr(args, "db", None))
        if not state.sentinel_roster:
            print("No sentinels registered.")
            return 0
        fmt = "{:<36} {:<12} {:<24} {:<8} {}"
        print(fmt.format("SENTINEL_ID", "STATUS", "COLONY_ID (prefix)", "UPTIME%", "ENDPOINT"))
        print("-" * 90)
        for sid, rec in state.sentinel_roster.items():
            print(fmt.format(
                sid,
                rec.status,
                rec.colony_id[:24],
                f"{rec.uptime_percent:.1f}%",
                f"{rec.host}:{rec.port}",
            ))
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_sentinel_status(args) -> int:
    """Show this Sentinel's on-chain status and uptime."""
    sentinel_id = getattr(args, "sentinel_id", None)
    db_path = getattr(args, "db", None)

    try:
        state = _load_state(db_path)
        if sentinel_id:
            rec = state.sentinel_roster.get(sentinel_id)
            if rec is None:
                print(f"Sentinel {sentinel_id!r} not found in chain state.", file=sys.stderr)
                return 1
            print(f"Sentinel: {rec.sentinel_id}")
            print(f"  Status:     {rec.status}")
            print(f"  Colony:     {rec.colony_id[:32]}...")
            print(f"  Endpoint:   {rec.host}:{rec.port}")
            print(f"  Uptime:     {rec.uptime_percent:.1f}%")
            print(f"  Registered: block {rec.registered_at_height}")
            return 0
        else:
            active = state.active_sentinels()
            print(f"Active Sentinels: {len(active)}/{len(state.sentinel_roster)}")
            for rec in active:
                print(f"  {rec.sentinel_id}  {rec.host}:{rec.port}  uptime={rec.uptime_percent:.1f}%")
            return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def run_sentinel_command(args: list) -> int:
    """Dispatch 'colony sentinel <subcommand>'."""
    if not args:
        _print_sentinel_help()
        return 1

    sub = args[0]
    remaining = args[1:]

    parsed = _parse_args(remaining)

    class FakeArgs:
        pass

    fa = FakeArgs()
    for k, v in parsed.items():
        setattr(fa, k, v)

    dispatch = {
        "register": cmd_sentinel_register,
        "deregister": cmd_sentinel_deregister,
        "list": cmd_sentinel_list,
        "status": cmd_sentinel_status,
    }

    handler = dispatch.get(sub)
    if handler is None:
        print(f"Unknown sentinel subcommand: {sub!r}", file=sys.stderr)
        _print_sentinel_help()
        return 1

    return handler(fa)


def _parse_args(args: list) -> dict:
    """Parse --key value or --key=value pairs."""
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


def _print_sentinel_help() -> None:
    print("""Usage: colony sentinel <subcommand> [options]

Subcommands:
  register     Register this node as a Sentinel
  deregister   Step down as Sentinel
  list         Show all registered Sentinels
  status       Show Sentinel status and uptime

Options:
  --db PATH              Chain DB path (default: ~/.colony/chain.db)
  --key-file PATH        Colony Ed25519 private key (hex)
  --sentinel-key-file P  Sentinel-specific key (defaults to --key-file)
  --sentinel-id ID       Sentinel UUID (generated if omitted for register)
  --colony-id ID         Colony ID (derived from key if omitted)
  --host HOST            Sentinel host (default: localhost)
  --port PORT            Sentinel port (default: 7744)
""")
