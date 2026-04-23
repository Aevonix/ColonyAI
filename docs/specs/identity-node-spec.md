# Colony Identity & Node Specification

**Version:** 1.0  
**Status:** Implementing  
**Author:** DevAgent

---

## Overview

Colony identity is a two-layer system:

1. **Colony** — the logical identity. One Colony, owned by one person, persists forever.
2. **Node** — a physical device running that Colony. One Colony can have many nodes.

This spec covers everything needed for a user to start their Colony, claim their identity, run it on any number of devices, and be ready for networking/federation when it ships.

---

## Identity Hierarchy

```
Colony (colony_id + Colony Ed25519 keypair)
  │
  ├── genesis.json          — signed manifest (Genesis Colony only)
  ├── colony-id             — permanent UUID
  ├── colony-keys/
  │   ├── private.pem       — Colony private key (NEVER leaves the owner)
  │   └── public.pem        — Colony public key (shareable)
  │
  └── nodes/
      ├── node-id           — this device's UUID
      ├── node-keys/
      │   ├── private.pem   — node private key (stays on this device)
      │   └── public.pem    — node public key
      └── node-cert.json    — signed by Colony key, proves membership
```

---

## Colony Identity

### colony_id
- Random UUID, generated once at `colony init`
- Stored in `{state_dir}/colony-id`
- **Never changes** — even across key rotation or device migration
- Not derived from keys (decoupled by design)

### Colony Keypair
- Ed25519, generated at `colony init`
- Stored as PEM in `{state_dir}/colony-keys/`
- Optional passphrase encryption (`COLONY_KEY_PASSPHRASE` env var)
- Can be rotated without changing colony_id

### Colony Backup
- `colony backup` exports: colony_id + encrypted private key + Genesis manifest (if applicable)
- Single JSON file, AES-256 encrypted with user-chosen passphrase
- `colony restore` imports: interactive, guided, restores full identity
- **Restoring does NOT duplicate a node** — it restores the Colony identity so a new node can be created

---

## Node Identity

### node_id
- Random UUID, generated once on `colony start` (first run on a device)
- Stored in `{state_dir}/node-id`
- Unique per device — two nodes of the same Colony have different node_ids

### Node Keypair
- Ed25519, generated automatically on `colony start`
- Stored in `{state_dir}/node-keys/`
- Independent from Colony keypair — node keys are for device-level auth
- If node keys don't exist on `colony start`, they're generated automatically

### Node Certificate
- JSON document signed by the Colony's private key
- Binds: `{ colony_id, node_id, node_public_key, issued_at }`
- Proves: "this device belongs to this Colony"
- Created on first `colony start` after identity is initialized

```json
{
  "colony_id": "31a441ea-0191-4bfb-a87f-98a6703a6db3",
  "node_id": "a7f3c2e1-9b4d-4a8f-b6e2-d1c8f3a7e5b9",
  "node_public_key_ed25519": "a1b2c3d4...",
  "issued_at": "2026-04-20T17:35:00Z",
  "signature": "e5f6a7b8..."
}
```

**Signature** is computed over the JSON (excluding signature field) with the Colony's private key.

---

## Genesis Colony

the Genesis Colony is the trust anchor. It's special because:

1. **genesis.json** is committed to the repo — every Colony install can verify it
2. **Manifest is self-signed** — signature verifies against `GENESIS_TRUST_PUBLIC_KEY` hardcoded in source
3. **Unforgeable** — can't fake Genesis even locally without the Genesis Colony's private key
4. **The Genesis owner can run unlimited nodes** — each gets its own node_id and node cert signed by the Colony key

### Why This Is Safe
- Public key is public — it verifies signatures, can't create them
- Editing the hardcoded key in source only affects that fork, not the network
- Same model as CA root certs in browsers, Bitcoin genesis block, SSH known_hosts

---

## Lifecycle

### First Setup (Genesis)

```bash
colony init                    # Creates colony_id + Colony keypair
colony key claim-genesis       # Signs Genesis manifest with Colony private key
colony start                   # Generates node_id + node keypair + node cert
colony backup -o genesis-backup.json  # Encrypted backup for 1Password
```

### First Setup (Any Other User)

```bash
colony init                    # Creates colony_id + Colony keypair
colony start                   # Generates node_id + node keypair + node cert
colony backup -o my-backup.json     # Encrypted backup for safekeeping
```

### Adding a Node (Second Machine)

```bash
colony restore -i my-backup.json    # Restores Colony identity (interactive)
colony start                        # Generates NEW node_id + node cert for this device
```

The restore gives you the Colony identity. The `colony start` creates a unique node for that machine. Two nodes, one Colony.

### Disaster Recovery

```bash
# On a fresh machine:
colony restore                     # Interactive: asks for file + passphrase
colony start                       # New node for this device
```

Same Colony identity. New node. Previous node_ids remain valid unless revoked.

---

## CLI Commands

### Identity Management
| Command | Description |
|---|---|
| `colony init` | Create colony_id + Colony keypair (first run only) |
| `colony key info` | Show colony_id, public key, Genesis status, node info |
| `colony key generate` | Rotate Colony keypair (colony_id stays the same) |
| `colony key set-passphrase` | Encrypt Colony private key |
| `colony key manifest` | Create shareable colony manifest |
| `colony key claim-genesis` | Claim Genesis (the Genesis Colony, one-time) |

### Backup & Restore
| Command | Description |
|---|---|
| `colony backup` | Export encrypted Colony identity |
| `colony restore` | Restore Colony identity (interactive) |

### Node Management
| Command | Description |
|---|---|
| `colony node info` | Show this device's node_id + public key |
| `colony node list` | List known nodes for this Colony (when networking is live) |

---

## API Endpoints

### Existing (Updated)
```
GET /v1/host/identity/status
→ { colony_id, public_key, node_id, node_public_key, initialized, keys_configured, is_genesis }
```

### New
```
GET /v1/host/identity/node
→ { node_id, node_public_key, colony_id, certified, issued_at }

GET /v1/host/identity/certificate
→ Full node certificate JSON (for sharing with other Colonies/nodes)
```

---

## Data Files

```
{state_dir}/
├── colony-id                    # Colony UUID
├── colony-keys/
│   ├── private.pem              # Colony Ed25519 private key
│   └── public.pem               # Colony Ed25519 public key
├── node-id                      # This device's node UUID
├── node-keys/
│   ├── private.pem              # Node Ed25519 private key
│   └── public.pem               # Node Ed25519 public key
├── node-cert.json               # Node certificate (signed by Colony key)
├── genesis.json                 # Genesis manifest (the Genesis Colony only)
└── ... (other Colony state)
```

---

## Networking Readiness (Future)

This spec is the foundation. When networking ships, nodes can:

1. **Discover peers** — "I'm node X of Colony Y, here's my cert"
2. **Verify peers** — check node cert signature against Colony's public key
3. **Cluster** — nodes of the same Colony coordinate (leader election, work distribution)
4. **Federate** — Colonies trust each other by exchanging manifests
5. **Revoke nodes** — Colony signs a revocation for a compromised node_id
6. **Route messages** — addressed to colony_id, delivered to specific node_id

None of this requires changes to the identity system. The building blocks are already in place.

---

## Threat Model

| Attack | Prevention |
|---|---|
| Spoof Genesis status | Signed manifest + hardcoded trust key |
| Fake node certificate | Certificate signed by Colony private key |
| Steal Colony private key | AES-256 encrypted backup, passphrase protected |
| Duplicate Colony on many machines | Each machine gets unique node_id |
| Rogue node in Colony | Node revocation (signed by Colony key) — future |
| Fork Colony source to change trust key | Only affects that fork, not the network |

---

## Implementation Priority

### Now (Phase 2)
- [x] Colony identity (colony_id + keypair)
- [x] Genesis trust anchor (signed manifest + hardcoded key)
- [x] Backup & restore (encrypted, interactive)
- [ ] Node identity (node_id + keypair + certificate)
- [ ] `colony init` command (separate from `colony start`)
- [ ] `colony node info` command
- [ ] Updated `/identity/status` with node info

### Phase 3 (Meshing)
- [ ] Node discovery & verification
- [ ] Intra-Colony clustering (nodes of same Colony coordinate)
- [ ] Node revocation

### Phase 4 (Federation)
- [ ] Inter-Colony trust (manifest exchange)
- [ ] Colony-to-Colony message routing
- [ ] Colony key migration to Shamir-split storage

### Phase 5 (SuperColony Network)
- [ ] Network-wide discovery via Genesis trust anchor
- [ ] Hierarchical trust delegation
