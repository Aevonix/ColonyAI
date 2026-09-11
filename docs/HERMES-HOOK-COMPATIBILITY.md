# Hermes callback compatibility

Colony's optional [concurrent background tasks](NATIVE-TASK-CHANNELS.md) require
healthy overlapping plugin callbacks to retain each invocation's context and
result. The pinned public CI target is the explicit Hermes build below. The
installer does not patch an existing Hermes checkout or change its selection.

## Published qualification target

**SHIPPED source:** [Kurcide/hermes-agent at
`a78454b963030962ccefd7e37461e35d2082b587`](https://github.com/Kurcide/hermes-agent/commit/a78454b963030962ccefd7e37461e35d2082b587),
based on [Hermes v0.21.1,
`2237be355906fbe6065ce1815711eee52b2d646e`](https://github.com/NousResearch/hermes-agent/commit/2237be355906fbe6065ce1815711eee52b2d646e),
under the [MIT license](https://github.com/Kurcide/hermes-agent/blob/a78454b963030962ccefd7e37461e35d2082b587/LICENSE).
This is a published compatibility fork, not a claim that the change shipped in
an upstream Hermes release.

The change adapts [upstream PR #104763](https://github.com/NousResearch/hermes-agent/pull/104763),
specifically [source commit
`b9c112c83b60b91e918341cbb587a6a913d9d9eb`](https://github.com/NousResearch/hermes-agent/commit/b9c112c83b60b91e918341cbb587a6a913d9d9eb).
The three production files match that proposal, and the original contributor's
authorship is preserved. The fork retains two synchronized regression tests.

| Runtime file | Purpose |
| --- | --- |
| `hermes_cli/plugins.py` | Track the running callback and dispatch generation. |
| `hermes_cli/plugins_dispatch.py` | Serialize healthy overlap while retaining each caller's copied context; retain timeout suppression for a stuck callback. |
| `hermes_cli/plugins_ledger.py` | Invalidate waiting dispatches during unload without treating a still-running worker as finished. |

The unmodified base can skip a callback because another invocation is still
running. For concurrent Colony turns, that can omit source binding or lifecycle
observations. The correction operates within Hermes' existing callback
dispatcher. It adds no Colony service, model route, deployment setting or new
plugin registration API.

## What is qualified

The two synchronized core invariants fail on the unmodified base and pass on
the selected build. They cover distinct sessions retaining their own callback
results and parallel tools in one session receiving separate policy decisions.
The existing plugin, ownership-ledger and event-bus checks also passed, for 132
affected core tests with file retries disabled.

Colony's [actual native task fixture](../tests/hermes_adapter/test_native_task_channels.py)
uses the installed adapter, real gateway, canonical source/contact APIs and
controlled SDK responses. It holds two task roots while ordinary conversation
continues, steers and stops one from another owner channel, and verifies that
the other task completes with its source parents. Late stopped output is not
retained. The [source checks](../tests/hermes_adapter/test_task_sources.py)
also distinguish inherited child authority from a fresh owner instruction.

[Pinned CI](../.github/workflows/ci.yml) selects the exact fork commit and sets
the native runtime path explicitly. It runs the adapter suite once, then checks
the JUnit report to require the actual concurrent fixture to have run and
passed. Missing collection and a skipped fixture both fail qualification.

The separate [daily upstream check](../.github/workflows/hermes-upstream.yml)
continues to select the latest published stable **NousResearch** release and
run the adapter suite against it. It is a compatibility signal: an incompatible
release produces a failing run, without changing the pinned runtime or hiding
the failure behind the fork. A passing run is evidence for review, not an
automatic production upgrade.

These checks establish controlled runtime integration. They do not establish
model instruction following, ordinary-use memory quality, physical channel
delivery, deployment health or child-process cleanup.

## Remaining limits and removal

Healthy callbacks can accumulate waiting time; the change promises neither
FIFO ordering nor a bounded backlog. A callback recursively invoking itself
can still wait until timeout. Genuine hung callbacks remain suppressed, and
abandoned threads are not terminated. Plugin process globals do not become
session-scoped automatically.

Replace the fork with an unmodified upstream release when all of these hold:

1. Identify the released commit and review its callback admission, timeout and
   unload behavior. A PR or temporary merge commit alone is not a release.
2. Run the two core invariants and affected existing suites against that exact
   unmodified release, retaining the timeout and context checks.
3. Pass Colony's actual concurrent task fixture, including separate roots,
   ordinary conversation, source receipts, steering and targeted interruption.
4. Update the pinned CI commit and documentation, then select the runtime
   through the deployment's normal reversible upgrade path.

Keep the regression tests and upstream attribution after removing the fork
selection. Do not carry an old dispatcher diff over a newer implementation
without checking whether the upstream behavior already satisfies the contract.
