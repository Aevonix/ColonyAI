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

The wizard asks for your name, the agent's name, the model API root and model,
then whether to start Colony. An API key is prompted without echo. For unattended
setup use `COLONY_MODEL_API_KEY` in the process environment, never a command-line
key. A model that requires no key works too.

```bash
colony init --non-interactive \
  --hermes-python /path/to/hermes/.venv/bin/python \
  --hermes-home "$HOME/.hermes-orion" \
  --agent-name Orion --contact-name Owner \
  --model-url http://127.0.0.1:8000/v1 --model my-local-model --start
```

`--hermes-home` wins over `HERMES_HOME`, then `~/.hermes`. Only that home is
inspected. `--dir` selects private Colony state, otherwise `COLONY_STATE_DIR` or
`<selected Hermes home>/colony` is used. State and credentials cannot be placed
inside a Git checkout. Select that same Hermes home when launching Hermes:

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

Graph/vector retrieval, embedding downloads and consequential background workers
are disabled in this profile. Install `./sidecar[graph,vectors]` only when adding
those services intentionally. Model quality still determines extraction and
reasoning quality. Lexical retrieval does not promise semantic recall of every
paraphrase. This setup is a growing local base, not a claim that every autonomous
behaviour or public channel is ready.

## Identity and authority

A real owner contact is created, with a generated exact-person API credential.
The server has no global legacy bearer key. The native local CLI is explicitly
bound to the owner; unrecognized real-channel senders do not inherit that
identity. The wizard does not enroll messaging handles, remote users or devices.
Add those through their authenticated transport integration and scoped grants.
Native owner tools remain available; consequential effects remain subject to
the existing application consent rules. Public guest context needs the existing
scoped projection contract and is not enabled by this local profile.

Canonical adapters are copied from the distribution into private state. Tiny
profile-local entry points load those exact modules. No code is duplicated or
patched. Other profiles and running Hermes sessions are not restarted or
modified. Start a new Hermes session after attachment.

For a new home, `SOUL.md` contains the chosen identity. An existing SOUL, channels,
model and unrelated settings are retained. An incumbent non-Colony memory
provider requires an explicit wizard choice or `--replace-memory-provider`;
its data is retained. An existing Colony directory adapter or native JSON config
requires an explicit upgrade rather than being silently replaced.

## Start, observe and recover

```bash
colony --instance "$HOME/.hermes-orion/colony" start --detach
colony --instance "$HOME/.hermes-orion/colony" status
colony --instance "$HOME/.hermes-orion/colony" stop
```

With the selected `HERMES_HOME`, start/status/stop also discover the instance
from `plugins.colony.instance_dir`. They never fall back to another instance's
`.env`. The private instance's `.env` governs startup; edit that file for lasting
changes. Its `sidecar.log` and process record belong to that instance. A busy port
is not permission to stop its occupant. Stop checks the recorded process's
creation time and command before signaling it. This daemon does not install
reboot persistence: use your existing per-instance launchd/systemd supervisor
with `colony --instance /private/path start` for unattended operation. The legacy
global `colony service` command refuses this profile.

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
when no original existed), and remove the two wizard-created forwarding
directories `plugins/colony` and `plugins/colony-memory`. Keep the private Colony
state and Hermes transcripts. No database rollback is part of installation or
recovery. Compare files before restoring if you have edited them since setup.
