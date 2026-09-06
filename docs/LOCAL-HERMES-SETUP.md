# Create or attach a private Hermes agent

The supported lightweight path uses Hermes **0.21.0** (qualified stable commit
`29112bef099274229cadff79cdff7bf7b99c4b77`), Python 3.11 to 3.13, and one local
OpenAI-compatible chat endpoint. Install Hermes separately with its native
requirements. Colony does not patch or download Hermes, models, containers or
machine services.

From this public checkout, install both distributable packages in your Colony
Python environment. They may share an environment with Hermes, but need not:

```bash
python -m pip install . ./sidecar
colony init --hermes-python /path/to/hermes/.venv/bin/python
```

A separate Hermes environment needs its own native core dependencies. It does
not need Colony's CLI dependency `typer` or a preinstalled Colony adapter; setup
can attach the private adapter directly from the Colony environment.

The wizard offers optional task skills, asks for your name, the agent's name,
the model API root and model, whether to enable accepted local drafts, then
whether to start Colony. An API key is prompted without echo. For unattended
setup use `COLONY_MODEL_API_KEY` in the process environment, never a command-line
key. A model that requires no key works too.

```bash
colony init --non-interactive \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --hermes-home "$HOME/.hermes-orion" \
  --agent-name Orion --contact-name Owner \
  --model-url http://127.0.0.1:8000/v1 --model my-local-model --local-work --start
```

`--hermes-home` wins over `HERMES_HOME`, then `~/.hermes`. Only that home is
inspected. `--dir` selects private Colony state, otherwise `COLONY_STATE_DIR` or
`<selected Hermes home>/colony` is used. Both the selected Hermes home and Colony
state must stay outside Git checkouts, including when `--dir` is separate.
Select that same Hermes home when launching Hermes:

```bash
export HERMES_HOME="$HOME/.hermes-orion"
hermes
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

## What the local profile enables

Ordinary native turns enter the durable outbox and canonical source ledger.
Source quotations, temporal claim projections, contacts, commitments, execution
observations and self state use the existing SQLite stores. Text recall uses
lexical retrieval; it needs no embedding model. The native memory provider
injects selected evidence before inference. Compression uses Hermes' native
checkpoint contract with durable local capture. General-plugin turn capture is
the single ordinary writer.

The selected model supplies the legacy SMALL, MEDIUM and LARGE role bindings.
Optional named role/capability configuration can refine that later. Fresh homes
also get that model in native Hermes config. An existing Hermes model is kept.

Accepted local drafts are optional. With `--local-work`, setup checks function
calling and creates a named `planning` role bound to the selected local model.
It registers one job with the selected Hermes scheduler, running every five
minutes. The selected Hermes gateway must be running for scheduled fires.
The job remains idle until the owner accepts a specific question and local text
sources through `colony_accept_local_draft`. No existing commitment is required;
an optional commitment ID associates the draft with a broader obligation.

The worker reads those sources, produces a cited draft and retains its execution
and report in the instance. It cannot send the draft or change its source files.
It loads the planning role afresh on each fire, using the Colony interpreter to
resolve routing and the selected Hermes interpreter for native execution. The
two environments need no shared dependencies. See
[accepted local work](ACCEPTED-LOCAL-WORK.md) for limits and cancellation.
If scheduler registration fails after attachment, rerun the same command with
`--local-work`. It retries the missing registration using the retained planning
role, identity and credentials. Restart an already-running Colony instance to
load the new job binding. An older instance without a planning role or compatible
adapter needs explicit configuration or an adapter upgrade first.

Graph/vector retrieval, embedding downloads and consequential background workers
are disabled in this profile. Install `./sidecar[graph,vectors]` only when adding
those services intentionally. Model quality still determines extraction and
reasoning quality. Lexical retrieval does not promise semantic recall of every
paraphrase. This setup is a growing local base, not a claim that every autonomous
behaviour or public channel is ready.

## Optional task guidance

**Unreleased, current source only.** Published 1.0.3 artifacts predate this option
and skill pack. Use matching packages built from this source until a new release
includes them.

The adapter distribution includes two original instruction packs:
`colony-evidence-recall` for resolving incomplete or contradictory recollection,
and `colony-work-handoff` for continuing an authorized task across sessions.
They are experimental task guidance. Packaging and native discovery do not
establish a measured improvement in model behavior.

The wizard offers them with a default of no. Add `--task-skills` to `colony init`
to opt in. An existing attached instance can add them without repeating model
setup or changing its identity, credentials or runtime:

```bash
colony init --non-interactive --hermes-home /path/to/private/hermes --task-skills
```

Use `--adapter-wheel /path/to/colony_hermes.whl` to select a built adapter artifact
instead of the installed distribution. Setup copies its public instructions to
`<selected Hermes home>/skills/<skill name>/SKILL.md`. Hermes discovers their
descriptions in its ordinary skill index and loads each body on demand; there
is no additional loader, service or memory writer. Begin a new session to see
the installed skills, and use Hermes's native controls to disable them.

Re-running the option leaves identical files untouched and reports any existing
different skill directory as preserved. It never silently updates user-edited
skills. Compare the packaged source with your existing copy before replacing
it manually. Private identity, device details and deployment procedures belong
in your private agent's files, not in these public instructions.

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
