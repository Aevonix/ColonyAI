# Colony Channel Framework

End-to-end design for generic channel registration, cross-channel context
sharing, and turnkey agent deployment with backup/restore.

## Problem

Colony receives messages from multiple channels (RCS, WhatsApp, SMS, voice,
companion apps, webhooks) but has no formal channel registration system.
The current state:

- `channel_id` is never populated by hosts -- context provenance and
  cross-context leak detection are completely dead
- Contact handle gateways are constrained to a hardcoded SQL CHECK enum
- Phone-number unification logic is hardcoded to a fixed gateway set
- Companion apps (terminals, kiosks, voice bots) all appear as the same
  generic `api_server` or `cli` gateway -- Colony cannot distinguish them
- Session keying is per-contact (not per-contact-per-channel), which is
  correct for unified context but invisible to the provenance system
- Outbound delivery scans a hardcoded set of platform env vars
- No full-state backup/restore exists -- only identity keys are portable
- Deploying a new agent persona requires manually wiring services,
  databases, tunnels, and config files with no automation or manifest

## Goals

1. Any channel can self-register with Colony, declaring its capabilities
2. Cross-channel context sharing works automatically with no per-channel config
3. Context provenance and cross-context guards work without host cooperation
4. A new agent deployment is: install Hermes, install Colony, install persona
   layer, restore from backup -- four steps, fully automated
5. Everything in Colony stays generic -- no persona-specific code

---

## Terminology

Terms used consistently throughout this spec:

| Term | Meaning |
|------|---------|
| **channel_key** | Unique identifier for a registered channel (e.g., `"whatsapp"`, `"terminal-01"`). Primary key in the channel store. |
| **channel_id** | Per-conversation identifier sent by the host on each turn (e.g., `"whatsapp:120363425135486141@g.us"`). Used for provenance tracking. Auto-derived from `gateway:contact_id` when the host does not provide one. |
| **gateway** | The transport a contact handle is associated with (e.g., `"sms"`, `"whatsapp"`). Stored on contact handles. Maps to a registered channel's `channel_key`. |
| **gateway_family** | Grouping of related gateways (e.g., `"phone"` for sms/whatsapp/signal). Used for policy, not routing. |
| **platform** | Host-framework term for a messaging adapter (e.g., Hermes platform). Colony receives this as the `gateway` value. |

---

## Part 1: Channel Registration Protocol

### 1.1 Channel Manifest

Every channel declares itself to Colony via a manifest, either at startup
(registration API) or via static config. Validated as a Pydantic model.

```python
class ChannelManifest(BaseModel):
    channel_key: str           # unique key: "whatsapp", "rcs", "voice", "terminal-01"
    display_name: str          # human label: "WhatsApp", "Office Terminal"
    gateway_family: str        # grouping: "phone", "messaging", "companion", "api"

    # Capabilities
    supports_media: bool = False
    supports_reactions: bool = False
    supports_voice: bool = False
    supports_rich_text: bool = False
    max_message_length: int | None = None

    # Identity policies
    phone_identity_unification: bool = False   # participates in phone-number merging
    session_isolation: bool = False            # True = per-channel sessions (deferred, see 3.3)
    provides_channel_id: bool = False           # host will send channel_id

    # Delivery
    delivery_webhook: str | None = None        # URL for push delivery (validated, see 1.6)
    delivery_protocol: str = "hermes"          # "hermes" | "webhook" | "direct"
    delivery_aliases: list[str] = []           # gateways this channel can deliver for
                                               # e.g., ["sms", "imessage"] on a whatsapp channel
    home_chat_id: str | None = None            # chat ID for home/broadcast channel

    # Metadata
    platform_hint: str = ""    # injected into system prompt when active
    pii_safe: bool = True      # safe for PII in responses
```

### 1.2 Registration API

All channel endpoints require the Colony API key (`COLONY_API_KEY` header
or `Authorization: Bearer <key>`), same as other `/v1/host/` endpoints.

```
POST /v1/channels/register
{
  "channel_key": "terminal-office",
  "display_name": "Office Terminal",
  "gateway_family": "companion",
  "supports_media": true,
  "phone_identity_unification": false,
  "provides_channel_id": true,
  "delivery_webhook": "http://localhost:8775/deliver"
}

Response: 201 Created
{
  "channel_key": "terminal-office",
  "registered_at": "2026-06-27T...",
  "channel_token": "ch_..."   // required for subsequent updates/deletes
}
```

**Conflict policy**: If a `channel_key` already exists:
- Same `channel_token` in the request header: update (upsert). Returns 200.
- No token or wrong token: reject with 409 Conflict.
- `DELETE` requires the original `channel_token`.

This prevents a rogue process from hijacking an existing channel
registration. The `channel_token` is generated on first registration
and must be stored by the registering service.

```
GET /v1/channels                   -- list registered channels
GET /v1/channels/{key}             -- get single channel manifest
PUT /v1/channels/{key}             -- update (requires channel_token)
DELETE /v1/channels/{key}          -- unregister (requires channel_token)
```

### 1.3 Storage

Registered channels stored in `colony-channels.db` inside
`COLONY_STATE_DIR` (so `colony backup --full` auto-discovers it):

```sql
CREATE TABLE channels (
    channel_key    TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    gateway_family TEXT NOT NULL,
    manifest_json  TEXT NOT NULL,    -- full ChannelManifest as JSON
    channel_token  TEXT NOT NULL,    -- HMAC token for update/delete auth
    registered_at  TEXT NOT NULL,
    last_seen_at   TEXT,
    status         TEXT DEFAULT 'active'  -- active | inactive | revoked
);
```

### 1.4 Removing the Hardcoded Gateway Enum

The contact handle store currently has:

```sql
CHECK(gateway IN ('imessage','telegram','whatsapp','discord','slack','email','sms','signal','custom'))
```

Replace with open-ended validation:
- Remove the SQL CHECK constraint via table rebuild migration
- Validate against registered channels at the application layer
- Accept any gateway string; warn on unregistered gateways
- Keep `custom` as a catch-all for unregistered gateways

**Migration**: SQLite has no `ALTER TABLE DROP CONSTRAINT`. The migration
must rebuild the table:

```sql
-- 003_open_gateway_enum.sql
CREATE TABLE contact_handles_new (
    id         TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    gateway    TEXT NOT NULL,
    address    TEXT NOT NULL,
    label      TEXT,
    is_primary INTEGER DEFAULT 0,
    verified   INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(gateway, address)
);
INSERT INTO contact_handles_new SELECT * FROM contact_handles;
DROP TABLE contact_handles;
ALTER TABLE contact_handles_new RENAME TO contact_handles;
```

**Migration tracking**: Colony currently uses numbered `.sql` files with
no schema version table. Phase 1 adds a `schema_version` table to each
database and a migration runner that applies unapplied migrations
idempotently on startup.

### 1.5 Phone Identity Unification

Currently hardcoded in **three** locations (all must be updated):
- `contacts/store.py:60` -- `_PHONE_GATEWAYS = ("imessage", "sms", "signal", "whatsapp")`
- `identity/resolver.py:38` -- `_PHONE_GATEWAYS = ("imessage", "sms", "signal")`
- `contacts/world_bridge.py:19` -- `_PHONE_GATEWAYS = ("imessage", "sms", "signal")`

Replace all three with a single query against registered channels:

```python
def get_phone_gateways(channel_store: ChannelStore) -> set[str]:
    registered = {c.channel_key for c in channel_store.list_active()
                  if c.phone_identity_unification}
    # Fallback for fresh installs before any channels are registered
    return registered or {"imessage", "sms", "signal", "whatsapp"}
```

The fallback set ensures phone-number unification works on a fresh
install before any host has registered channels.

Channels declare `phone_identity_unification: true` in their manifest.
The last-10-digit normalization logic stays -- it is applied to any gateway
in the phone-unification set.

### 1.6 Channel Health and Observability

`colony doctor` gains channel-related checks:

- **Registered channels**: lists all channels with status, `last_seen_at`,
  and whether the webhook is reachable (HTTP HEAD probe)
- **Stale channels**: warns if `last_seen_at` is older than 24 hours
- **Orphaned channels**: warns if a channel is registered but its
  `gateway` never appears in any contact handle

Channel status transitions:
- `active`: Normal. Set on registration and on any turn received.
- `inactive`: Set automatically when `last_seen_at` exceeds a
  configurable threshold (default 7 days). Inactive channels are
  deprioritized in delivery resolution but not removed.
- `revoked`: Set by explicit `DELETE` call. Channel is ignored by all
  subsystems. Can be re-registered with a new token.

`last_seen_at` is updated whenever Colony processes a turn whose
resolved gateway matches the channel's `channel_key`.

### 1.7 Webhook Validation

`delivery_webhook` URLs are validated on registration:
- Must be a valid HTTP(S) URL
- Private/link-local IPs (`10.x`, `172.16-31.x`, `192.168.x`,
  `169.254.x`, `127.x`, `::1`, `fd00::/8`) are rejected unless
  `COLONY_ALLOW_PRIVATE_WEBHOOKS=true` is set (for same-machine
  deployments)
- Colony signs webhook payloads with an HMAC using the channel's
  `channel_token`, sent in the `X-Colony-Signature` header. Receivers
  should verify this signature.

This prevents SSRF via malicious channel registrations while allowing
legitimate local-network deployments to opt in.

---

## Part 2: Automatic channel_id Derivation

### 2.1 The Problem

`HostTurnContext.channel_id` is optional and currently always None because
Hermes does not send it. This kills:
- Context provenance recording (hard-gated on `channel_id` being truthy)
- Cross-context leak detection (queries the empty provenance store)

### 2.2 Solution: Auto-derive When Not Provided

In the turn processing pipeline (`host.py`), after receiving `HostTurnContext`:

```python
def _ensure_channel_id(ctx: HostTurnContext) -> str:
    if ctx.channel_id:
        return ctx.channel_id

    # Derive a stable synthetic key from gateway + contact
    gateway = _resolve_gateway(ctx)  # from session, contact, or "unknown"
    contact = ctx.contact_id or "anonymous"
    return f"{gateway}:{contact}"
```

This gives every conversation a stable `channel_id` even when the host
does not provide one. The synthetic key groups all turns from the same
contact on the same gateway into one provenance bucket -- which is the
correct granularity for cross-context leak detection.

### 2.3 Gateway Resolution

The `gateway` for derivation comes from (in priority order):
1. The active session's `gateway` field (set at session creation)
2. The contact's primary handle gateway
3. The `HostIdentity.host_id` field (e.g., "hermes")
4. `"unknown"`

### 2.4 Host-side Enhancement (Hermes)

For hosts that can provide a real `channel_id`, the auto-derivation is
bypassed. Two changes needed in Hermes:

**A) Pass `chat_id` to plugin hooks:**

In `agent/turn_context.py` and `agent/turn_finalizer.py`, add `chat_id`
to the kwargs passed to `pre_llm_call` and `post_llm_call`:

```python
# turn_context.py, in pre_llm_call invocation
plugin_mgr.invoke_hook("pre_llm_call",
    session_id=..., task_id=..., turn_id=...,
    user_message=..., conversation_history=...,
    is_first_turn=..., model=...,
    platform=agent.platform,
    sender_id=agent._user_id,
    chat_id=agent.chat_id,       # NEW
)
```

**B) Colony plugin sends `channel_id`:**

In the Colony plugin's `post_llm_call` hook, include channel_id in the
turn sync payload:

```python
# In sync_turn call
channel_id = f"{_session_state.get('platform', 'unknown')}:{_session_state.get('chat_id', 'direct')}"
```

**C) `hermes -z --platform` flag:**

Add a `--platform` argument to one-shot mode so companion apps can
identify themselves distinctly:

```
hermes -z --platform terminal-office "What's on my calendar?"
hermes -z --platform voice-bot "Summarize the last meeting"
```

This passes through to `AIAgent(platform=args.platform)` instead of
hardcoding `"cli"`.

---

## Part 3: Cross-Channel Context Architecture

### 3.1 What Already Works (No Changes Needed)

- **Unified memory store**: Neo4j graph stores all memories without channel
  partitioning. Context assembly ranks by semantic relevance, not filtered
  by channel. A fact learned via WhatsApp is available in a Terminal session.

- **Contact unification**: A single `contact_id` spans all channels.
  Handle resolution merges phone-based identities across gateways.

- **Session-scoped entity tracking**: `IsolatedSession.mentioned_entities`
  tracks what entities were discussed, enabling cross-context guards.

- **Comms governance**: The comms ledger tracks communication frequency
  per contact across all channels, preventing over-contact.

### 3.2 What Gets Fixed by Parts 1-2

- **Context provenance**: With auto-derived `channel_id`, every turn is
  indexed by `(entity, gateway:contact)`. The provenance guard can now
  detect when an entity mentioned in a WhatsApp conversation leaks into
  an RCS conversation with a different contact.

- **Channel-aware delivery**: The ChannelRegistry resolves outbound
  channels from registered channel manifests instead of scanning hardcoded
  env vars.

- **Companion app identity**: Terminal, kiosk, voice bot each register as
  distinct channels. Colony can track which topics were discussed on which
  surface and apply appropriate guards.

### 3.3 What Does NOT Change

- **No channel-scoped memory**: Memories remain globally searchable. This
  is intentional -- the value of Colony is unified context. Channel-scoping
  would fragment the agent's knowledge.

- **One session per contact**: Sessions remain per-contact, not
  per-contact-per-channel. Switching from WhatsApp to Terminal mid-topic
  should feel seamless. The `session_isolation` manifest flag is reserved
  for future use (e.g., a public kiosk that should not share session state
  with private channels). The session-forking mechanism is deferred --
  `session_isolation` is stored in the manifest but not acted upon until
  a future phase defines how isolated sessions are created and routed.

- **No channel-based access control**: Trust is per-contact, not
  per-channel. A trusted contact is trusted regardless of which channel
  they use. Channel-level restrictions (e.g., "no PII on public kiosk")
  are handled by the ResponseGuard's `pii_safe` check against the channel
  manifest.

---

## Part 4: ChannelRegistry Overhaul

### 4.1 Current State

`ChannelRegistry` resolves outbound delivery targets via a 4-layer cascade:
env vars, JSON config, contact handle inference, home channel env vars.
The scanning logic is hardcoded to specific platform names.

### 4.2 New Design

ChannelRegistry reads from the channel registration database and merges
with config overrides:

```python
class ChannelRegistry:
    def __init__(self, channel_store, config_overrides=None):
        self._store = channel_store  # colony-channels.db
        self._overrides = config_overrides or {}

    def resolve_dm(self, contact_id: str) -> Channel | None:
        # 1. Config override (env var or JSON)
        # 2. Contact's preferred handle, mapped through registered channels
        # 3. Contact's primary handle gateway -> registered channel
        ...

    def resolve_home(self) -> Channel | None:
        # 1. Config override: COLONY_HOME_CHANNEL=platform:chat_id
        # 2. First registered channel with a home_chat_id in its manifest
        ...

    def list_channels(self) -> list[ChannelManifest]:
        return self._store.list_active()
```

**Delivery resolution priority** (for a contact with handle gateway "sms"):
1. A registered channel whose `channel_key` exactly matches "sms" (direct)
2. A registered channel whose `delivery_aliases` includes "sms" (indirect).
   If multiple channels claim the same alias, the one registered first wins.
3. `DEFAULT_GATEWAY_MAP` fallback (static, for fresh installs)

Direct match always beats alias match. This means if a real SMS channel
registers as `channel_key: "sms"`, it takes precedence over a WhatsApp
channel that claims `delivery_aliases: ["sms"]`.

**Multi-host scenarios**: Multiple host frameworks can register channels
with the same Colony instance. Channel key uniqueness is enforced --
two hosts cannot both register `channel_key: "whatsapp"`. If they need
to, they should use distinct keys (e.g., `"whatsapp-hermes"`,
`"whatsapp-bot2"`).

**Fallback for fresh installs**: If no channels are registered yet
(before any host connects), the current `DEFAULT_GATEWAY_MAP` is used
as a static fallback. Once any channel registers, the dynamic lookup
takes precedence for that gateway. This ensures DM inference works
immediately after `colony init` without requiring a host to be running.

---

## Part 5: Full-State Backup and Restore

### 5.1 Current Gap

`colony backup` only exports identity keys (Ed25519 keypair + colony-id).
A full restore requires manually copying ~17 SQLite databases, the Neo4j
graph, the LanceDB vector store, and all config files.

### 5.2 `colony backup --full`

Produces an encrypted archive containing all portable state. Encryption
is on by default (AES-256-GCM with Argon2id-derived key from passphrase).
Use `--no-encrypt` to produce a plaintext archive (not recommended).

```
colony-backup-{colony_id}-{timestamp}.tar.gz.enc
  /identity/
    colony-id
    colony-keys/private.pem
    colony-keys/public.pem
    genesis.json
  /databases/
    *.db  (all SQLite databases discovered in COLONY_STATE_DIR)
  /vector/
    lancedb/  (full directory)
  /graph/
    neo4j-dump.cypher  (or neo4j-admin dump binary)
  /config/
    .env (scrubbed -- see 5.7)
    channels.json
    .colony-llm-config.json
    standing_approvals.json
  /events/
    *.json (event log)
  /meta.json
    backup_version, colony_version, timestamp, colony_id,
    colony_id_hmac (binds backup to this identity),
    database_manifest (list of included .db files with schemas)
```

Database discovery is automatic -- the backup command walks
`COLONY_STATE_DIR` and includes every `.db` file it finds. No hardcoded
database list; new subsystems that add databases are automatically
included.

```bash
# Create backup (encrypted by default, prompts for passphrase)
colony backup --full --output ~/backups/
colony backup --full --passphrase-file ~/.colony/backup-key --output ~/backups/

# Plaintext backup (not recommended)
colony backup --full --no-encrypt --output ~/backups/

# Restore on new machine
colony restore --full colony-backup-cid1234-20260627.tar.gz.enc
# Runs: identity restore + copy DBs + import graph + rebuild vector index
```

**Identity binding**: The archive header includes an HMAC of the
colony-id. On restore, Colony verifies the backup matches the current
identity (or the machine has no identity yet). Restoring a backup onto
a Colony instance with a different identity requires explicit
`--force-identity` to prevent accidental identity cloning.

### 5.3 Backup Atomicity

Backups run while the sidecar is live. Consistency is ensured by:

- **SQLite**: Use `VACUUM INTO '<temp_path>'` (SQLite 3.27+) for each
  database. This creates a consistent snapshot without blocking writes
  and without relying on WAL checkpoint timing. If `VACUUM INTO` is
  unavailable, fall back to the SQLite backup API (`sqlite3_backup_*`).
- **Neo4j**: Cypher export runs in a read transaction. Writes during
  export may not be included but will not corrupt the snapshot.
- **LanceDB**: Directory copy with file-level consistency (LanceDB
  uses append-only files).
- **Ordering**: SQLite databases are snapshotted first (they are the
  source of truth), then Neo4j, then vectors. This ensures the most
  critical state is captured at the earliest point.

### 5.4 Neo4j Backup Strategy

Two approaches depending on deployment:

**Docker (default):** `docker exec colony-neo4j neo4j-admin database dump neo4j`
produces a binary dump. Restore with `neo4j-admin database load`.

**Cypher export (portable):** Export all nodes and relationships as Cypher
statements. Slower but works across Neo4j versions and deployment modes.

Colony supports both, defaulting to Cypher for portability. If Neo4j is
unreachable during backup, the backup completes without the graph export
and emits a warning. The `/graph/` directory in the archive will contain
only a `SKIPPED.txt` marker. This allows SQLite-only partial backups
when Neo4j is down.

### 5.5 Incremental Backup

For ongoing protection, a lightweight incremental mode:

```bash
colony backup --incremental --output ~/backups/
```

Uses `VACUUM INTO` for each database that has been modified since the
last backup (compared by file mtime). Includes new event JSONs. Skips
Neo4j and LanceDB (those only change on `--full`). Each incremental
archive is restorable only on top of a prior full backup.

### 5.6 Scheduled Backup

```bash
colony backup --schedule daily --output ~/backups/ --retain 7
```

Installs a cron/launchd job that runs daily full backups, retaining the
last 7.

### 5.7 Secret Scrubbing

Config files included in backups are scrubbed before archival:

- `.env` files: values for keys matching `*_KEY`, `*_SECRET`,
  `*_PASSWORD`, `*_TOKEN` are replaced with `<REDACTED>`.
- The `persona.yaml` `variables.yaml` snapshot in persona backups
  redacts any variable whose name matches the same patterns.
- SQLite databases and Neo4j graph are NOT scrubbed (they may contain
  user-discussed credentials, but programmatic scrubbing would corrupt
  data). The encryption layer is the protection for database contents.

This is enforced by the backup serializer, not by convention.

### 5.8 Host State Backup

Colony can also back up the host agent's state if the host supports it.
The backup command accepts `--include-host` to bundle host state alongside
Colony state. Host state paths are declared in `colony.yaml` or via
environment variables:

```bash
colony backup --full --include-host --output ~/backups/
```

The host state manifest is provided by the host integration (see Part 6)
and tells Colony which files to include. Colony does not assume any
specific host directory structure.

---

## Part 6: Deployment Layer Framework

### 6.1 The Three Layers

```
┌──────────────────────────────────────────────────────┐
│  Persona Layer (user-defined agent identity)         │
│  Identity, personality, companion apps, secrets       │
│  User's repo -- never in Colony or Hermes             │
├──────────────────────────────────────────────────────┤
│  Colony Sidecar                                       │
│  Memory, cognition, contacts, channels, delivery      │
│  Public repo -- generic, no persona specifics         │
├──────────────────────────────────────────────────────┤
│  Host Agent Framework (e.g., Hermes)                  │
│  Gateway, platforms, plugins, tools, LLM routing      │
│  Public repo -- generic agent harness                 │
└──────────────────────────────────────────────────────┘
```

Each layer is independently installable and upgradable. The persona
layer is the only one that contains deployment-specific state.

Colony provides the deployment tooling (`colony persona`) that reads
any persona manifest and orchestrates setup, backup, and restore. The
persona repo itself is just a data package -- config files, service
scripts, templates -- with no framework code of its own.

**Single persona constraint**: A Colony instance supports one active
persona at a time. Running `colony persona setup` with persona B while
persona A is active requires first running `colony persona uninstall`
(which stops services, removes overlays, deregisters channels, and
restores the pre-persona config backup). Attempting setup without
uninstalling emits an error naming the active persona.

### 6.2 Persona Manifest

A persona is defined by a `persona.yaml` manifest at its repo root.
Colony reads this manifest to automate setup, backup, and service
management. The schema is generic -- no field references a specific
agent name, host framework, or infrastructure.

```yaml
# persona.yaml -- schema definition (all fields)
manifest_schema: 1                     # schema version; Colony rejects unknown versions
name: string                           # agent name, used as namespace
version: string                        # semver

host:
  type: string                         # host framework: "hermes", "openclaw", etc.
  config_overlay: path                 # merged into host config (e.g., config.yaml)
  env_overlay: path                    # merged into host .env
  identity: path                       # identity document (e.g., SOUL.md)
  skin: path | null                    # optional UI theme
  plugins:                             # host plugins to install
    - name: string
      source: path                     # relative to persona repo root

colony:
  env_overlay: path | null             # merged into Colony .env
  channels_config: path | null         # static channel definitions (channels.json)
  seed_data: path | null               # directory of seed memories, contacts

services:                              # long-running processes
  - name: string                       # unique service name
    script: path | null                # Python script (relative to repo)
    binary: path | null                # pre-built binary (alternative to script)
    type: string                       # "daemon" (default) | "scheduled"
    schedule: { interval: int } | null # for scheduled services (seconds)
    env: map[string, string]           # environment variables (supports {{ var }} templates)
    depends_on: list[string]           # other services or "hermes" | "colony"
    platforms: list[string]            # OS filter: ["macos", "linux"] (default: all)
    service_template: path | null      # custom plist/systemd template (optional)

companion_apps:                        # apps that register as Colony channels
  - name: string
    source: path                       # app source directory
    channel_key: string                # Colony channel registration key
    channel_manifest:                  # passed to POST /v1/channels/register
      display_name: string
      gateway_family: string
      supports_media: bool
      supports_voice: bool
      session_isolation: bool
      provides_channel_id: bool
      delivery_webhook: string | null
      platform_hint: string | null
      pii_safe: bool

tunnels:                               # SSH tunnels (optional, for remote model serving etc.)
  - name: string
    local_port: int
    remote: string                     # host:port on the remote network
    jump: string | null                # SSH jump host (user@host)
    tool: string                       # "autossh" (default) | "ssh" | "cloudflared"

secrets:                               # secret names (values never in the manifest)
  - name: string
    target: string                     # which .env file: "host" | "colony" | "service:<name>"
    description: string | null         # shown during interactive setup
    required: bool                     # true = setup fails without it

backup:
  host_state: list[path]              # host framework state to include in backups
  custom: list[path]                  # persona-specific state paths
  # Colony state is always included automatically

variables:                             # user-provided values for {{ template }} expansion
  - name: string
    prompt: string                     # shown during interactive setup
    default: string | null
    env_var: string | null             # pre-populate from this env var if set
```

### 6.3 Example: Minimal Persona

A persona that just adds personality to a Hermes + Colony stack with
no custom services:

```yaml
manifest_schema: 1
name: atlas
version: 0.1.0

host:
  type: hermes
  config_overlay: hermes/config-overlay.yaml
  identity: hermes/SOUL.md

colony:
  seed_data: colony/seed/

secrets:
  - name: LLM_API_KEY
    target: host
    description: "API key for your LLM provider"
    required: true
```

### 6.4 Example: Full Persona with Services and Channels

```yaml
manifest_schema: 1
name: my-agent
version: 1.0.0

host:
  type: hermes
  config_overlay: hermes/config-overlay.yaml
  env_overlay: hermes/env-overlay
  identity: hermes/SOUL.md
  skin: hermes/skins/agent.yaml
  plugins:
    - name: custom-messaging
      source: plugins/custom-messaging/

colony:
  env_overlay: colony/env-overlay
  channels_config: colony/channels.json
  seed_data: colony/seed/

services:
  - name: sms-adapter
    script: services/sms-adapter.py
    env:
      PHONE_GATEWAY: "{{ phone_gateway_addr }}"
    depends_on: [hermes]

  - name: voice-gateway
    script: services/voice-gateway.py
    depends_on: [hermes, colony]

  - name: health-monitor
    script: services/health-monitor.py
    type: scheduled
    schedule: { interval: 900 }

companion_apps:
  - name: terminal
    source: apps/terminal/
    channel_key: terminal
    channel_manifest:
      display_name: "Agent Terminal"
      gateway_family: companion
      supports_media: true
      provides_channel_id: true

tunnels:
  - name: llm-backend
    local_port: 5005
    remote: "{{ llm_host }}:{{ llm_port }}"
    jump: "{{ tunnel_jump_host }}"

secrets:
  - name: LLM_API_KEY
    target: host
    required: true
  - name: COLONY_API_KEY
    target: colony
    required: true
  - name: SMS_GATEWAY_SECRET
    target: "service:sms-adapter"
    required: false

backup:
  host_state:
    - "~/.hermes/state.db"
    - "~/.hermes/sessions/"
    - "~/.hermes/kanban.db"
    - "~/.hermes/skills/"
  custom:
    - "~/.local/share/custom-relay/state.db"

variables:
  - name: phone_gateway_addr
    prompt: "Phone gateway address (host:port)"
    default: "192.168.1.100:8080"
  - name: llm_host
    prompt: "LLM server hostname"
  - name: llm_port
    prompt: "LLM server port"
    default: "8000"
  - name: tunnel_jump_host
    prompt: "SSH jump host for tunnels (user@host, or empty for direct)"
    default: ""
```

### 6.5 `colony persona` CLI

Colony provides the generic tooling. The persona repo has no CLI of its
own -- `colony persona` reads the manifest and does everything.

```bash
# Setup from a persona repo
colony persona setup ./my-agent-repo
colony persona setup ./my-agent-repo --config vars.yaml   # non-interactive

# Backup everything (Colony state + host state + persona state)
colony persona backup --output ~/backups/

# Restore on a new machine (after colony init + host install)
colony persona restore ~/backups/persona-backup-20260627.tar.gz.enc

# Service management
colony persona services status          # health check all persona services
colony persona services start           # start all services
colony persona services stop            # stop all services
colony persona services restart <name>  # restart one service
colony persona services install         # re-render and install service defs
colony persona services uninstall       # stop + remove service definitions

# Validate manifest without mutating state (dry run)
colony persona validate ./my-agent-repo

# Update persona (re-apply overlays, update plugins, re-register channels)
colony persona update ./my-agent-repo

# Uninstall persona (stop services, remove overlays, deregister channels)
colony persona uninstall
```

`colony persona validate` runs step 1 only (schema check, path existence,
dependency verification) and reports issues without making changes.

**Setup steps** (what `colony persona setup` does):
1. Reads `persona.yaml`, validates against Pydantic schema (same as `validate`)
2. Checks that host framework and Colony are installed and running
3. Prompts for variables and secrets (or reads from `--config` file)
4. Merges config overlays into host and Colony configs (see 6.8)
5. Copies identity document, skin, plugins to host directories
6. Generates and installs service definitions (see 6.6)
7. Sets up SSH tunnels (autossh configs)
8. Registers companion app channels with Colony
9. Seeds Colony with initial data if provided
10. Runs `colony doctor` to validate

### 6.6 Idempotency

`colony persona setup` is safe to run multiple times:

- **Variables/secrets**: On re-run, existing values are loaded from
  `~/.colony/persona/{name}/vars.yaml` and presented as defaults.
  Only new/changed variables are prompted.
- **Config overlays**: The original host config is backed up before
  first merge (`config.yaml.pre-persona`). On re-run, the overlay is
  re-applied to the backup, not layered on top of the previous merge.
- **Services**: Existing service definitions are overwritten with
  freshly generated versions. Running services are restarted.
- **Channel registrations**: Upserted using the stored `channel_token`.
- **Seed data**: Skipped if Colony already has data (checks for
  existing contacts/memories).

`colony persona update` is an alias for re-running setup with the
latest manifest from the repo.

### 6.7 Service Definition Generation

Colony generates platform-appropriate service definitions from the
persona manifest. No templates needed in the persona repo unless
custom configuration is required.

**Supported platforms**: macOS (LaunchAgent) and Linux (systemd user
units). Windows and Docker are out of scope for Phase 5; containerized
deployments should use `docker-compose.yml` authored separately.

**macOS (LaunchAgent):**

Colony generates plist files automatically from the service definition:
- Label: `colony.persona.{persona_name}.{service_name}`
- Working directory: persona repo path
- Environment: loaded from `EnvironmentFile` (see 6.10), not embedded
- Logging: `~/.colony/logs/persona/{service_name}.log`
- KeepAlive: true for daemons, StartInterval for scheduled
- Dependencies expressed via `colony persona services start` ordering

If a service provides a custom `service_template` path, Colony uses that
template instead, passing all variables and environment as template context.

**Linux (systemd user unit):**

Colony generates `~/.config/systemd/user/colony-persona-{name}.service`
files with equivalent configuration. Uses `EnvironmentFile=` for secrets.
`systemctl --user enable/start`.

### 6.8 Config Overlay Merge Strategy

Config overlays are merged into the host/Colony config using a
**deep merge with overlay precedence**:

- Scalar values: overlay wins (overwrite)
- Lists: overlay replaces entirely (no append)
- Dicts: recursive merge (overlay keys added/overwritten, existing
  keys not in overlay are preserved)

The original config is backed up before first merge. On re-setup, the
merge re-applies to the original backup, preventing overlay drift.

If a merge would remove an existing key (not present in overlay), the
key is preserved. Explicit removal requires setting the key to `null`
in the overlay.

### 6.9 Service Failure Handling

`colony persona services start` starts services in dependency order:

- If a service fails to start, its dependents are skipped and a warning
  is emitted. Other independent services continue starting.
- Circular dependencies are detected at manifest validation time and
  rejected with an error.
- Missing script/binary paths are detected at `install` time, not at
  `start` time.
- `colony persona services status` reports each service as:
  `running`, `stopped`, `failed`, `not-installed`.

### 6.10 Secret Storage

Secrets prompted during setup are stored in
`~/.colony/persona/{name}/secrets.env`, a `chmod 0600` file with
`KEY=value` format. This file is:

- Never committed to any repo (persona repos should `.gitignore` it)
- Never included in backups (secrets must be re-entered on restore)
- Referenced via `EnvironmentFile=` in systemd units or loaded by a
  wrapper script for LaunchAgents, rather than embedded in service
  definitions

On macOS, Colony can optionally use the system Keychain if
`COLONY_SECRETS_BACKEND=keychain` is set. Default is the env file.

### 6.11 Persona Backup Archive

`colony persona backup` produces:

```
persona-backup-{name}-{timestamp}.tar.gz[.enc]
  /colony/
    (full colony backup -- identity, databases, graph, vectors, config)
  /host/
    (files listed in backup.host_state from persona.yaml)
  /persona/
    (files listed in backup.custom from persona.yaml)
  /manifest/
    persona.yaml            (copy of the manifest at backup time)
    variables.yaml          (resolved variable values, secrets redacted)
    registered_channels.json (channel registrations at backup time)
  /meta.json
    persona_name, persona_version, colony_version, host_type,
    host_version, timestamp, platform
```

Restore applies each section:
1. Colony state via `colony restore --full`
2. Host state copied to host directories
3. Persona state copied to custom paths
4. Channels re-registered from saved manifests
5. Services re-installed and started
6. `colony doctor` run to validate

---

## Part 7: Host Integration Contract

Colony is host-agnostic -- any agent framework can integrate via the
`/v1/host/` API. This section describes the integration contract and
uses Hermes as the reference implementation.

### 7.1 What the Host Must Provide

At minimum, the host must call Colony's turn sync endpoint with:
- `context.session_id` -- stable per-conversation identifier
- `context.contact_id` -- resolved contact (via `/v1/host/contacts/resolve`)

For full channel awareness, the host should also provide:
- `context.channel_id` -- stable per-conversation-per-channel identifier
  (if not provided, Colony auto-derives from gateway + contact)

### 7.2 Reference: Hermes Changes

For Hermes specifically, two small upstream changes enable full integration:

**H1: Add `chat_id` to plugin hook kwargs**

In `agent/turn_context.py` and `agent/turn_finalizer.py`, include
`chat_id=agent.chat_id` in the kwargs passed to `pre_llm_call` and
`post_llm_call`. This is the only Hermes-core change required.

**H2: Add `--platform` flag to `hermes -z`**

In `hermes_cli/oneshot.py`, accept an optional `--platform` argument.
Default remains `"cli"`. Passed through to `AIAgent(platform=...)`.

### 7.3 Colony Host Plugin Changes

These are changes to the Colony host plugin (the plugin that connects
the host framework to Colony's API -- can live in the persona repo or
a shared plugin repo):

**P1: Capture `chat_id` from hooks**

```python
def _pre_llm_call_hook(**kwargs):
    _session_state["platform"] = kwargs.get("platform", "unknown")
    _session_state["sender_id"] = kwargs.get("sender_id", "")
    _session_state["chat_id"] = kwargs.get("chat_id", "")  # NEW
```

**P2: Send `channel_id` in turn sync**

```python
def _post_llm_call_hook(**kwargs):
    platform = _session_state.get("platform", "unknown")
    chat_id = _session_state.get("chat_id", "direct")
    channel_id = f"{platform}:{chat_id}"

    client.sync_turn(
        ...,
        context={"session_id": ..., "contact_id": ..., "channel_id": channel_id},
    )
```

**P3: Register channels at startup**

```python
def register(ctx):
    # After Colony client init, register this Hermes instance's platforms
    for platform in ctx.list_platforms():
        client.register_channel(ChannelManifest(
            channel_key=platform.name,
            display_name=platform.label,
            gateway_family=_infer_family(platform),
            ...
        ))
```

### 7.4 Backward Compatibility

Colony's auto-derivation of `channel_id` (Part 2) ensures that hosts
that do NOT send `channel_id` still get working provenance. The host
changes described above are enhancements, not requirements. Colony
works with any host that calls its HTTP API, regardless of whether it
implements the channel registration protocol or provides `channel_id`.

---

## Part 8: Implementation Phases

### Phase 0: Auto-derive channel_id (Colony-only, no dependencies)

- Add `_ensure_channel_id()` to host turn processing
- Remove the `if channel_id:` guard on provenance recording
- Gateway resolution uses simple fallback chain: session gateway field
  (already set at session creation from host context) -> contact primary
  handle gateway -> `HostIdentity.host_id` -> `"unknown"`. All of these
  fields exist in the current codebase; no Phase 1 dependency.
- **Result**: Provenance and cross-context guards come alive immediately

### Phase 1: Channel Registration API (Colony-only)

- Build schema migration runner (schema_version table, numbered .sql files,
  idempotent application on startup)
- Add `colony-channels.db` and `ChannelStore` in `COLONY_STATE_DIR`
- Add `/v1/channels/` REST endpoints with API key + channel_token auth
- Run migration `003_open_gateway_enum.sql` to remove SQL CHECK constraint
- Replace `_PHONE_GATEWAYS` in all three locations (store.py, resolver.py,
  world_bridge.py) with channel store query + fallback set
- Update `ChannelRegistry` to read from channel store, keep
  `DEFAULT_GATEWAY_MAP` as fallback for pre-registration state
- Add webhook URL validation (1.6)
- **Result**: Any channel can register itself; delivery routing is dynamic

### Phase 2: Full-State Backup (Colony-only)

- `colony backup --full` command
- SQLite snapshot via `VACUUM INTO` (consistent, non-blocking)
- Neo4j Cypher export (with graceful fallback if unreachable)
- LanceDB directory archival
- AES-256-GCM encrypted archive with identity binding (HMAC)
- Secret scrubbing for config files (5.7)
- `colony restore --full` command with identity verification
- **Result**: Colony state is fully portable between machines

### Phase 3: Host Integration Enhancement (Host upstream)

- Host exposes conversation/chat ID to plugin hooks
- Host supports platform identity for one-shot/companion invocations
- **Result**: Hosts can provide real channel_id; companion apps get
  distinct identities

### Phase 4: Colony Host Plugin Update

- Capture conversation ID, send `channel_id` in turn sync
- Auto-register host platforms as Colony channels at startup
- **Result**: Full channel awareness with real conversation IDs

### Phase 5: Persona Deployment Framework (Colony)

- Implement `persona.yaml` manifest schema and validation
- Build `colony persona setup` / `backup` / `restore` CLI
- Service definition generation (macOS LaunchAgent + Linux systemd)
- Service lifecycle management (`colony persona services`)
- Template variable resolution and secret prompting
- **Result**: Any persona can be deployed from a manifest + `colony persona setup`

### Phase 6: Persona Migration (Per-deployment)

- Organize existing services/scripts into a persona repo with `persona.yaml`
- Create config overlays (host config, colony env)
- Move companion apps into the persona repo
- Define backup paths for host and custom state
- Test full cycle: clean machine, `colony persona setup`, `colony persona restore`
- **Result**: Agent is rebuildable from `git clone` + `colony persona setup` +
  `colony persona restore`

---

## Appendix A: Backup State Categories

Colony backup (`colony backup --full`) automatically discovers and
includes all SQLite databases in `COLONY_STATE_DIR`. The categories
below describe what kinds of state exist, not a hardcoded list.

### Colony State (always included automatically)

| Category | Examples | Criticality |
|----------|----------|-------------|
| Identity | colony-id, Ed25519 keypair, genesis | Critical -- not regenerable |
| Relationship data | contacts, affect, facts, comms | Critical -- learned over time |
| Goals and commitments | goals, commitments, schedules | Critical -- active plans |
| Behavioral learning | preferences, patterns, observations | Important -- slow to relearn |
| Operational state | initiatives, task queue, agents | Rebuildable -- ephemeral work |
| Audit and provenance | guard audit, provenance, engagement | Low -- forensic only |
| Vector embeddings | lancedb/ | Rebuildable from graph |
| Graph database | Neo4j | Critical -- all memories |
| Event log | events/*.json | Important -- audit trail |
| Config | .env, channels.json, LLM config | In persona repo or regenerable |

### Host State (declared in persona.yaml `backup.host_state`)

Each host framework has its own state. The persona manifest declares
which paths to include. Common examples for Hermes:

| Category | Examples |
|----------|----------|
| Conversation history | state.db, sessions/ |
| Task state | kanban.db |
| Learned skills | skills/ |
| Platform credentials | whatsapp/, pairing/ (re-pairable) |
| Identity and config | SOUL.md, config.yaml (in persona repo) |

### Persona-Specific State (declared in persona.yaml `backup.custom`)

Any additional state the persona needs. Examples:
- Messaging relay databases
- Contact directories
- Custom service state files

The persona manifest is the single source of truth for what gets
backed up beyond Colony's own state.

## Appendix B: Channel Registration Examples

### Registering a WhatsApp Channel

```python
# Hermes WhatsApp platform adapter calls this at connect time
colony_client.register_channel({
    "channel_key": "whatsapp",
    "display_name": "WhatsApp",
    "gateway_family": "phone",
    "supports_media": True,
    "supports_reactions": True,
    "phone_identity_unification": True,
    "provides_channel_id": True,  # will send JID as channel_id
    "delivery_protocol": "hermes",
    "max_message_length": 65536,
})
```

### Registering a Companion Terminal

```python
# Terminal app calls this at startup
colony_client.register_channel({
    "channel_key": "terminal",
    "display_name": "Agent Terminal",
    "gateway_family": "companion",
    "supports_media": True,
    "supports_voice": False,
    "session_isolation": False,          # shares context with other channels
    "provides_channel_id": True,
    "delivery_webhook": "http://localhost:8775/deliver",
    "platform_hint": "User is at a local terminal.",
})
```

### Registering a Voice Channel

```python
# Voice gateway registers at boot
colony_client.register_channel({
    "channel_key": "voice",
    "display_name": "Phone Call",
    "gateway_family": "companion",
    "supports_media": False,
    "supports_voice": True,
    "phone_identity_unification": True,
    "session_isolation": True,          # calls are isolated (deferred)
    "provides_channel_id": True,  # call-id as channel_id
    "platform_hint": "User is on a live phone call. Be concise.",
    "pii_safe": False,  # voice is overheard
    "max_message_length": 200,
})
```

## Appendix C: Rebuild Procedure (Target State)

### Fresh Install (no prior state)

```bash
# 1. Install host framework
pip install hermes-agent        # or whichever host
hermes init

# 2. Install Colony
pip install colonyai
colony init
colony service install
colony service start

# 3. Deploy persona
git clone <persona-repo-url> ~/my-agent
colony persona setup ~/my-agent
# Interactive: prompts for secrets, tunnel hosts, etc.
# Non-interactive: colony persona setup ~/my-agent --config vars.yaml

# 4. Verify
colony doctor

# 5. Start everything
colony persona services start
```

### Restore from Backup (rebuilding on a new machine)

```bash
# 1. Install host framework + Colony (same as above)
pip install hermes-agent && hermes init
pip install colonyai && colony init

# 2. Deploy persona with setup
git clone <persona-repo-url> ~/my-agent
colony persona setup ~/my-agent

# 3. Restore all state from backup
colony persona restore ~/backups/persona-backup-latest.tar.gz.enc
# Restores: Colony state (identity, DBs, graph, vectors)
#           Host state (conversations, skills, sessions)
#           Persona state (custom service databases, relay state)
#           Re-registers channels, re-installs services

# 4. Verify and start
colony doctor
colony persona services start
```

### Ongoing Backup

```bash
# Manual backup
colony persona backup --output ~/backups/

# Scheduled backup (installs cron/launchd job)
colony persona backup --schedule daily --output ~/backups/ --retain 7

# Encrypted backup
colony persona backup --output ~/backups/ --encrypt
```

The entire rebuild (excluding model downloads and re-pairing of
platform credentials like WhatsApp) should take under 15 minutes
on a prepared machine.
