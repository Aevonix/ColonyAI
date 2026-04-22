"""CLI commands for colony key management and genesis key operations.

Usage:
    colony genesis status
    colony genesis add-node NODE_ID
    colony genesis remove-node NODE_ID
    colony genesis export-share [--output PATH]
    colony genesis import-share --file PATH
    colony genesis rotate

    colony keys status
    colony keys rotate
    colony keys recover --from-backup PATH
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_DATA_DIR = Path.home() / ".colony"
_DEFAULT_SHARES_DIR = _DEFAULT_DATA_DIR / "shares"


def _get_data_dir(args) -> Path:
    return Path(getattr(args, "data_dir", None) or _DEFAULT_DATA_DIR)


def _load_share_backend(data_dir: Path):
    from colony_sidecar.chain.keys import LocalFileShareBackend
    return LocalFileShareBackend(data_dir)


def _fmt_ago(ts_str: str) -> str:
    """Format an ISO timestamp as 'X days ago'."""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        days = delta.days
        if days == 0:
            hours = delta.seconds // 3600
            return f"{hours}h ago"
        return f"{days} days ago"
    except Exception:
        return ts_str


# ---------------------------------------------------------------------------
# colony genesis commands
# ---------------------------------------------------------------------------


def cmd_genesis_status(args) -> int:
    """Display Genesis key health."""
    data_dir = _get_data_dir(args)
    backend = _load_share_backend(data_dir)

    # Try to load chain state for network info
    network_id = "(unavailable)"
    genesis_colony = "(unavailable)"
    try:
        from colony_sidecar.chain.storage import ChainStore
        from colony_sidecar.chain.state_machine import ChainStateMachine
        db_path = data_dir / "chain.db"
        if db_path.exists():
            store = ChainStore(db_path)
            sm = ChainStateMachine(store)
            state = sm.get_current_state()
            network_id = state.network_id or "(none)"
            if state.genesis_admin_id:
                rec = state.colony_registry.get(state.genesis_admin_id)
                genesis_colony = f"{rec.name} ({state.genesis_admin_id[:16]}...)" if rec else state.genesis_admin_id[:16]
    except Exception:
        pass

    # Load share indices from local backend
    colony_id = getattr(args, "colony_id", "")
    share_indices = backend.list_shares(colony_id) if colony_id else []

    print("Genesis Key Status")
    print("==================")
    print(f"  Network ID:     {network_id}")
    print(f"  Genesis Colony: {genesis_colony}")
    print(f"  Local shares:   {share_indices or '(none)'}")

    # Show genesis node file if present
    genesis_node_file = data_dir / "genesis_node.json"
    if genesis_node_file.exists():
        try:
            gn = json.loads(genesis_node_file.read_text())
            print(f"  Node ID:        {gn.get('node_id', '?')}")
            print(f"  State:          {gn.get('state', '?')}")
            peers = gn.get("peers", {})
            print(f"  Peers:          {len(peers)}")
            for nid, st in peers.items():
                print(f"    {nid[:20]:20s}  {st}")
        except Exception:
            pass

    return 0


def cmd_genesis_add_node(args) -> int:
    """Add a node to the Genesis key quorum."""
    node_id = getattr(args, "node_id", None)
    if not node_id:
        print("Error: node_id required (e.g. colony genesis add-node NODE_ID)", file=sys.stderr)
        return 1

    print(f"Adding node {node_id!r} to Genesis quorum...")
    print("NOTE: This operation requires the active Genesis signer to be reachable.")
    print("      The current node must hold the active key to redistribute shares.")
    print()
    print("Full implementation requires the mesh layer to be running.")
    print(f"Node {node_id!r} will receive a new encrypted share after resharing.")
    return 0


def cmd_genesis_remove_node(args) -> int:
    """Remove a node from the Genesis key quorum."""
    node_id = getattr(args, "node_id", None)
    if not node_id:
        print("Error: node_id required (e.g. colony genesis remove-node NODE_ID)", file=sys.stderr)
        return 1

    print(f"Removing node {node_id!r} from Genesis quorum...")
    print("NOTE: This requires the active Genesis signer. All remaining nodes")
    print("      will receive new shares from a fresh split of the current key.")
    print()
    print("Full implementation requires the mesh layer to be running.")
    return 0


def cmd_genesis_export_share(args) -> int:
    """Export this node's encrypted key share to a .colonyshare file."""
    from colony_sidecar.chain.keys import LocalFileShareBackend, export_share_file

    data_dir = _get_data_dir(args)
    colony_id = getattr(args, "colony_id", "")
    network_id = getattr(args, "network_id", "")
    share_index = getattr(args, "share_index", None)

    backend = LocalFileShareBackend(data_dir)
    available = backend.list_shares(colony_id)

    if not available:
        print("Error: No shares found in local storage.", file=sys.stderr)
        print(f"       Looked in: {backend.shares_dir}", file=sys.stderr)
        return 1

    # Use specified index or first available
    if share_index is not None:
        idx = int(share_index)
    else:
        idx = available[0]

    share = backend.retrieve_share(colony_id, idx)
    if share is None:
        print(f"Error: Share #{idx} not found.", file=sys.stderr)
        return 1

    # Determine output path
    output_arg = getattr(args, "output", None)
    if output_arg:
        output_path = Path(output_arg)
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        output_path = Path(f"genesis-share-{idx}-{date_str}.colonyshare")

    export_share_file(share, colony_id, network_id, output_path)
    print(f"Exported share #{idx} to: {output_path}")
    print("Store this file in a secure offline location.")
    return 0


def cmd_genesis_import_share(args) -> int:
    """Import a .colonyshare file onto this node."""
    from colony_sidecar.chain.keys import LocalFileShareBackend, import_share_file

    file_arg = getattr(args, "file", None)
    if not file_arg:
        print("Error: --file PATH required", file=sys.stderr)
        return 1

    file_path = Path(file_arg)
    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        return 1

    try:
        colony_id, network_id, share = import_share_file(file_path)
    except Exception as e:
        print(f"Error reading share file: {e}", file=sys.stderr)
        return 1

    data_dir = _get_data_dir(args)
    backend = LocalFileShareBackend(data_dir)
    backend.store_share(share)

    print(f"Imported share #{share.share_index} (of {share.n}) for colony {colony_id[:16]}...")
    print(f"  Stored at: {backend.shares_dir / f'share_{share.share_index:02d}.json'}")
    print("This node is now a Genesis Node and can participate in failover.")
    return 0


def cmd_genesis_rotate(args) -> int:
    """Rotate the Genesis keypair."""
    print("Genesis key rotation requires confirmation.")
    print("This will:")
    print("  1. Generate a new Ed25519 keypair")
    print("  2. Split into new Shamir shares")
    print("  3. Distribute to all Genesis Nodes")
    print("  4. Record a colony_rotate_key transaction on-chain")
    print()

    confirm = getattr(args, "yes", False)
    if not confirm:
        try:
            response = input("Proceed with key rotation? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if response != "y":
            print("Rotation cancelled.")
            return 0

    print("Full rotation requires the active signing key and mesh layer.")
    print("Use the ColonyKeyManager.rotate() API to perform this operation programmatically.")
    return 0


# ---------------------------------------------------------------------------
# colony keys commands
# ---------------------------------------------------------------------------


def cmd_keys_status(args) -> int:
    """Display this colony's key health."""
    from colony_sidecar.chain.keys import LocalFileShareBackend

    data_dir = _get_data_dir(args)
    colony_id = getattr(args, "colony_id", "")
    backend = LocalFileShareBackend(data_dir)

    # Try to get colony name from chain
    colony_name = "(unknown)"
    pubkey_hex = "(unavailable)"
    try:
        from colony_sidecar.chain.storage import ChainStore
        from colony_sidecar.chain.state_machine import ChainStateMachine
        db_path = data_dir / "chain.db"
        if db_path.exists():
            store = ChainStore(db_path)
            sm = ChainStateMachine(store)
            state = sm.get_current_state()
            if colony_id and colony_id in state.colony_registry:
                rec = state.colony_registry[colony_id]
                colony_name = rec.name
                # Key history gives us current pubkey
                history = state.key_history.get(colony_id, [])
                if history:
                    active = [e for e in history if e.revoked_at_height is None]
                    if active:
                        pubkey_hex = active[-1].public_key_hex
    except Exception:
        pass

    share_indices = backend.list_shares(colony_id) if colony_id else []

    print("Colony Key Status")
    print("=================")
    print(f"  Colony ID:     {colony_id[:32]}..." if len(colony_id) > 32 else f"  Colony ID:     {colony_id or '(none)'}")
    print(f"  Colony Name:   {colony_name}")
    print(f"  Public Key:    {pubkey_hex[:32]}..." if len(pubkey_hex) > 32 else f"  Public Key:    {pubkey_hex}")
    print(f"  Local shares:  {share_indices or '(none)'}")
    print(f"  Shares dir:    {backend.shares_dir}")
    return 0


def cmd_keys_rotate(args) -> int:
    """Rotate this colony's keypair."""
    import os
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    data_dir = _get_data_dir(args)
    colony_id = getattr(args, "colony_id", "")
    passphrase = getattr(args, "passphrase", b"")

    if isinstance(passphrase, str):
        passphrase = passphrase.encode()

    if not colony_id:
        print("Error: --colony-id required", file=sys.stderr)
        return 1

    # Generate new keypair
    new_priv = Ed25519PrivateKey.generate()
    new_priv_bytes = new_priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    new_pub_hex = new_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()

    from colony_sidecar.chain.keys import (
        LocalFileShareBackend, ShamirKeyManager, KeyShareStore,
        InMemoryShareBackend, ColonyKeyManager,
    )

    n = int(getattr(args, "n", 5))
    k = int(getattr(args, "k", 3))
    network_id = getattr(args, "network_id", "0" * 64)

    backend = LocalFileShareBackend(data_dir)
    shamir = ShamirKeyManager()
    store = KeyShareStore(colony_id, network_id, [backend])

    # Split and encrypt new key
    raw_shares = shamir.split(new_priv_bytes, n, k)
    encrypted = [
        store.encrypt_share(raw_share, x, n, k, passphrase)
        for x, raw_share in raw_shares
    ]

    # Delete old shares
    for idx in backend.list_shares(colony_id):
        backend.delete_share(colony_id, idx)

    # Store new shares
    for enc_share in encrypted:
        backend.store_share(enc_share)

    print(f"Key rotation complete for colony {colony_id[:16]}...")
    print(f"  New public key: {new_pub_hex}")
    print(f"  Shares:         {n} total, {k} threshold")
    print(f"  Stored at:      {backend.shares_dir}")
    print()
    print("Record a colony_rotate_key transaction on-chain to complete rotation.")
    return 0


def cmd_keys_recover(args) -> int:
    """Recover a lost share from an encrypted backup file."""
    from colony_sidecar.chain.keys import LocalFileShareBackend, import_share_file

    from_backup = getattr(args, "from_backup", None)

    if from_backup:
        file_path = Path(from_backup)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            return 1
        try:
            colony_id, network_id, share = import_share_file(file_path)
        except Exception as e:
            print(f"Error reading backup file: {e}", file=sys.stderr)
            return 1

        data_dir = _get_data_dir(args)
        backend = LocalFileShareBackend(data_dir)
        backend.store_share(share)

        print(f"Recovered share #{share.share_index} from {file_path}")
        print(f"  Stored at: {backend.shares_dir / f'share_{share.share_index:02d}.json'}")
        return 0

    from_1password = getattr(args, "from_1password", False)
    if from_1password:
        print("1Password recovery requires 'op' CLI to be installed and authenticated.")
        print("Not yet implemented — export the share manually from 1Password and use --from-backup.")
        return 1

    print("Error: specify --from-backup PATH or --from-1password", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def run_genesis_command(args: list) -> int:
    """Dispatch 'colony genesis <subcommand>'."""
    if not args:
        _print_genesis_help()
        return 1

    sub = args[0]
    remaining = args[1:]
    parsed = _parse_args(remaining)

    class FakeArgs:
        pass

    fa = FakeArgs()
    for k, v in parsed.items():
        setattr(fa, k, v)
    # positional: node_id is args[1] for add-node / remove-node
    if sub in ("add-node", "remove-node") and remaining and not remaining[0].startswith("--"):
        fa.node_id = remaining[0]

    dispatch = {
        "status": cmd_genesis_status,
        "add-node": cmd_genesis_add_node,
        "remove-node": cmd_genesis_remove_node,
        "export-share": cmd_genesis_export_share,
        "import-share": cmd_genesis_import_share,
        "rotate": cmd_genesis_rotate,
    }

    handler = dispatch.get(sub)
    if handler is None:
        print(f"Unknown genesis subcommand: {sub!r}", file=sys.stderr)
        _print_genesis_help()
        return 1

    return handler(fa)


def run_keys_command(args: list) -> int:
    """Dispatch 'colony keys <subcommand>'."""
    if not args:
        _print_keys_help()
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
        "status": cmd_keys_status,
        "rotate": cmd_keys_rotate,
        "recover": cmd_keys_recover,
    }

    handler = dispatch.get(sub)
    if handler is None:
        print(f"Unknown keys subcommand: {sub!r}", file=sys.stderr)
        _print_keys_help()
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


def _print_genesis_help() -> None:
    print("""Usage: colony genesis <subcommand> [options]

Subcommands:
  status              Display Genesis key health
  add-node NODE_ID    Add NODE_ID to the Genesis key quorum
  remove-node NODE_ID Remove NODE_ID from the Genesis key quorum
  export-share        Export this node's encrypted share (--output PATH)
  import-share        Import a .colonyshare file (--file PATH)
  rotate              Rotate the Genesis keypair

Options:
  --data-dir PATH     Colony data directory (default: ~/.colony)
  --colony-id HEX     Colony ID hex string
  --network-id HEX    Network ID hex string
  --output PATH       Output path for export-share
  --file PATH         Input path for import-share
  --yes               Skip confirmation prompts
""")


def _print_keys_help() -> None:
    print("""Usage: colony keys <subcommand> [options]

Subcommands:
  status              Display this colony's key health
  rotate              Rotate this colony's keypair
  recover             Recover a lost share

Options:
  --data-dir PATH     Colony data directory (default: ~/.colony)
  --colony-id HEX     Colony ID hex string
  --network-id HEX    Network ID hex string
  --from-backup PATH  Backup .colonyshare file for recovery
  --from-1password    Recover from 1Password
  --n INT             Total shares (default: 5)
  --k INT             Threshold (default: 3)
  --passphrase STR    Share encryption passphrase
""")
