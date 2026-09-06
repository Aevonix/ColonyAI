# Native Hermes adapter distribution

`colony-hermes` packages the existing general adapter and memory provider for
installation into the Python environment that runs Hermes. The Colony sidecar
is a separate service. The adapter does not install the sidecar, a context
engine, a worker daemon, or operating-system services.

Build from the repository root:

```sh
python -m pip install build
python -m build
```

Install the resulting wheel with the Python interpreter that runs Hermes:

```sh
python -m pip install dist/colony_hermes-0.1.0-py3-none-any.whl
```

The wheel exposes `colony` through `hermes_agent.plugins` and `colony-memory`
through `hermes_agent.memory_providers`. It maps the canonical source files in
`plugins/hermes-plugin/` and `plugins/colony-memory/` to importable packages.
Only `catalog.py` and `contract.py` from `hostworker/colony_hostworker/` are
included in the adapter's private catalog package. Source-checkout plugin paths
and the existing installer's copying behavior remain compatible.

## Activation and current limits

Installing the wheel makes the adapters discoverable. It does not change a
Hermes profile, select a memory provider, or enable tools. Activation requires
the existing general-adapter configuration, `plugins.enabled: [colony]`, and
`memory.provider: colony-memory` in the selected private profile. Preserve
other enabled plugins when editing that list. The existing coexistence settings
are also required in the Hermes process environment:

```sh
COLONY_GENERAL_PLUGIN_ACTIVE=1
COLONY_MEMORY_WORKER_TOOLS=0
COLONY_MEMORY_TURN_WRITER=disabled
```

Configure the sidecar URL and contact through native `hermes memory setup`
and matching `plugins.colony` configuration, and supply `COLONY_API_KEY` privately.
Native setup stores non-secret fields in the selected profile's
`colony-memory.json`, which overrides legacy `memory.config`. The
general adapter needs a private writable turn outbox and verified participant
bindings. Consequential tools retain their existing mediator requirements.
This packaging change does not provision those dependencies.

Do not silently replace a selected external memory provider. Existing
directory installations also need explicit migration: Hermes gives directory
memory providers precedence over pip providers, while a pip general plugin can
override a same-name directory plugin. Remove or archive obsolete plugin
directories only as part of an intentional profile migration.

When the memory provider is selected, native CLI discovery exposes
`hermes colony-memory status`, `goals`, `context`, and `sync`. These commands
resolve the same selected profile settings and credentials as the provider;
explicit URL/contact arguments remain available. The Typer app remains available
to existing callers.

Profile settings and handoff files stay scoped to the selected Hermes home.
The provider remains attached through a sidecar startup outage and retries on
later requests. Automatic profile activation remains follow-up work; packaging
alone does not establish production readiness.

## Durable source capture

The native memory provider implements compression checkpoint API v2. Set
`compression.checkpoint_required: true` to make Hermes retain its transcript
when local checkpoint persistence fails. A successful checkpoint commits the
normalized direct source messages into the same private SQLite outbox used by
the general adapter. The callback then attempts delivery within 250 ms.
Sidecar downtime leaves a pending durable record and permits compression;
pending does not mean centrally recallable. Diagnostics expose checkpoint
state. The ordinary turn writer and subsequent checkpoints drain that outbox.

The outbox accepts at most 8 MiB of serialized data per record. Oversized or
unserializable evidence fails explicitly without clipping it. System rows,
tool wrappers, compression summaries and injected `api_content` are excluded.
Direct message content, including media references, is retained. The general
turn writer also retains complete messages instead of cutting at 2,000
characters. New records require a `source_recorded` receipt before delivery is
acknowledged, so an older sidecar cannot silently discard an unfamiliar
checkpoint payload during an upgrade.

The existing `/v2/host/turns/{turn_id}` endpoint stores direct source JSON and a
lexical index transactionally in `turn-idempotency.db`. Checkpoints bypass
ordinary interaction, affect, graph and initiative effects. Replays do not
create another source. `occurred_at` is retained when supplied explicitly in
context metadata; otherwise it is unknown. Server `ingested_at` is separate
and is never treated as the date asserted by the conversation.

`/v1/host/context/assemble` reads the source index after the existing exact
viewer check. Ordinary attributed turns can be recalled across that person's
sessions. Full-history checkpoints additionally require their original session,
because the native history does not guarantee old per-message speaker
attribution. Media references remain source data; only text is indexed. This
is direct evidence recall, not an embedding migration or a belief contradiction
engine.

## Source erasure and replay

`POST /v1/host/memory/sources/forget` accepts an authenticated contact and 1 to
100 canonical source IDs. The existing MCP server exposes `colony_forget_sources`.
Native Hermes committed memory removes also attempt an exact, session-bound
`old_text` match, including when the general plugin owns ordinary turn writes.
No match or multiple matches produces an explicit unmapped/ambiguous diagnostic;
it never broadens a text search into deletion. IDs unknown to the central source
store are rejected. Pending-only local evidence needs to be identified at its host.

Erasure commits source-ID and exact-message hashes before cleaning linked graph
summaries. Checkpoint copies are redacted within the same contact and session;
unrelated messages remain. New graph summaries carry `source_uri=turn:<id>` and
`source_turn_id`. Projection markers block late writes and reads while deletion
is pending. The response separates source erasure from graph cleanup and host
reconciliation. A repeated request retries the same derived cleanup targets.

The host outbox migrates its existing v1 database transactionally to v2 with a
separate contact-bound erasure watermark. Canonical turn/checkpoint delivery
fetches `/v1/host/memory/sources/erasures` before PUT. A missing endpoint, outage,
incomplete page or server history behind the host cursor holds replay. The host
purges both pending payloads and delivered receipts, preserving unrelated pending
messages as evidence-only checkpoints. Erased IDs cannot be enqueued again after
reconciliation. This is not a model-generation counter. Generic caller-provided
outbox delivery callbacks must use `ColonyClient.sync_turn(..., outbox=outbox)`
to participate in reconciliation.

This is scoped source erasure, not a claim of global forgetting. Native Hermes
transcripts/API context, backups, prior graph records without source lineage,
legacy shared facts, ToM, commitments, and other old derivative stores need their
own erasure adapters. Offline hosts retain bytes until reconnecting; filesystem
snapshots and physical-media remnants are outside this logical-delete contract.
Erasure history must survive backup restores; replay detects an older watermark
but cannot prove that a replaced server with reused sequence numbers is equivalent.

## Qualification

The qualification target is Hermes v0.21.0, tag `v2026.8.31`, commit
`29112bef099274229cadff79cdff7bf7b99c4b77`, tested on Python 3.12. The package
allows Python 3.11 through 3.13; those other interpreters are not yet qualified.
Other Hermes
releases are unqualified until the native-loader checks pass against them.
Hermes is installed separately; this package does not select or upgrade it.

Install test dependencies into an isolated environment with the target Hermes
checkout available, then run:

```sh
python -m pip install build pytest "setuptools>=77" wheel
python -m pytest tests/hermes_adapter -q
```

The tests build a wheel and a source distribution, rebuild from the source
distribution, install the wheel outside the checkout, and exercise the actual
Hermes general and memory loaders. Native-loader tests require Hermes and
are explicitly skipped when it is absent. No live sidecar, model endpoint,
production profile, or channel is contacted.

## Shared execution observations

On the qualified Hermes v0.21.0 release (`v2026.8.31`, commit
`29112bef0992`), set `plugins.colony.execution_registry_enabled: true` to
publish native turn, API, tool and delegated-child lifecycle observations.
This is opt-in for existing installations. The adapter credential must already
have scoped `turns:write` and exact person grants; `context:read` grants access
to the corresponding view. The legacy global bearer does not attest identities
for this new surface. To observe trusted agent cron fires, include `cron` in
the existing `attested_system_platforms` configuration; it is not enabled by
the adapter's default `cli` binding. No network call is made during registration.

Colony stores only execution IDs, participant/session linkage, channel, phase,
tool name and observation times in the existing `turn-idempotency.db`. It does
not copy prompts, tool arguments/results or task descriptions. Each hook has a
400 ms network deadline and failures do not stop a turn. Ordinary use supplies
new observations at API/tool boundaries; there is no heartbeat thread. After
120 seconds without an observation, liveness is **unknown**, including during
a long model response. A terminal native turn event closes its observation;
late events cannot reopen it. Metadata older than seven days is hidden from
the view and removed during subsequent observation writes.

`GET /v1/host/executions?contact_id=<bound-person>&session_id=<session>` returns
the scoped view with age, coverage and truncation fields. The owner, identified
by server configuration and an existing exact person grant, can see registered
turns across sessions. Other people can see only their own selected session;
public/guest turns receive no automatic registry injection. The owner's normal
context assembly includes up to eight observations when present.

The native `subagent_start` event binds each executing child to the exact
observed parent turn. Child observations inherit that participant and cannot
broaden it. Observation does not grant tool authority; the separate enforcement
below also works when observation is disabled. No model argument selects the
writer or owner role.

The view observes running Hermes turns, including agent cron fires and
executing subagents, and reads claimed/running workers from the canonical task
queue for the owner. It also reads native cron attempts, including script-only
jobs, from one explicitly bound Hermes profile. The sidecar uses its private
local-instance manifest or explicit `HERMES_HOME`; it never scans other profiles
or assumes a default home. A conflicting binding is unavailable. The existing
CLI-managed service supplies the same private home automatically.

The cron projection reads `cron/executions.db` without importing or running the
native scheduler, initializing its schema, or rewriting its status. It returns
native execution/job IDs, a hashed source-home identity, optional job names,
record timestamps and claimed/running status, plus a bounded day of recent
completed/failed/unknown records. Prompts, scripts, output, errors and credentials
are excluded. No observation writer or polling daemon is added. Native recorded
running state has **unknown process liveness**, however recent it is. Only
Hermes can reconcile abandoned attempts; completed records do not prove a
third-party effect. Guests receive no native cron rows. Missing, incompatible
or unreadable ledgers are explicitly unavailable, and `complete` is always
false for the combined work view.

Qualification fires a due native `no_agent` job in a disposable profile with
`deliver=local`, observes the same execution ID during and after actual script
completion, and checks the persisted output. This does not depend on an empty
`enabled_toolsets`, which Hermes treats as its default tool selection. A separate
installed-wheel test runs actual native parent and child turns with controlled
inference, reads a neutral local file, delivers the asynchronous completion
through the native CLI ownership filter, and resumes the parent. A foreign
session cannot consume that completion; a guest child retains guest tool limits.
These tests do not contact a model endpoint or a production profile.

The view still does not cover queued children,
unregistered external coding processes or every hardware service.
It does not enforce atomic free-text conversational commitments. The optional
[explicit shared undertaking](COMMITMENT-WORK.md) coordinates work against an
existing person commitment ID in its durable store. Seeing concurrent work is a prerequisite
for coordination, not a guarantee that promises cannot conflict.

## Native tool authority

On the qualified Hermes release, the general plugin enforces participant
authority at native `tool_execution` middleware. Resolved owner turns and
explicitly configured local system platforms retain native tools and existing
Hermes toolset, approval and guardrail checks. Guests cannot directly invoke
shell, files, network, devices, coding, delegation, native memory/session tools,
or unknown plugin/MCP tools. The exact Colony tools keep their existing scoped
read checks and action mediators. No arbitrary tool prefix or relationship
score grants access.

A native-tool request from a guest returns `requires_authorization`, with no
effect and no approval created. The response directs the agent to an enabled
Colony action request or the owner. It does not invent an approval request or
permit a raw tool after conversational consent. Broader guest capabilities
require a scoped mediated interface; this packet supplies none automatically.

Authority uses the transport-resolved participant bound to the exact native
session/task/turn, never fields in model arguments. A native child-spawn event
binds the child to the exact parent's authority. Missing or conflicting parent
bindings cannot acquire the local CLI system role. Native pre-API callbacks
retain that binding across compression session rotation. Coding RPC calls use
Hermes' propagated execution context, without a process-global last-owner or
task-only lookup. Unavailable identity returns an explicit error because
Hermes middleware exceptions otherwise fail open. Local trusted CLI remains
available when contact resolution is offline; remote identity is not guessed.

This is a boundary for model-requested tool dispatch while the general plugin
is loaded. It does not sandbox trusted installed plugin code, remove private
facts already present in old transcripts/system prompts, or replace the
deployment's channel admission, secret isolation and consent policy. Native
owner tools retain their existing consent behavior; they are not converted to
Colony intents by this gate. Qualify both owner and guest messages before
enabling a public channel.

## Native post-turn review

Hermes' automatic review reuses its parent's session ID without emitting a
subagent-start event. The adapter captures the exact parent participant on the
native caller thread and accepts inheritance only with Hermes' trusted review
provenance, matching parent/session, and a distinct task/turn. Later speakers in
the same session cannot upgrade a queued guest review. Reviews get a separate
work record linked to their parent, do not replace the session's current speaker,
and do not write their internal harness into ordinary conversation evidence.

For owner/system reviews, `skill_manage` produces a proposal in Hermes' existing
`pending/skills` store. Native curator ownership checks still apply. Foreground
owner requests retain native behavior. The proposal is not an evaluated or
activated improvement: an explicit operator or a separately configured task
evaluator must use the native apply path. Mutation history and rollback remain
in Hermes' skill ledger. No new consent transport or learning store is added.

Installed-wheel qualification runs real native automatic review threads with
controlled inference for owner and guest, including a later-owner race in the
guest's session. It checks proposal persistence, exact inherited authority,
separate work observation, absent harness capture, and explicit native
apply/ledger rollback. It does not claim measured task improvement from that
controlled model fixture.
