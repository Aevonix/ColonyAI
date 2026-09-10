# Create or attach a private Hermes agent

The supported lightweight path uses Hermes **0.21.1** (qualification commit
`2237be355906fbe6065ce1815711eee52b2d646e`) or **0.21.0**, Python 3.11 to 3.13, and one local
OpenAI-compatible chat endpoint. Install Hermes separately using its
[native installation guide](https://hermes-agent.nousresearch.com/docs/getting-started/installation).
Colony does not patch or download Hermes, models, containers or machine services.

Install the matching published packages in a private Python environment. This
path needs no Colony checkout or source edits. The environment may be shared
with Hermes, but the commands below keep an existing Hermes installation intact:

```bash
python3 -m venv "$HOME/.local/share/colony/venv"
source "$HOME/.local/share/colony/venv/bin/activate"
python -m pip install "colonyai[hermes]==1.1.4" "colony-hermes==1.1.4"
colony init --hermes-python /path/to/hermes/.venv/bin/python
```

Use a Python version supported above. Replace the interpreter placeholder with
the Python from the Hermes runtime you actually run. Keeping both Colony
packages at the same version avoids attaching an older adapter to a newer
sidecar. `colony init --help` lists the wizard's optional and unattended flags.

A separate Hermes environment needs its own native core dependencies. It does
not need Colony's CLI dependency `typer` or a preinstalled Colony adapter; setup
can attach the private adapter directly from the Colony environment.

The wizard asks for your name, the agent's name, the model API root and model,
whether to enable accepted local drafts, whether to enable general native tasks
(default no), then whether to start Colony. An API
key is prompted without echo. For unattended
setup use `COLONY_MODEL_API_KEY` in the process environment, never a command-line
key. A model that requires no key works too.

```bash
colony init --non-interactive \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --hermes-home "$HOME/.hermes-orion" \
  --agent-name Orion --contact-name Owner \
  --model-url http://127.0.0.1:8000/v1 --model my-local-model --local-work --start
```

`--hermes-home` wins over `HERMES_HOME`. Without either selection, guided setup
lists live profiles through the selected Hermes runtime and asks which home to
attach. Listing reads profile names and paths, not their private configuration.
Noninteractive setup retains the `~/.hermes` default. Only the selected home's
configuration is inspected or changed. `--dir` selects private Colony state, otherwise `COLONY_STATE_DIR` or
`<selected Hermes home>/colony` is used. Both the selected Hermes home and Colony
state must stay outside Git checkouts, including when `--dir` is separate.
Select that same Hermes home when launching Hermes:

```bash
export HERMES_HOME="$HOME/.hermes-orion"
/path/to/hermes/.venv/bin/hermes
```

Setup checks native runtime imports/version, canonical adapter resources, the
selected model with one neutral completion, the free local sidecar port, and
configuration conflicts before it writes the private instance. `--adapter-wheel`
can select an already-built canonical wheel instead of an installed
`colony-hermes` distribution. This is also usable from built Colony wheels with
no editable source checkout.

The selected model hostname is recorded for runtime routing, and setup checks
its addresses with the router's existing local-network rules. A LAN hostname
can therefore serve extraction as well as the initial chat probe. Runtime calls
continue to resolve and check that configured host when its address changes.

## WhatsApp read receipts for a selected profile

For an existing Hermes home, preview and then apply the supported channel
preference without initializing an identity, attaching Colony, probing a model
or restarting a service:

```bash
colony init --hermes-home "$HOME/.hermes-orion" \
  --preferences-only --whatsapp-read-receipts on --preview
colony init --hermes-home "$HOME/.hermes-orion" \
  --preferences-only --whatsapp-read-receipts on
```

Use `off` to disable receipts. Omitting the option preserves the existing
setting. Preference-only mode requires an existing `config.yaml` and rejects
instance/setup options. It does not require a Colony instance manifest.
`--hermes-home`, then `HERMES_HOME`, then `~/.hermes` selects the profile in this
mode. The preview lists changed setting paths without displaying other config
values. Applying uses the existing atomic writer and retains the exact previous
config in a private `.config.yaml.colony-backup-*` file; repeating an unchanged
choice adds no backup.

The preference is qualified against Hermes 0.21.1 at the commit above. It writes
native `send_read_receipts` settings in the profile's existing layout, including
existing alternate spellings that could override each other. Channel enablement,
sender/group policy, credentials and unrelated extras are retained. A profile
without explicit WhatsApp channel selection or existing connection settings is
rejected: adding preference-only extras can otherwise implicitly enable a stock
Hermes channel. Select `enabled: true` or `enabled: false` explicitly in that
profile's WhatsApp config first. An explicitly disabled channel stays disabled.
The option also works during ordinary init when that channel selection already
exists; ordinary init still performs its documented setup checks.

Use the selected gateway's normal reconnect/restart procedure to load the new
setting. This command changes config only. Native receipts apply to accepted
incoming messages and remain subject to WhatsApp account privacy behavior;
they do not retroactively mark a backlog as read. Read receipts do not imply
online status. Custom bridges may expose their own presence configuration.

## What the local profile enables

Ordinary native turns enter the durable outbox and canonical source ledger.
Source quotations, temporal claim projections, contacts, commitments, execution
observations and self state use the existing SQLite stores. Text recall uses
lexical retrieval; it needs no embedding model. The native memory provider
injects selected evidence before inference. Compression uses Hermes' native
checkpoint contract with durable local capture. General-plugin turn capture is
the single ordinary writer.

Ordinary sources also support cited appraisals and revisable working judgments.
They do not trigger a second legacy mood-analysis call or inject numeric mood
estimates into conversation context. Existing affect history and explicit affect
APIs remain available for inspection.

The selected model supplies the legacy SMALL, MEDIUM and LARGE role bindings.
Optional named role/capability configuration can refine that later. Fresh homes
also get that model in native Hermes config. An existing Hermes model is kept.

Accepted local drafts are optional. With `--local-work`, setup checks function
calling and creates a named `planning` role bound to the selected local model.
It creates a native `colony-drafts` board and constrained worker profile.
The selected Hermes gateway dispatcher must be running.
The board remains idle until the owner accepts a specific question and local text
sources through `colony_accept_local_draft`. No existing commitment is required;
an optional commitment ID associates the draft with a broader obligation.

The worker reads those sources, produces a cited draft and retains its execution
and report in the instance. It cannot send the draft or change its source files.
The gateway refreshes its planning profile from the current function role before
promoting new work and on nonempty dispatch ticks. A running attempt keeps its
selected snapshot; subsequent attempts use the refreshed profile. The
two environments need no shared dependencies. See
[accepted local work](ACCEPTED-LOCAL-WORK.md) for limits and cancellation.
If native registration fails after attachment, rerun the same command with
`--local-work`. It resumes the prepared profile using the retained planning
role, identity and credentials. Restart an already-running Colony instance and
Hermes gateway to load the new binding. Existing cron assignments drain before
their old draft job is paused. An older instance without a planning role or compatible
adapter needs explicit configuration or an adapter upgrade first.

Graph/vector retrieval, embedding downloads and consequential background workers
are disabled in this profile. Install `colonyai[graph,vectors]==1.1.4` only when
adding those services intentionally. Model quality still determines extraction and
reasoning quality. Lexical retrieval does not promise semantic recall of every
paraphrase. This setup is a growing local base, not a claim that every autonomous
behaviour or public channel is ready.

## Persistent native tasks

`--native-goals` is a separate opt-in for general background tasks, including
native `goal_mode` continuation. It works on a new attachment or an existing
private instance:

```sh
colony init --non-interactive --hermes-home "$HOME/.hermes-orion" --native-goals
```

For a fresh attachment, also supply the interpreter, identity and model options
shown above. Unattended init without `--native-goals` or `--local-work` retains
the memory and observation profile. The interactive accepted-draft choice keeps
its existing default; general native tasks default to no.

Setup adds `kanban` to the existing profile's global toolsets and CLI selection,
preserving other selections, identity, model and authority configuration. Hermes
gates Kanban globally: enabling it can expose task tools to authorized turns on
other channels of the same profile even when those channels have saved tool
lists. Guest authority remains constrained by the existing Colony middleware.
This is not blanket consent for consequential external actions.

The agent can use native `kanban_create` with the existing profile as `assignee`,
`goal_mode: true` and a chosen `goal_max_turns`. Setup prints that profile name
and selects the current native board plus the exact accepted-draft board, when
installed. An existing explicit `COLONY_HERMES_WORK_BOARDS` list is retained;
other boards remain outside the observation view. No board is enumerated or
created by this opt-in, and no new worker profile or executor is added.

The selected Hermes gateway must be running to dispatch tasks. Colony does not
start or restart it. For a new profile, run it in a separate terminal using the
same interpreter selected during setup:

```bash
HERMES_HOME="$HOME/.hermes-orion" \
  /path/to/hermes/.venv/bin/python -m hermes_cli.main gateway run
```

For an existing deployment, use Hermes' existing host lifecycle after configuration;
setup reports an explicitly disabled dispatcher configuration or environment
override instead of silently overriding it. Enabling config is not proof that
the gateway has acquired native dispatch ownership or completed a task.
Existing `HERMES_KANBAN_HOME` and `HERMES_KANBAN_DB` overrides must match the
selected native root and observed board; conflicting paths are rejected before
configuration changes.

An explicitly configured `auxiliary.goal_judge` is preserved. If its routing is
unset or automatic and the main provider/model are explicit, setup binds the
judge to that main configuration snapshot while retaining its limits. Later
main-model changes do not rewrite this explicit judge role. An implicit main
binding leaves automatic judging intact and is reported. Existing native
fallback behavior still applies: this setting does not establish global LAN
confinement. Use the existing Hermes auxiliary configuration to select another
judge later. Accepted local drafts keep their separate constrained worker and
planning role.

## Identity and authority

A real owner contact is created, with a generated exact-person API credential.
The server has no global legacy bearer key. The native local CLI is explicitly
bound to the owner; unrecognized real-channel senders do not inherit that
identity. The wizard does not enroll messaging handles, remote users or devices.
Add those through their authenticated transport integration and scoped grants.
Native owner tools remain available; consequential effects remain subject to
the existing application consent rules. Public guest context needs the existing
scoped projection contract and is not enabled by this local profile.

Canonical adapter bytes are retained in private state. If the selected Hermes
interpreter already has both native Colony entry points, setup verifies their
package bytes against the selected artifact and uses that installed package.
It records the loading mode, package version and source paths in `instance.json`.
A different or incomplete installed adapter is rejected before attachment;
upgrade it explicitly or select its matching artifact or another interpreter.
With a separate interpreter lacking those entry points, tiny profile-local
forwarders load the private adapter copy. Setup does not patch Hermes or install
competing forwarders that its native precedence would ignore. An explicit later
upgrade of a shared installed package affects every home using that interpreter.
Other profiles and running Hermes sessions are not restarted or modified by
attachment. Start a new Hermes session afterward.

For a new home, `SOUL.md` contains the chosen identity. An existing SOUL, channels,
model and unrelated settings are retained. An incumbent non-Colony memory
provider requires an explicit wizard choice or `--replace-memory-provider`;
its data is retained. An existing Colony directory adapter or native JSON config
requires an explicit upgrade rather than being silently replaced.

## Update an existing attachment

Stop the selected Hermes gateway and its workers using their existing host
lifecycle, then stop this Colony instance (`colony --instance /private/path stop`,
or `service stop` for a managed instance). Complete or cancel in-flight work
through Hermes before stopping it. Keep the private instance and Hermes home.

Update both Colony distributions in the environment that runs Colony, selecting
the same release for both packages:

```sh
python -m pip install --upgrade "colonyai[hermes]==1.1.4" "colony-hermes==1.1.4"
colony init --non-interactive --hermes-home "$HOME/.hermes-orion" --refresh-adapter
```

Replace `1.1.4` with the release you are selecting. A Hermes interpreter with
native installed Colony entry points also needs that adapter package updated explicitly in its
own environment before refresh. That package update affects all homes using the
interpreter. Refresh verifies those installed bytes and records the binding;
it does not copy a second active adapter or install packages itself.
Keep the attachment's existing loading mode. Switching between a package
installed in Hermes and profile-local directory adapters requires a separate
migration; refresh rejects that change before writing anything.

The ordinary separate-environment attachment uses a copied adapter. Updating
Python packages alone does not update that copy. `--refresh-adapter` replaces
it with the selected canonical resources, updates the known profile and draft
worker manifests, and retains the previous directory as `adapter-previous-*`.
It retains identity, credentials, config, model roles, databases, native boards
and worker configuration. Local changes to managed adapter files are reported
before replacement. Repeating the same refresh leaves matching bytes unchanged.
The instance records the Colony environment running this command; supply
`--hermes-python` only when deliberately selecting another supported native
interpreter. No model probe, service restart or new consent process runs here.
If the Colony interpreter moved and this instance uses a user service, run the
existing `service install` command from the new environment while the service
is stopped, then `service start`. Refresh preserves the old service definition;
updating the instance manifest alone does not move the service interpreter.

Start Colony and Hermes through their existing lifecycle, then check `status`
and `doctor` and recall a harmless fact from a new session. A package version
alone is not evidence that the attached code or retained memory works. On an
ordinary write failure refresh restores the files it changed. If the process
itself is interrupted, keep both runtimes stopped and restore the retained
adapter directory and corresponding `.colony-backup-*` manifest files before
retrying. Database downgrade or rollback is outside this code-only refresh.

## Develop from a checkout

For source development, install both distributions from the public repository
root in your development environment:

```bash
python -m pip install . ./sidecar
colony init --hermes-python /path/to/hermes/.venv/bin/python \
  --hermes-home "$HOME/.hermes-colony-dev"
```

Keep generated profiles and private state outside the checkout. After changing
managed adapter code, use the same stopped-runtime refresh procedure above;
installing changed source packages alone does not update an existing copied
adapter. Source development is optional for a normal published installation.

## Start, observe and recover

```bash
colony --instance "$HOME/.hermes-orion/colony" start --detach
colony --instance "$HOME/.hermes-orion/colony" status
colony --instance "$HOME/.hermes-orion/colony" doctor
colony --instance "$HOME/.hermes-orion/colony" stop
```

With the selected `HERMES_HOME`, start/status/stop also discover the instance
from `plugins.colony.instance_dir`. They never fall back to another instance's
`.env`. The private instance's `.env` governs startup; edit that file for lasting
changes. Its `sidecar.log` and process record belong to that instance. A busy port
is not permission to stop its occupant. Stop checks the recorded process's
creation time and command before signaling it.

`doctor` checks the selected local instance's files, configured model, HTTP
health and scoped source-job status using its client credential. It does not
require a global administrator credential, graph services or background-effect
workers. Reported extraction errors remain visible; a healthy diagnostic is
not a substitute for the real recollection check below.

For automatic restart and login startup, use the existing CLI's user-service
commands from the Python environment you want to run. First stop any detached
instance process. Installation enables the selected service but does not start
it; `service start` waits for both the manager's process and authenticated HTTP
health before reporting readiness.

```sh
colony --instance /private/path service install
colony --instance /private/path service start
colony --instance /private/path service status
colony --instance /private/path service stop
colony --instance /private/path service uninstall
```

Linux uses `systemctl --user`; macOS uses a LaunchAgent in the logged-in user's
GUI session. Each label derives from the resolved instance directory. Definitions
and logs live under that instance's `service/` directory; the user manager gets
only a link to its definition. Commands never use sudo or change login policy.
Other instances retain their definitions, processes and data. Uninstall stops
and removes this instance's manager registration while retaining its private
state, logs and definition.

These are user services. A macOS LaunchAgent runs while that user is logged in.
A Linux user service's lifetime follows the existing user-manager policy; boot
before login or operation after logout requires an already configured lingering
user manager. The CLI does not enable lingering. See the native
[launchd lifecycle](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
and [systemd user service contract](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

The definition preserves the exact Python interpreter used for installation,
including its virtual environment. To change that environment, stop the service,
run `service install` with the new environment, then start it. The previous
definition is retained as `service/<name>.previous`, and a failed installation
restores its bytes. This does not roll back databases or application releases.
If startup fails, inspect the private `service/sidecar.log`; the manager remains
installed for recovery. An occupied port is an error, never permission to stop
its occupant. The old global launchd command remains only for legacy profiles.

The opt-in `sidecar/tests/test_instance_service_live.py` qualification creates
two disposable instances through the installed CLI and actual native manager.
It checks HTTP turn capture and later recall, kills only one newly created
service PID to verify manager recovery, verifies the second instance stays up,
and uninstalls both registrations while retaining their private data. Normal
unit runs skip this test. Set `COLONY_TEST_USER_SERVICE=1`,
`COLONY_TEST_SERVICE_PYTHON` to the installed Colony interpreter, and
`COLONY_TEST_HERMES_PYTHON` to a supported Hermes interpreter to run it explicitly.
It uses a loopback model fixture; this is process/persistence evidence, not a
model-quality or actual reboot test.

Verify the real loop: tell Hermes a distinctive harmless fact, exit, open a new
session and ask about it. Check the answer and retained source through the
scoped context view. HTTP health and configured modules are not a memory test.
CI performs that actual sequence using the built packages, full sidecar and
native Hermes against a disposable local streaming model fixture; it proves
capture and injection rather than judging a real model's recall quality.

Setup keeps the original config and environment in `colony/hermes-original/`.
YAML values are preserved, but formatting/comments may normalize. New secrets
are kept in private files; generated config contains environment references.
On an attachment write failure, only the installer's exact written bytes are
undone. Concurrent owner edits are retained. Prepared private state remains for
inspection, with no running process restarted. Re-running a completed init
retains it; it is not an upgrade command.

To undo an attachment, stop this instance and Hermes, restore the original
`config.yaml` and `.env` from `hermes-original` (remove only wizard-created files
when no original existed), and remove `plugins/colony` and `plugins/colony-memory`
only if this setup created them in private-directory mode. Keep the private Colony
state and Hermes transcripts. No database rollback is part of installation or
recovery. Compare files before restoring if you have edited them since setup.

The former built-in self-knowledge catalog is retired. Legacy standalone/MCP
setup no longer writes that catalog into the world model or requests graph
seeding. The guided Hermes installation follows its existing setup path.
`colony seed`, including its compatibility flags `--force` and `--verify`, reports
retirement without contacting a sidecar. `POST /v1/host/seed` retains its response
shape with zero counts, no errors, and `skipped=["builtin_self_knowledge_retired"]`.
Historical records remain intact. This retirement does not revise previously
stored claims or the separate self-question context corpus.
