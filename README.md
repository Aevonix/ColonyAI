# Colony

Persistent memory and shared work for a personal agent, using your own models.

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/Aevonix/ColonyAI/actions/workflows/ci.yml/badge.svg)](https://github.com/Aevonix/ColonyAI/actions/workflows/ci.yml)

Colony runs beside [Hermes](https://github.com/NousResearch/hermes-agent).
Hermes handles conversations, tools, scheduling and delegation. Colony retains
what happened, assembles relevant evidence before a turn, and shares commitments
and work across sessions. Your private deployment supplies its identity,
credentials, channels and hardware adapters.

This repository is undergoing the 1.0 consolidation. The native attachment,
source memory and shared-work paths have executable qualifications. The larger
legacy autonomy surface is still being reduced and integrated. A package or an
API route being present does not establish that its loop works in a deployment.

## Start with Hermes

The qualified runtime is Hermes 0.21.0. Use its Python interpreter for attachment;
Python 3.12 is exercised by native integration CI. One local OpenAI-compatible
chat endpoint is sufficient. The lightweight profile needs no Docker, Neo4j,
embedding model or external account.

Install from this checkout to use the changes described here:

```bash
python -m pip install . ./sidecar
colony init --hermes-python /path/to/hermes/.venv/bin/python
```

The wizard selects a Hermes home, asks for your name, agent name and model, and
creates private state outside Git. It preserves existing identity, channels and
model settings. Replacing an incumbent memory provider is an explicit choice.
It attaches the canonical installed adapter when its bytes match, or installs
profile directory adapters when no native package is present. It neither
patches Hermes core nor restarts an existing gateway.

Accept the wizard's startup option, or run:

```bash
colony --instance /path/to/private/colony start --detach
colony --instance /path/to/private/colony status
```

Start a new Hermes session, give it a harmless fact, then ask for that fact in a
second session. The [setup guide](docs/LOCAL-HERMES-SETUP.md) explains existing
profiles, unattended flags, startup and recovery. The minimum profile remembers
and observes. Consequential background execution requires deliberate setup.
For login startup and automatic process recovery, stop a detached instance and
run `colony --instance /path/to/private/colony service install`, then
`colony --instance /path/to/private/colony service start`. Linux systemd user
services and macOS LaunchAgents use the selected environment and private state.
This follows the user session's lifetime, not a guarantee of operation before
login. See the setup guide for status, stop, uninstall and recovery.

## What the active paths provide

- **Automatic recollection.** The native memory provider requests context for
  the current participant, session and question before inference. A durable
  outbox captures ordinary turns; retry does not create another source.
- **Scoped source recall.** Authenticated participants can recall their own
  canonical evidence without the legacy graph runtime. Optional semantic
  projections find retained passages and image descriptions, then resolve them
  back to current sources before selection. [Source retrieval](docs/SOURCE-SEMANTIC-RECALL.md)
- **Evidence that outlives a model.** Original messages, timestamps, provenance
  and derived claims persist independently of inference weights. Corrections
  and conflicting claims remain inspectable. [Source claims](docs/SOURCE-CLAIMS.md)
- **Images with origins.** Retained image bytes and model-generated descriptions
  stay distinct. Recollection can include the description and a scoped reference
  to its source. This does not imply reliable understanding of every image.
  [Image memory](docs/SOURCE-IMAGES.md)
- **Shared work.** Sessions can observe commitments and claim work through one
  persistent registry. Another session sees who holds it. A lease coordinates
  work; it cannot by itself make an external side effect exactly once.
  [Commitment work](docs/COMMITMENT-WORK.md)
- **Replaceable search indexes.** Optional Lance indexes record embedding
  identity and rebuild into a separate generation. Interrupted rebuilds resume;
  incompatible or unknown vectors are not compared. Canonical evidence remains
  available through lexical recall. [Embedding generations](docs/EMBEDDING-GENERATIONS.md)
- **Source-linked forgetting.** Erasure fences source evidence immediately and
  removes linked fact, graph and vector projections. Cleanup status is explicit.
  Unlinked historical records, host transcripts and backups require additional
  reconciliation. [Derived fact lineage](docs/TOM-SOURCE-LINEAGE.md)

## Architecture and extension

One sidecar owns canonical state and a bounded context selector. SQLite supports
the minimum deployment. Neo4j remains the extended deployment's legacy memory
store; Lance is a replaceable search projection. Legacy graph records are not
yet fully reconstructible from canonical sources. There is no database service
per cognitive feature.

The Hermes adapter uses native plugin and memory-provider contracts. Other hosts
can use the HTTP API, with participant identity supplied by an authenticated
adapter. Device protocols, continuous audio, camera capture and private identity
belong in deployment adapters rather than public cognition code.

Named model roles choose configured local endpoints and fallbacks. Changing a
processor does not replace the agent's memories or identity. Fleet discovery,
capacity scheduling and empirical model selection are separate work; role
configuration alone does not provide them.

See [architecture and ownership](docs/ARCHITECTURE.md) before adding a component.
Every addition should close an observable loop, have one state owner, and reuse
Hermes facilities where they already fit. Self and relationships are explicit
state, not evidence of subjective feelings. Relationship scores must not grant
privileged capabilities.

## Operation and limits

Consequential actions need owner authorization. Authorization belongs to the
work being approved; losing a notification should not discard a build or its
result. The lightweight installer does not enable external-effect workers.
Existing extended deployments must inspect their actual resolved configuration
at `GET /v1/host/autonomy/posture` before enabling another loop.

The [known gaps](docs/KNOWN-GAPS.md) inventory includes legacy components that
are partial, dormant or awaiting replacement. It is not a list of 1.0 guarantees.
Current work includes unattended startup, broader hardware and session
session coverage, measured self-improvement and removal of obsolete operational
machinery. No claim of general intelligence or complete autonomy is made here.

## Development

Install the sidecar development dependencies, then run its tests from `sidecar`:

```bash
python -m pip install -e './sidecar[dev]'
cd sidecar
python -m pytest -q tests colony_sidecar
```

`tests/hermes_adapter` qualifies built packages against the pinned native
Hermes runtime. Its controlled model fixture verifies integration, not model
quality. Deployment acceptance also needs real inference, source receipts,
cross-session behavior and recovery with the actual configuration.

Keep personal data, endpoint coordinates, secrets and deployment configuration
out of public examples, commits and test artifacts.

## License

[MIT](LICENSE). Optional dependencies retain their own licenses.
