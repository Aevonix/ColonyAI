# Native Hermes adapter distribution

Ordinary recall no longer inserts the three latest global briefings into every
turn. Those records were selected without a relevance or freshness check and
could repeatedly add old operational summaries to unrelated conversations.
Their stored contents remain available through `/v1/host/briefings`. Scoped memory retrieval, source
citations and current work context continue through the existing assembly path.

Hermes 0.21.1 applies `hooks.output_spill` to external memory-provider output.
Its default 10,000-character head/tail preview can cut a PacoMind source envelope
or remove the middle of an original/correction bundle. Guided attachment and
explicit `pacomind init --refresh-adapter` align `hooks.output_spill.max_chars` to
65,536 characters, retaining an existing larger cap or disabled spill setting.
The original configuration is backed up and an adjustment is printed during
refresh. The managed draft profile receives the same alignment at creation,
role refresh and adapter refresh. Restart the selected Hermes gateway to load
this provider setting; newly spawned workers load their own profile setting.

This allowance leaves headroom above the selected-memory ceiling of 24,000
characters for citations and other default context sections. It does not increase
retrieval budgets, shorten corrections or change model context capacity. This
is a shared Hermes hook setting: unusually large custom context producers must
size their own transfer allowance. The aggregate of optional context sections
has no universal size bound. A native MemoryManager regression verifies a complete envelope
larger than the old default survives the installed configuration unchanged.

`pacomind-hermes` packages the existing general adapter and memory provider for
installation into the Python environment that runs Hermes. The PacoMind sidecar
is a separate service. The adapter does not install the sidecar, a context
engine, a worker daemon, or operating-system services.

Build from the repository root:

```sh
python -m pip install build
python -m build
```

Install the resulting wheel with the Python interpreter that runs Hermes.
Use a build directory containing only the wheel for the selected source revision:

```sh
python -m pip install dist/pacomind_hermes-*.whl
```

The wheel exposes `pacomind` through `hermes_agent.plugins` and `pacomind-memory`
through `hermes_agent.memory_providers`. Only the canonical `pacomind_hermes` and
`pacomind_memory` packages are shipped. It maps the source files in
`plugins/hermes-plugin/` and `plugins/pacomind-memory/` to importable packages.
Only `catalog.py` and `contract.py` from `hostworker/pacomind_hostworker/` are
included in the adapter's private catalog package. The source installer forwards
to the guided, profile-aware installer.

## Bundled skills

The adapter wheel includes `pacomind-deep-research` for cited investigations and
decision reports, and `pacomind-skill-creator` for creating useful, concise Hermes
skills. Both use the deployment's available tools without selecting a model or
provider. New guided `pacomind init` attachments install them into the selected
profile's native skill catalog.

For an existing Hermes profile, install or explicitly refresh the bundled copy
without instance setup, inference or configuration changes:

```sh
pacomind init --skills-only --hermes-home /path/to/selected/hermes-home
```

The command reads the installed `pacomind-hermes` distribution. Use
`--adapter-wheel /path/to/pacomind_hermes-VERSION-py3-none-any.whl` to select an
exact built artifact instead. For each packaged `pacomind-*` skill it writes
`skills/<name>/SKILL.md` and its local `.pacomind-owned.json` hash record,
retaining previous bytes in local backups on refresh. An unowned
destination or modified bundled copy is preserved and reported as a conflict.
Move a customized skill directory aside and give it a different name/path before
installing the bundled revision. Other skills are untouched. An adapter refresh alone does
not replace the profile's skill; run `--skills-only` explicitly for that update.

Hermes advertises the name and short description in its compact skill index;
`skills_list` discovers it and `skill_view(name="pacomind-deep-research")` loads
the full instructions on demand. The installer does not inject the body into
every prompt or enable otherwise disabled skill tools.

With this adapter version active, PacoMind refreshes skill discovery in ongoing
conversations. Before a model request it checks the current native skill
locations and disabled state, hashes changed files including instruction bodies,
and invalidates the native index/list caches when their inputs change. This
covers ordinary profile skills created with Hermes tools as well as bundles.
Unchanged skills retain their caches. A compact request-only notice supplies
changed descriptions, identifies removed or disabled skills, and asks for a new
`skill_view` before using stale loaded instructions. A current complete native
tool result clears that skill's reload notice. Full bodies still arrive only
through native skill loading.

Native loading can preprocess templates, so returned instructions need not
equal the raw file bytes. A new successful native load after PacoMind observed
the same unchanged file version also settles the notice. A restored transformed
copy without that observation requires one reload; PacoMind does not execute
template commands to compare it.

Refresh requires `skill_view` to be available directly or named in Hermes'
current deferred-tool catalog. The default native configuration exposes skill
tools directly. Explicit deferral works with full or names-only catalogs; a
group-only or omitted catalog cannot establish an individual tool's availability,
so PacoMind does not infer access or announce reloads from those summaries.

Hermes' original session prompt and historical tool results remain stored
unchanged. Current discovery notices explicitly supersede their stale skill
information; no global conversation rewrite or new chat is required. Selecting
this adapter code initially still uses the normal runtime activation lifecycle.
Afterward, skill file changes require no process restart. Installing skills alone
does not enable PacoMind in an otherwise native-only profile; that profile retains
Hermes' own cache behavior. The installer never restarts a process.

Built-artifact tests exercise native discovery and deliver the loaded skill
through the real Hermes tool loop into a controlled SDK request. This proves
instruction delivery, not research quality on a particular model.

## Activation and current limits

Installing the wheel makes the adapters discoverable. It does not change a
Hermes profile, select a memory provider, or enable tools. Activation requires
the existing general-adapter configuration, `plugins.enabled: [pacomind]`, and
`memory.provider: pacomind-memory` in the selected private profile. Preserve
other enabled plugins when editing that list. These two durable selections make
the general plugin the canonical writer and keep the memory provider read-only,
including cold native workers that do not inherit launcher environment flags.
An explicit `plugins.disabled: [pacomind]` or an enabled list excluding `pacomind`
takes precedence over inherited flags. A contradictory configured provider
`turn_writer: enabled` is rejected; native `pacomind-memory.json` settings retain
their precedence over legacy `memory.config`.

Older embedded profiles without this paired selection still require the existing
coexistence settings in the Hermes process environment:

```sh
PACOMIND_GENERAL_PLUGIN_ACTIVE=1
PACOMIND_MEMORY_WORKER_TOOLS=0
PACOMIND_MEMORY_TURN_WRITER=disabled
```

Configure the sidecar URL and contact through native `hermes memory setup`
and matching `plugins.pacomind` configuration, and supply `PACOMIND_API_KEY` privately.
Native setup stores non-secret fields in the selected profile's
`pacomind-memory.json`, which overrides inline `memory.config`. The
general adapter needs a private writable turn outbox and verified participant
bindings. Consequential tools retain their existing mediator requirements.
This packaging change does not provision those dependencies.

Do not silently replace a selected external memory provider. Existing
directory installations also need explicit migration: Hermes gives directory
memory providers precedence over pip providers, while a pip general plugin can
override a same-name directory plugin. Remove or archive obsolete plugin
directories only as part of an intentional profile migration.

When the memory provider is selected, native CLI discovery exposes
`hermes pacomind-memory status`, `goals`, `context`, and `sync`. These commands
resolve the same selected profile settings and credentials as the provider;
explicit URL/contact arguments remain available. The Typer app remains available
to existing callers.

Profile settings and handoff files stay scoped to the selected Hermes home.
The provider remains attached through a sidecar startup outage and retries on
later requests. Use `pacomind init` for guided attachment and explicit refresh of a managed
installation. Packaging alone does not establish production readiness.

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

## Already captured host input

From 1.0.32, a host that has already admitted the human input can wrap the
existing native conversation in `pacomind_hermes.input_provenance.supplied_input`.
The host validates its participant, source hashes and current revisions first.
The native participant resolver and tool authority remain authoritative; the
supplied contact does not grant capabilities.

Hermes may retain the original message for display while sending a derived
task request. The adapter records the resulting assistant source with exact
input and source dependencies, without recording the task wrapper as another
human statement. Inherited source handles are labelled as unopened evidence
and become usable only after their exact host block reaches the actual native
request. Existing request, tool and writer erasure checks still apply.

From 1.0.33, validated supplied-input replies are retained even on a native
platform excluded from automatic ordinary turn capture. The exception writes
only the dependent assistant source. Ordinary excluded CLI turns and callers
without valid native participant authority remain excluded.

The context exposes the durably queued result's dependencies for the host to
revalidate before delayed delivery or further effects. It does not confirm
backend delivery or reconstruct unlinked historical paraphrases. See the
[supplied-input contract](../plugins/hermes-plugin/SUPPLIED-INPUT.md), also
included as `pacomind_hermes/SUPPLIED-INPUT.md` in the adapter package.

Hosts can also register [source-bound updates during native work](NATIVE-SOURCE-UPDATES.md)
on this same input context. Registration, request visibility and behavioral
application are separate; native task execution stays with Hermes.

## Source erasure and replay

`POST /v1/host/memory/sources/forget` accepts an authenticated contact and 1 to
100 canonical source IDs. The existing MCP server exposes `pacomind_forget_sources`.
The native plugin exposes `pacomind_memory_forget` for explicit owner requests in
an attested interactive turn. It accepts source IDs from canonical recalled
provenance, including older sessions; legacy graph memory IDs are not source IDs.
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

New ordinary communication summaries retain the canonical contact, turn, session
and exact message hashes through the existing source-lineage reader. Erasure
removes their incoming and outgoing summary rows. Startup and explicit erasure
perform full reconciliation. Reads reconcile existing erasure IDs, then validate
only the prose rows selected by the query; count/date aggregates do not parse
original conversation bodies. Writes check their source before and after insertion.
Linked communication data requires an explicitly supplied canonical ledger.
Opening an offline copy without that binding reports an error when linked data
is read or reconciled; it never treats a different process-default profile as
evidence of erasure. Existing unlinked-only reads remain compatible.
Transport receipt metadata and unrelated contact history remain. The additive
`communications_cleanup` field reports only `source_linked_summaries_only` scope.
`communications_unlinked_rows` counts same-contact non-receipt summaries without
lineage. Those rows remain: matching their prose, session or time is insufficient
to establish a source. Summary-only turns without a canonical source no longer
add unlinked prose to this ledger. Older readers ignore the new column and do not
provide this cleanup behavior; retain a reader with this change when rolling back
if communication-summary erasure must remain active.

Cleanup status is separate from canonical erasure. `complete` means the named
cleanup ran successfully, `pending` means it failed, and `unavailable` means its
store was not available. An explicitly disabled graph or embedding store reports
`disabled_not_checked`, which makes no claim about bytes retained on disk. The
existing `host_reconciliation` field reports `not_observed`: this response exposes
an erasure-feed watermark but does not measure which hosts have applied it.
Host outboxes and request middleware still reconcile their own copies on contact.

New native answers retain the authorized canonical source revisions supplied in
their recalled context. Removing one revision also removes recorded dependent
assistant messages, including chains recalled in later sessions. This records
conservative input dependency, not proof each source influenced every word: an
entire assistant message can disappear even if it also contains other useful
material. Independent user messages remain. After partial erasure their new source
revision can support future answers. Generated citations and user-typed context
markers cannot establish these dependencies; historical unlinked paraphrases are
not reconstructed. Other transports need to forward the same structured references
before their answer copies have this guarantee.

The host outbox migrates its existing v1 database transactionally to v2 with a
separate contact-bound erasure watermark. Canonical turn/checkpoint delivery
fetches `/v1/host/memory/sources/erasures` before PUT. A missing endpoint, outage,
incomplete page or server history behind the host cursor holds replay. The host
purges both pending payloads and delivered receipts, preserving unrelated pending
messages as evidence-only checkpoints. Authenticated ordinary survivors retain
their sender and person scope through a source-only route, which schedules user
claim projection without replaying summary/tool/relationship effects. A predecessor
server rejects that dedicated route, so its host must keep the survivor queued.
Ordinary answers carrying source references also use a dedicated route, preventing
a predecessor backend from silently accepting an answer while discarding its links.
Erased IDs cannot be enqueued again after
reconciliation. This is not a model-generation counter. Generic caller-provided
outbox delivery callbacks must use `PacoMindClient.sync_turn(..., outbox=outbox)`
to participate in reconciliation.

Partial erasure events retain exact message hashes under opaque event IDs in the
existing cursor sequence. Predecessor readers still filter those message copies
without treating surviving user sources as wholly deleted. Downgrading preserves
completed erasures, but predecessor writers do not record new answer dependencies.

This is scoped source erasure, not a claim of global forgetting. Persisted native
Hermes transcripts and `api_content` bytes, backups, prior graph records without source lineage,
legacy shared facts, ToM, commitments, and other old derivative stores need their
own erasure adapters. Offline hosts retain bytes until reconnecting; filesystem
snapshots and physical-media remnants are outside this logical-delete contract.
Erasure history must survive backup restores; replay detects an older watermark
but cannot prove that a replaced server with reused sequence numbers is equivalent.

## Qualification

Pull-request CI keeps an exact qualified Hermes commit pinned. The separate
`Latest Hermes compatibility` workflow runs daily and can be dispatched manually.
It selects the latest published stable upstream release, records its exact Git
commit and runs the same built-artifact/native-profile tests. It uses disposable
profiles and controlled inference, without deployment credentials or a live
agent. A compatibility failure leaves production and the qualified pin intact.

When a new upstream release passes, update the pin in an ordinary reviewed
change and qualify the deployment's actual channels and recovery before moving
that deployment. The scheduled result catches upstream drift; it does not
establish production readiness or automatically upgrade a running agent.

The qualification target is the [Hermes 0.21.2 compatibility build](HERMES-HOOK-COMPATIBILITY.md),
based on tag `v2026.9.11`, commit `939e45c91d751fadd94dcd1b873ac3cb44846213`,
tested on Python 3.12. The adapter uses Hermes' native task and skill modules.
Retained older-runtime fallbacks are outside this qualification target. The package
allows Python 3.11 through 3.13; those other interpreters are not yet qualified.
Other Hermes
releases are unqualified until the native-loader checks pass against them.
Hermes is installed separately; this package does not select or upgrade it.

The additional native provider-call memory boundary is qualified on Hermes
0.21.1 with NeMo Relay 0.8.3 and the Linux 0.21.2 qualification environment
with NeMo Relay 0.8.4. Upstream frozen environments can select another version;
qualify that actual interpreter before switching a deployment. Install it with
`python -m pip install 'pacomind-hermes[native-memory]'`; the supported Hermes
release also declares this Relay dependency. CI installs that extra explicitly.
Missing scope-local Relay capabilities leave the ordinary adapter active and
emit a warning when the native boundary cannot be registered.

Hermes's maximum-iteration summary rebuilds historical `api_content` and bypasses
`llm_request` and ordinary API observers. PacoMind binds its existing authenticated
source-validity and erasure filter to that native turn's scoped Relay execution
contract. The SDK receives filtered history for the summary, including its
empty-answer retry. A one-use digest avoids repeating the ordinary request
filter; another physical attempt without a new ordinary check is revalidated.
The boundary also covers native streaming calls through the same Relay scope.
It neither rewrites stored transcripts nor creates new recollection. Joined
children use their own participant state, and completion or native scope teardown
removes the registrations. No process-global Relay configuration is activated.
An exact erased user or assistant source also withdraws its historical
user-to-next-user segment from provider input. Its intervening tool arguments,
results and reasoning are withheld together, with a small forgotten-source
placeholder. The current native-observed input stays available even when Hermes
adds a synthetic summary nudge, including when the user intentionally retells a
fact. Unaffected historical turns remain available. This uses retained source
hashes and observed content aliases, not word matching or inferred dependencies
across unrelated turns.

This is provider-input coverage on those dispatch paths, not erasure of native
transcript files, Relay exports, arbitrary paraphrases, or calls outside them.

Hermes 0.21.1 wraps provider recollection in a note calling it authoritative
reference data. PacoMind's supported `llm_request` middleware replaces that outer
note only in an observed, current, source-stamped native memory suffix. It keeps
the person’s input, quoted source bytes, source revisions and erasure checks
unchanged. Recollection retains its speaker, time and factual, reported,
fictional or hypothetical scope; finding a quotation does not verify its claim.
The shared memory section header carries this guidance to direct consumers such
as voice adapters that do not use the Hermes envelope. Unknown envelope formats
are unchanged. The native request test exercises the actual upstream wrapper,
so a framing change requires renewed qualification rather than a core patch.

For an authenticated host task, the native request may contain a task wrapper
while `persist_user_message` stores the original person's text. The adapter binds
the actual current native row before Hermes appends context. This preserves the
same evidence framing and source lineage when the two input forms differ.

System/developer instructions and Responses instructions may document the generic
`<memory-context>` fence. That markup alone is not a recalled packet there, even
when the example omits a closing tag. Exact forgotten source copies and explicit
PacoMind lineage packets still reconcile in these fields. Legacy untagged fenced
instruction text is preserved unless it is an exact erased source or observed
alias; substring and paraphrase erasure are not promised. Native automatic
recollection continues to reconcile in its appended user-content boundary.

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

## Opening retained task results

`pacomind_memory_read_source` accepts `view="observations"` for an exact recalled
instruction. It lists four retained original tool references per page. Open
relevant references with `view="source"` to read what the tools actually returned.
Selection reasons are model-authored navigation hints and may be wrong.

Every source page exposes the selected record's `reported_at` and `recorded_at`
outside its paginated content, with `evidence_basis: "retained_record"`.
Missing report times remain unknown. A directory's outer times describe its
originating instruction; an entry's `observed_at` is the original native tool
time and its `recorded_at` is ingestion. Neither opening nor recent ingestion
makes an older document current. Source versions and read revisions identify
retained evidence, not the current state of its subject. Use supported historical
or stable facts directly; current-state advice needs evidence that applies now.

This directory follows only the current observation writer's first origin
reference, with current participant scope, source revisions and native result
digests checked. A shared session or another dependency does not establish that
link. Original task arguments are not reconstructed. Missing retained results
cannot be recovered through this view, and a complete directory never establishes
that every task result was retained or that the task succeeded.

`next_offset` and `read_revision` continue a page. Changed membership or corrections
require restarting at offset zero. More than 64 candidates returns
`cohort_limit_exceeded` without result references. Directory entries and their
attributed corrections are never split to fit: oversized corrections make the
read unavailable. Result bodies are not automatically added to ordinary recall.

The directory and explicit opening pass the native reader fixtures. Whether the
extra tool steps improve ordinary task-outcome answers remains pending model
qualification. Observation ancestry now also carries parent corrections and
participant invalidation; corrected recall content can consequently differ.

## Shared execution observations

On the [current qualification build](HERMES-HOOK-COMPATIBILITY.md), set
`plugins.pacomind.execution_registry_enabled: true` to
publish native turn, API, tool and delegated-child lifecycle observations.
This is opt-in for a current attachment. The adapter credential must already
have scoped `turns:write` and exact person grants; `context:read` grants access
to the corresponding view. The legacy global bearer does not attest identities
for this new surface. To observe trusted agent cron fires, include `cron` in
the existing `attested_system_platforms` configuration; it is not enabled by
the adapter's default `cli` binding. No network call is made during registration.

PacoMind stores only execution IDs, participant/session linkage, channel, phase,
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

The same owner view reads native Kanban tasks, including general `goal_mode`
work, without dispatching or reconciling them. `PACOMIND_HERMES_WORK_BOARDS` can
select up to eight board slugs as a JSON list, for example
`["default","pacomind-drafts"]`. Without that setting it follows only the
selected home's native current board (`HERMES_KANBAN_BOARD`, then
`kanban/current`, then `default`), including Hermes' lowercase normalization.
It does not enumerate other boards. Profile
homes use Hermes' shared root for board paths; conflicting explicit native
home/database overrides are unavailable.

Each task includes a title capped at 128 characters, quoted as operational
data, native task/run IDs and states, configured goal mode/turn budget and
record timestamps. Bodies, arbitrary result prose, claim tokens and worker
PIDs are excluded. The per-request native work refresh includes these titles
and states, so another owner session can identify an undertaking and observe
its later terminal record. Goal budgets describe configured limits, not
remaining turns or evidence of completion. Running rows retain unknown
process liveness; terminal rows do not prove an external effect. Missing
selected boards, partial board coverage and omitted rows remain visible.
Recent terminal records are bounded to seven days using their actual terminal
timestamp. Archiving unfinished work uses its native archive event for
`terminal_record_at` and leaves `completed_at` unset. Guest context receives no
Kanban board rows. Hermes retains task creation, goal continuation, completion,
recovery and all execution authority.

New dispatcher-owned completions can also retain their finalized native run
summary as explicitly machine-authored, unverified assistant evidence. The
`kanban_task_completed` hook re-reads the completed native run while the actual
`kanban_complete` tool still holds its attested CLI participant context. It copies
the canonical source revisions actually supplied to that request, without using
generated citations. Task bodies, worker instructions, continuations and artifact
files are not forwarded. A missing scope or request-lineage observation leaves
the report native-only and records a structural log reason; old completions are
not backfilled.
This separately scoped assistant-report hook does not depend on which platforms
enable ordinary conversation capture; excluding CLI user turns still retains
eligible native completion reports.
An unrelated dispatcher-owned board worker omits the profile's draft-board
controller and rejects local-draft acceptance before any handoff. Native's
worker DB pin stays intact; the selected draft worker still requires its own
board and profile. General scope, request lineage and completion hooks remain
available in the ordinary worker.

The existing outbox and source-only route retain this assistant summary for
scoped lexical and semantic recall after the seven-day operational window.
They do not run ordinary turn cognition or promote it into a user's claims.
Repeated callbacks reuse one native-home/board/task/run identity, including after
a lost acknowledgement. Erasing a supplied parent removes the dependent report;
native task and session history remain under their existing retention policy.
Queued delivery is not proof of central availability, and a reported result is
not independent verification that its claimed effects occurred.

An optional private `PACOMIND_WORKER_STATUS_PATHS` environment value maps neutral
worker labels to local JSON heartbeat paths, for example
`{"Local transport":"/private/runtime/transport-heartbeat.json"}`. It is unset
by default. The same owner API and context join expose these as separate
`reported_worker` entries, never executions or success receipts. Heartbeats
supply `state` and numeric Unix `updated_at`; optional `detail_code` and
`release_commit` are included. Existing heartbeats remain compatible. The reader
also retains optional `task_id`, `parent_task_id`, `worker_id`, `kind`,
`started_at`, `finished_at`, and integer `exit_code`. A parent ID is a reported
binding, not permission to act for that parent. Each row includes `status_sha256`
to identify the exact observed report bytes. Configured status paths, process
arguments, environment values, logs and arbitrary result bodies are omitted.

Producers can include up to four measured `progress` records:
`{"completed":3,"total":8,"unit":"files","source":"pinned_manifest_metadata","observed_at":1234}`.
`total` is optional; the other fields are required. Counts must be nonnegative
and finite, and a supplied total cannot be less than the count. The source and
measurement time must describe the actual observation. Reading artifact progress
must not refresh an old process heartbeat or imply an unmeasured transfer rate.

Up to four `result_refs` can contain `kind`, an opaque `reference`, and optional
SHA256 `sha256`, numeric `observed_at`, and a short `verification` description.
For example, a reference can identify a retained process exit receipt or artifact
report. The reader never follows those references or verifies the referenced
content; it reports the producer's stated scope of checking. Explicit result
locations therefore belong only in operator-selected private reports. Automatic
request context retains these same bounded work fields and references, subject
to its existing overall context limit. The complete scoped API remains available.

The reader reports age and marks reports older than 120 seconds stale; future
timestamps and unreadable/malformed files remain unknown or unavailable.
States such as `uncertain` are preserved even when stale. A task with a valid
finish timestamp and explicit `exited`, `completed`, `failed`, `interrupted` or
`cancelled` state is labelled `terminal_report`; otherwise it is a
`progress_report`. An old terminal report remains intelligible after a session
disconnect. A stale running report or missing file never becomes completion.
Process liveness and
external effects remain unverified. Reads are limited to eight configured
workers and 16 KiB per heartbeat; no store, heartbeat writer or poller is added.
Guests receive no configured worker reports.

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
or unknown plugin/MCP tools. The exact PacoMind tools keep their existing scoped
read checks and action mediators. No arbitrary tool prefix or relationship
score grants access.

A native-tool request from a guest returns `requires_authorization`, with no
effect and no approval created. The response directs the agent to an enabled
PacoMind action request or the owner. It does not invent an approval request or
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
PacoMind intents by this gate. Qualify both owner and guest messages before
enabling a public channel.

## Native post-turn review

Hermes' automatic review reuses its parent's session ID without emitting a
subagent-start event. The adapter captures the exact parent participant on the
native caller thread and accepts inheritance only with Hermes' trusted review
provenance, matching parent/session, and a distinct task/turn. Later speakers in
the same session cannot upgrade a queued guest review. Reviews get a separate
work record linked to their parent, do not replace the session's current speaker,
and do not write their internal harness into ordinary conversation evidence.

On 0.21.2, request middleware binds the detached review using the copied parent
scope and trusted native origin. Trusted native input aliases and authenticated
source-read receipts are carried from the parent, without promoting the review
prompt into human evidence. Host-supplied tasks retain their existing source
bindings. The compatibility build's `on_detached_turn_end` observer reports exact
success, failure or interruption and releases the review's transient memory state.
Ordinary user/session ingestion hooks remain skipped. Earlier supported runtimes
retain their existing lifecycle path. See the [interface cost and removal
conditions](HERMES-HOOK-COMPATIBILITY.md).

For owner/system reviews, `skill_manage` produces a proposal in Hermes' existing
`pending/skills` store. Native curator ownership checks still apply. Foreground
owner requests retain native behavior. The proposal is not an evaluated or
activated improvement: an explicit operator or a separately configured task
evaluator must use the native apply path. Mutation history and rollback remain
in Hermes' skill ledger. No new consent transport or learning store is added.
Batch proposals require an actual nonempty array of at most 20 operation objects
with supported actions and target names; deletion must be the sole operation.
Encoded or malformed strings are rejected
before staging; the adapter does not repair them silently. Valid native legacy
flat operations remain supported. Existing pending records are left intact.

Installed-wheel qualification runs real native automatic review threads with
controlled inference for owner and guest, including a later-owner race in the
guest's session. It checks proposal persistence, exact inherited authority,
separate work observation, absent harness capture, and explicit native
apply/ledger rollback. It does not claim measured task improvement from that
controlled model fixture.

### Measured skill updates

`python -m pacomind_hermes.review_evaluation --skill NAME --pending ID
--oracle trusted_local_module:function` evaluates one explicitly selected,
existing curator-owned `SKILL.md` proposal. It accepts native full-content
edits or exact text patches, including a one-operation native batch. Text
patches must match their captured original content without ambiguity unless
native `replace_all` is explicit. Support-file changes, multiple-operation batches,
new skills and unrelated proposals stay in Hermes' ordinary pending mechanism.
The selected local oracle is trusted operator code, not a model tool or a
model-selected command. It receives the playbook text and a phase and must bound
its own task execution. It returns explicit case IDs and boolean outcomes.

Baseline and candidate must run the same cases; every candidate case must pass
and at least one must improve. The adapter records task evidence, the proposal
hash, oracle identity and before/candidate files in the native skill ledger,
then calls native apply. It repeats the task after activation, permitting
additional held-out checks while requiring all original cases. A failed or
unavailable initial check invokes native rollback. `--audit EVALUATION_ID` repeats
an existing candidate check later, using the same oracle implementation. Once
the ledger records a completed activation, an unavailable later audit records
`unavailable` and retains the last qualified files. A completed task regression
still invokes native rollback. Infrastructure failure is not a failed task case. This is
an operator/cron entry point, not an additional background service.

The proposal records its original file hash. A stale proposal or later owner
edit is held rather than overwritten. A candidate ledger entry is written
before apply and remains a recovery target if the process exits between apply
and evaluation; retrying the pending ID resumes its post-check. Rollback checks
all captured current files against the candidate snapshot first and restores
only native skill files. It does not rewind memory, permissions or action
records. These byte checks do not lock editors or foreign processes, so select
a curator-owned path without another concurrent writer for automatic rollout.
Native curation usage counters remain native telemetry after file rollback.

Qualification tests exercise real pending files, native curator ownership,
mutation/rollback and ledger recovery with controlled task outcomes. A selected
oracle only establishes performance on its declared task cases. No evaluator
or automatic rollout is enabled by default, and this contract does not qualify
arbitrary code changes or prove general self-improvement.

A disposable local-inference qualification also exercised an actual native
review and exact text patch: its declared task scored 0/2 before, 2/2 with the
candidate, and 2/2 after activation. An injected evaluator outage restored the
original bytes through native rollback. This was a neutral fixture, not a live
recurring skill or an observed spontaneous model regression.

### Bounded operational reviews

New `pacomind_work_initiative` reviews require a managed `pacomind-reviews`
native profile. The existing guided `pacomind init --profile local` setup offers
an optional review choice, default off; `--native-reviews` selects it explicitly.
Existing attachments can use that same flag and their configured planning role.
`--refresh-adapter` refreshes an already enabled review profile without enabling
a previously disabled one. For private deployment staging, use the selected sidecar interpreter:
`python -m pacomind.setup_native_reviews --install /private/instance`.
This prepares the profile and enables `plugins.pacomind.native_reviews` in the
selected native root configuration. An existing profile belonging to another
instance is retained and installation fails. Named conversation profiles must
select their root deployment for this shared worker.

When enabled, the existing native dispatch tick discovers at most five new
server-eligible read-only proposals and reconciles already bound work. A separate
LLM queue steward or scheduling service is unnecessary. Disabling the choice
stops new discovery while preserving observation of existing bindings.

The profile exposes two tools: `pacomind_read_work_source` and
`pacomind_review_report`. Native `agent.disabled_toolsets: [kanban]` removes
the automatically added Kanban tools, including task creation and attachment
access. Native `tools.tool_search.enabled: false` keeps both tools directly
visible to the model; readiness checks the assembled schema array. The report
wrapper completes or blocks only the worker's current
claimed task through native lifecycle handlers. It accepts text, not files,
metadata, task identifiers or new work. Scratch artifact references in report
text are rejected because Hermes otherwise infers attachments from them.
Finite reviews use ordinary native completion and automatic heartbeat. They do
not start recursive goal-judge continuations: report usefulness is measured
separately, and judge acceptance does not establish factual quality.

Source 0 returns the registered observation. For operational log reviews,
sources 1 through 5 select that observation's `largest_files` list, restricted
to the configured log directory. Each read measures current file size,
modification time and filesystem capacity and returns at most 16 KiB of
redacted text. Writer and retention configuration are explicitly marked
unavailable. Observation and modification timestamps are also rendered in UTC;
timestamps inside log text may use another timezone. Neither a sample nor file
age proves service liveness, historical volume causality or recovery readiness.
New task contracts require one evidenced next step and verification while
preserving logs. A useful report can identify unavailable configuration as the
next bounded inspection. Existing bound contracts retain their exact body and
digest; only new bindings use the bounded report-tool wording.
The instance may set `operational_log_directory`; its
default matches the existing producer's `~/.pacomind/logs`. A mismatched
registered directory is unavailable, never substituted with another log.

Before promotion, the adapter refreshes this profile from the existing
`planning` function role and verifies actual native discovery exposes exactly
the two tools. Missing installation, a missing manifest, failed imports or
unavailable role configuration leave new work undispatched. No initial model
output cap is added. Current owner conversations and shared tasks keep their
own profiles and tools.

Historical default-profile review tasks remain observable and are never
automatically promoted; a queued old review is blocked. Follow-up review
dispatch is currently unqualified and held until bounded reports integrate
with its existing governed outbox. Historical completions and matched-reply
cancellations still reconcile. This restriction does not stop ordinary owner
messages or create an additional consent service.
