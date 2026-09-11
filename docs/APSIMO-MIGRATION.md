# Moving an existing ColonyAI installation to Apsimo

Apsimo is the platform name. Aeva and other private agents retain their own
identity. Historical transcripts, source citations and release evidence keep
their original names. Existing memory, contact, task, erasure and action IDs
remain unchanged.

## Packages and interfaces

| Preferred distribution or interface | Compatibility name |
| --- | --- |
| `apsimo`, import `apsimo` | `colonyai`, import `colony_sidecar` |
| `apsimo-hermes`, import `apsimo_hermes` | `colonyai-hermes`, import `colony_hermes` |
| `apsimo-hostworker`, import `apsimo_hostworker` | import `colony_hostworker` |
| `apsimo` and `apsimo-*` CLI commands | existing `colony` and `colony-*` commands |
| `APSIMO_*` environment variables | existing `COLONY_*` variables |

The new distributions provide legacy import and CLI aliases. Do not install old
and new distributions into the same environment: both would own compatibility
files, and uninstalling one could break the other. Build a fresh environment with
matching sidecar and adapter versions, then select it for the existing instance.
An alias imports the same module object, including submodules and their caches;
it does not run a second implementation or open a second store.

Environment aliases are normalized at startup and explicit instance selection.
Unequal explicit old and new values are an error identifying the variable names,
without printing their values. Do not set both names to different credentials or
directories. Existing instance directories and service state locations stay in
place. Unconfigured library use keeps its historical `~/.colony/data` fallback;
guided setup selects an explicit private instance directory.

The hostworker action catalog keeps its serialized `colony_*` operation names.
These are compatibility protocol identifiers used by digests and pending work,
not the preferred names advertised to the model. Existing MCP resource URIs
also remain valid. MCP lists one `apsimo_*` tool catalog; old tool calls map to
the same functions.

## Cutover and validation

1. Record the selected runtime, environment, profile, state directory and current
   release. Take the deployment's normal state backup. Do not move stores for a
   branding change.
2. Install the matching Apsimo packages in a fresh environment. Verify canonical
   and legacy imports, CLI help and actual plugin discovery before changing the
   selected runtime. The [setup guide](LOCAL-HERMES-SETUP.md) describes attachment
   refresh and service selection.
3. Stop the selected runtime only for its bounded attachment change, refresh the
   adapter, and select the new environment. Keep the old environment and previous
   attachment available for rollback. Never run two gateway writers for the same
   profile while testing the rename.
4. For coding harnesses, run `apsimo mcp setup --harness <name>`. It replaces one
   legacy config entry with one canonical entry, retains custom credentials,
   endpoint and launch settings, and preserves other servers. Explicit URL and
   command options override those selections. Authentication is inherited from
   the launching environment unless the existing config holds an explicit key.
   Conflicting duplicate entries need reconciliation before setup can write.
5. Test useful recall against the existing source IDs, one ordinary conversation,
   task continuity, erasure visibility and any configured voice/hardware adapter.
   Add one owner-approved naming decision to the existing canonical memory system,
   with ColonyAI as a historical search alias. Do not rewrite old conversations.

Generated `APSIMO.md` is a current reference, not a reason to erase an existing
private `COLONY.md`. Review any explicit harness include list during cutover.
Installed legacy diagnostic skill directories are reused rather than installing
a second discoverable copy. Fresh installations use `apsimo-diagnose`.

Publishing is a separate release step. The workflow retains the configured PyPI
token mechanism; existing token metadata cannot prove authority to create the
new package names. New PyPI names, GitHub repository identity and GHCR package
permissions must be verified during release. Repository-bound trusted-publisher
or OIDC registrations, when used by a deployment, need the new repository name.
Do not call the public rename complete until the canonical packages and image
have actually published and a clean install from those artifacts passes.
