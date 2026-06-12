"""CLI commands for Genesis admin operations.

Usage:
    colony admin suspend <colony-name>     Suspend a colony
    colony admin reinstate <colony-name>   Reinstate a suspended colony
    colony admin appoint-sentinel <colony-name> <host> <port>
    colony admin remove-sentinel <sentinel-id>
    colony admin upgrade --title "..." --changes <json> --activation-height N
    colony admin broadcast --message "..." --severity info
    colony admin transfer <colony-name>    Transfer genesis admin role

All commands require --key-file for the Genesis admin key.
"""

from __future__ import annotations

import json
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


def _make_genesis_admin(key_file: str, db_path: str | None = None):
    """Load Genesis admin helper from key file or Shamir shares.

    Tries Shamir share reconstruction first (from ~/.colony/shares/).
    Falls back to plaintext key file if shares are not available.
    The plaintext key file path is still accepted for backward compatibility
    but Shamir shares are the preferred mechanism.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from colony_sidecar.chain.genesis import GenesisAdmin
    import hashlib
    import json as _json

    raw = None
    colony_home = Path(key_file).parent if key_file else Path.home() / ".colony"

    # Try Shamir share reconstruction first
    shares_meta = colony_home / "shares" / "meta.json"
    if shares_meta.exists():
        try:
            from colony_sidecar.chain.keys import (
                ShamirKeyManager, KeyShareStore, LocalFileShareBackend,
            )
            meta = _json.loads(shares_meta.read_text())
            colony_id_from_meta = meta["colony_id"]
            network_id = meta["network_id"]
            k = meta["k"]

            # Derive passphrase (same derivation as setup)
            passphrase = hashlib.sha256(
                (colony_id_from_meta + network_id + "genesis-share-key").encode()
            ).digest()

            backend = LocalFileShareBackend(colony_home)
            store = KeyShareStore(colony_id_from_meta, network_id, [backend])
            shamir = ShamirKeyManager()

            collected = store.collect(k, lambda idx: passphrase)
            raw = shamir.reconstruct(collected, k)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).debug(
                "Shamir reconstruction failed, falling back to key file: %s", exc
            )

    # Fall back to plaintext key file
    if raw is None:
        if not key_file or not Path(key_file).exists():
            raise FileNotFoundError(
                f"No Shamir shares in {colony_home / 'shares'} and no key file at {key_file}"
            )
        raw = bytes.fromhex(Path(key_file).read_text().strip())

    priv = Ed25519PrivateKey.from_private_bytes(raw)
    pub = priv.public_key()
    pub_hex = pub.public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    colony_id = hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()

    # Zero the raw key material after loading
    raw_buf = bytearray(raw)
    import ctypes
    ctypes.memset(ctypes.addressof((ctypes.c_char * len(raw_buf)).from_buffer(raw_buf)), 0, len(raw_buf))

    chain_store = _load_store(db_path)

    def get_nonce(cid: str) -> int:
        last = 0
        for idx in range(chain_store.get_height() + 1):
            block = chain_store.get_block(idx)
            if block:
                for tx in block.transactions:
                    if tx.from_colony_id == cid and tx.nonce > last:
                        last = tx.nonce
        return last

    def sign_fn(data: bytes) -> str:
        return priv.sign(data).hex()

    return GenesisAdmin(colony_id, sign_fn, get_nonce), chain_store


def _check_genesis(state, colony_id: str) -> bool:
    if state.genesis_admin_id != colony_id:
        print(
            f"Error: this colony ({colony_id[:24]}...) is not the genesis admin "
            f"(genesis is {state.genesis_admin_id[:24] if state.genesis_admin_id else '(none)'}...)",
            file=sys.stderr,
        )
        return False
    return True


def cmd_admin_suspend(args) -> int:
    """Suspend a colony (genesis admin only)."""
    key_file = getattr(args, "key_file", None)
    target = getattr(args, "target", None)
    reason = getattr(args, "reason", "admin_action") or "admin_action"
    db_path = getattr(args, "db", None)

    if not key_file or not target:
        print("Error: --key-file and --target required", file=sys.stderr)
        return 1

    try:
        ga, store = _make_genesis_admin(key_file, db_path)
        state = _load_state(db_path)
        if not _check_genesis(state, ga.genesis_colony_id):
            return 1

        target_id = state.colony_id_for_name(target) or (
            target if target in state.colony_registry else None
        )
        if not target_id:
            print(f"Error: colony {target!r} not found", file=sys.stderr)
            return 1

        tx = ga.suspend_colony(target_id, reason)
        store.add_to_mempool(tx)
        print(f"Submitted colony_suspend to mempool:")
        print(f"  tx_id:  {tx.tx_id}")
        print(f"  target: {target_id[:24]}...")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_admin_reinstate(args) -> int:
    """Reinstate a suspended colony."""
    key_file = getattr(args, "key_file", None)
    target = getattr(args, "target", None)
    conditions = getattr(args, "conditions", "") or ""
    db_path = getattr(args, "db", None)

    if not key_file or not target:
        print("Error: --key-file and --target required", file=sys.stderr)
        return 1

    try:
        ga, store = _make_genesis_admin(key_file, db_path)
        state = _load_state(db_path)
        if not _check_genesis(state, ga.genesis_colony_id):
            return 1

        target_id = state.colony_id_for_name(target) or (
            target if target in state.colony_registry else None
        )
        if not target_id:
            print(f"Error: colony {target!r} not found", file=sys.stderr)
            return 1

        tx = ga.reinstate_colony(target_id, conditions=conditions)
        store.add_to_mempool(tx)
        print(f"Submitted colony_reinstate to mempool:")
        print(f"  tx_id:  {tx.tx_id}")
        print(f"  target: {target_id[:24]}...")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_admin_appoint_sentinel(args) -> int:
    """Appoint a Sentinel directly (genesis bypass of 2/3 vote)."""
    key_file = getattr(args, "key_file", None)
    colony_name = getattr(args, "target", None) or getattr(args, "colony", None)
    host = getattr(args, "host", "localhost") or "localhost"
    port = int(getattr(args, "port", 7744) or 7744)
    sentinel_pubkey = getattr(args, "sentinel_pubkey", None) or ""
    sentinel_id = getattr(args, "sentinel_id", None) or str(uuid.uuid4())
    db_path = getattr(args, "db", None)

    if not key_file or not colony_name:
        print("Error: --key-file and --target (colony name) required", file=sys.stderr)
        return 1

    try:
        ga, store = _make_genesis_admin(key_file, db_path)
        state = _load_state(db_path)
        if not _check_genesis(state, ga.genesis_colony_id):
            return 1

        colony_id = state.colony_id_for_name(colony_name) or (
            colony_name if colony_name in state.colony_registry else None
        )
        if not colony_id:
            print(f"Error: colony {colony_name!r} not found", file=sys.stderr)
            return 1

        # Use colony's active public key if no sentinel-specific key given
        if not sentinel_pubkey:
            rec = state.colony_registry.get(colony_id)
            sentinel_pubkey = rec.active_public_key_hex if rec else ""

        tx = ga.appoint_sentinel(
            sentinel_id=sentinel_id,
            colony_id=colony_id,
            host=host,
            port=port,
            public_key_hex=sentinel_pubkey,
        )
        store.add_to_mempool(tx)
        print(f"Submitted sentinel_register (genesis-appointed) to mempool:")
        print(f"  tx_id:       {tx.tx_id}")
        print(f"  sentinel_id: {sentinel_id}")
        print(f"  colony:      {colony_name} ({colony_id[:24]}...)")
        print(f"  endpoint:    {host}:{port}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_admin_remove_sentinel(args) -> int:
    """Forcibly remove a Sentinel."""
    key_file = getattr(args, "key_file", None)
    sentinel_id = getattr(args, "sentinel_id", None)
    reason = getattr(args, "reason", "admin_action") or "admin_action"
    db_path = getattr(args, "db", None)

    if not key_file or not sentinel_id:
        print("Error: --key-file and --sentinel-id required", file=sys.stderr)
        return 1

    try:
        ga, store = _make_genesis_admin(key_file, db_path)
        state = _load_state(db_path)
        if not _check_genesis(state, ga.genesis_colony_id):
            return 1

        if sentinel_id not in state.sentinel_roster:
            print(f"Error: sentinel {sentinel_id!r} not found", file=sys.stderr)
            return 1

        tx = ga.remove_sentinel(sentinel_id, reason)
        store.add_to_mempool(tx)
        print(f"Submitted sentinel_deregister to mempool:")
        print(f"  tx_id:       {tx.tx_id}")
        print(f"  sentinel_id: {sentinel_id}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_admin_upgrade(args) -> int:
    """Propose a protocol upgrade."""
    key_file = getattr(args, "key_file", None)
    title = getattr(args, "title", None)
    changes_raw = getattr(args, "changes", None) or "{}"
    activation_height = int(getattr(args, "activation_height", 0) or 0)
    description = getattr(args, "description", "") or ""
    db_path = getattr(args, "db", None)

    if not key_file or not title:
        print("Error: --key-file and --title required", file=sys.stderr)
        return 1

    try:
        # changes may be a JSON string or a file path
        if Path(changes_raw).exists():
            changes = json.loads(Path(changes_raw).read_text())
        else:
            changes = json.loads(changes_raw)
    except Exception as exc:
        print(f"Error parsing --changes: {exc}", file=sys.stderr)
        return 1

    try:
        ga, store = _make_genesis_admin(key_file, db_path)
        state = _load_state(db_path)
        if not _check_genesis(state, ga.genesis_colony_id):
            return 1

        if activation_height == 0:
            activation_height = store.get_height() + 10

        tx = ga.propose_protocol_upgrade(
            title=title,
            description=description,
            changes=changes,
            activation_height=activation_height,
        )
        store.add_to_mempool(tx)
        print(f"Submitted protocol_upgrade to mempool:")
        print(f"  tx_id:             {tx.tx_id}")
        print(f"  title:             {title}")
        print(f"  activation_height: {activation_height}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_admin_broadcast(args) -> int:
    """Broadcast a network-wide announcement."""
    key_file = getattr(args, "key_file", None)
    message = getattr(args, "message", None)
    severity = getattr(args, "severity", "info") or "info"
    db_path = getattr(args, "db", None)

    if not key_file or not message:
        print("Error: --key-file and --message required", file=sys.stderr)
        return 1

    try:
        ga, store = _make_genesis_admin(key_file, db_path)
        state = _load_state(db_path)
        if not _check_genesis(state, ga.genesis_colony_id):
            return 1

        tx = ga.broadcast_announcement(message)
        store.add_to_mempool(tx)
        print(f"Submitted broadcast announcement to mempool:")
        print(f"  tx_id:    {tx.tx_id}")
        print(f"  severity: {severity}")
        print(f"  message:  {message[:80]}")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_admin_transfer(args) -> int:
    """Transfer genesis admin role to another colony."""
    key_file = getattr(args, "key_file", None)
    target = getattr(args, "target", None)
    db_path = getattr(args, "db", None)

    if not key_file or not target:
        print("Error: --key-file and --target required", file=sys.stderr)
        return 1

    try:
        ga, store = _make_genesis_admin(key_file, db_path)
        state = _load_state(db_path)
        if not _check_genesis(state, ga.genesis_colony_id):
            return 1

        new_id = state.colony_id_for_name(target) or (
            target if target in state.colony_registry else None
        )
        if not new_id:
            print(f"Error: colony {target!r} not found", file=sys.stderr)
            return 1

        new_rec = state.colony_registry[new_id]
        tx = ga.transfer_admin(new_id, new_rec.active_public_key_hex)
        store.add_to_mempool(tx)
        print(f"Submitted transfer_genesis_admin to mempool:")
        print(f"  tx_id:     {tx.tx_id}")
        print(f"  new_admin: {target} ({new_id[:24]}...)")
        print(f"WARNING: Once committed, {ga.genesis_colony_id[:24]}... loses all admin privileges.")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def run_admin_command(args: list) -> int:
    """Dispatch 'colony admin <subcommand>'."""
    if not args:
        _print_admin_help()
        return 1

    sub = args[0]
    remaining = args[1:]

    # Positional arg support: sub-command may have a positional target
    # e.g. "colony admin suspend my-colony" → target = "my-colony"
    pos_args = [a for a in remaining if not a.startswith("--")]
    parsed = _parse_args([a for a in remaining if a.startswith("--")])
    if pos_args and "target" not in parsed:
        parsed["target"] = pos_args[0]

    class FakeArgs:
        pass

    fa = FakeArgs()
    for k, v in parsed.items():
        setattr(fa, k, v)

    dispatch = {
        "suspend": cmd_admin_suspend,
        "reinstate": cmd_admin_reinstate,
        "appoint-sentinel": cmd_admin_appoint_sentinel,
        "appoint_sentinel": cmd_admin_appoint_sentinel,
        "remove-sentinel": cmd_admin_remove_sentinel,
        "remove_sentinel": cmd_admin_remove_sentinel,
        "upgrade": cmd_admin_upgrade,
        "broadcast": cmd_admin_broadcast,
        "transfer": cmd_admin_transfer,
    }

    handler = dispatch.get(sub)
    if handler is None:
        print(f"Unknown admin subcommand: {sub!r}", file=sys.stderr)
        _print_admin_help()
        return 1

    return handler(fa)


def _parse_args(args: list) -> dict:
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


def _print_admin_help() -> None:
    print("""Usage: colony admin <subcommand> [options]

Requires genesis admin privileges (--key-file must be the genesis admin key).

Subcommands:
  suspend <colony>           Suspend a colony
  reinstate <colony>         Reinstate a suspended colony
  appoint-sentinel <colony> --host HOST --port PORT
                             Appoint a Sentinel (bypasses 2/3 vote)
  remove-sentinel --sentinel-id ID
                             Forcibly remove a Sentinel
  upgrade --title "..." --changes <json> --activation-height N
                             Propose a protocol upgrade
  broadcast --message "..." [--severity info|warning|critical]
                             Broadcast network announcement
  transfer <colony>          Transfer genesis admin role

Options:
  --db PATH              Chain DB path (default: ~/.colony/chain.db)
  --key-file PATH        Genesis admin Ed25519 private key (hex)
  --target NAME          Target colony name or ID
  --sentinel-id ID       Sentinel UUID
  --sentinel-pubkey HEX  Sentinel public key (optional, defaults to colony key)
  --host HOST            Sentinel host
  --port PORT            Sentinel port (default: 7744)
  --title TEXT           Protocol upgrade title
  --changes JSON         JSON object with config changes
  --activation-height N  Block height when upgrade activates
  --description TEXT     Optional upgrade description
  --message TEXT         Broadcast message content
  --severity LEVEL       Broadcast severity: info|warning|critical
  --conditions TEXT      Reinstatement conditions
  --reason TEXT          Suspension reason
""")
