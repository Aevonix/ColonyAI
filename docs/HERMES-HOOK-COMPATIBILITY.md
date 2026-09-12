# Hermes callback compatibility

PacoMind's optional [concurrent background tasks](NATIVE-TASK-CHANNELS.md) require
healthy overlapping plugin callbacks to retain each invocation's context and
result. The pinned public CI target is the explicit Hermes build below. The
installer does not patch an existing Hermes checkout or change its selection.

## Published qualification target

**SHIPPED source:** [Kurcide/hermes-agent at
`37fc68b45c2351957a7fa262978b0f1174b1bd44`](https://github.com/Kurcide/hermes-agent/commit/37fc68b45c2351957a7fa262978b0f1174b1bd44),
based on [Hermes v0.21.2,
`939e45c91d751fadd94dcd1b873ac3cb44846213`](https://github.com/NousResearch/hermes-agent/commit/939e45c91d751fadd94dcd1b873ac3cb44846213),
under the [MIT license](https://github.com/Kurcide/hermes-agent/blob/37fc68b45c2351957a7fa262978b0f1174b1bd44/LICENSE).
This is a published compatibility fork, not a claim that the change shipped in
an upstream Hermes release.

The callback change adapts [upstream PR #104763](https://github.com/NousResearch/hermes-agent/pull/104763),
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
running. For concurrent PacoMind turns, that can omit source binding or lifecycle
observations. The correction operates within Hermes' existing callback
dispatcher. It adds no PacoMind service, model route, deployment setting or new
plugin registration API.

The selected build also retains named custom-provider timeout settings after
Hermes resolves a named endpoint to its generic transport. It uses the existing
`requested_provider` identity and timeout schema. Per-model settings take
precedence over the named provider, then the generic provider fallback. Cached
agents read changed timeout settings on subsequent requests. No initial output
cap is restored; native truncation recovery keeps its existing increasing
budgets. These are per-attempt transport settings, not an overall task deadline.

Hermes 0.21.2 deliberately omits ordinary persistence hooks for detached review
forks. The compatibility build adds one `on_detached_turn_end` observer in
`agent/turn_finalizer.py`, registered in `hermes_cli/plugins.py`. Its payload is
limited to exact execution/parent identifiers, completion/failure/interruption
flags, exit reason, model and platform. It carries no conversation or output
content, ignores callback returns, and preserves the native persistence skips.
The adapter binds review starts through existing synchronous request middleware.
This small interface replaces reliance on user-turn hooks for detached work;
it does not add a scheduler, review service or alternative memory store.
Unmodified upstream 0.21.2 does not provide this completion observer.

The build also limits the Kanban completion stop gate to the existing
dispatcher-owned worker context. A delegated child inherits the task
environment but owns no Kanban card and cannot call the parent completion
tools. It can now return its findings to the parent. The actual claimed
worker still must complete or block its own card before ending. The change
reuses Hermes' existing delegation ContextVar; it adds no setting or hook.

## What is qualified

The two existing compatibility changes were reapplied to the 0.21.2 release
without conflicts. Their affected native suites passed 275 checks in a native-only
environment. The detached observer then passed 24 focused checks, including
successful, failed and interrupted endings, retained persistence-hook skips and
unchanged ordinary turn endings. These are separate run scopes, not a claim that
the entire upstream test suite ran.

The delegated-stop correction passed 52 focused checks using controlled
responses and the actual native Kanban store and completion tools. The
unmodified qualified base fails both delegated-return cases; the correction
passes them while retaining actual worker completion and blocking. These
checks establish the runtime stop boundary, not useful model completion.

The following callback and timeout counts describe their earlier qualification;
the retained behavior is rechecked on the current native candidate above.

The two synchronized core invariants fail on the unmodified base and pass on
the selected build. They cover distinct sessions retaining their own callback
results and parallel tools in one session receiving separate policy decisions.
The existing plugin, ownership-ledger and event-bus checks also passed, for 132
affected core tests with file retries disabled.

The timeout correction passed 139 focused native tests, including the pinned
optional Anthropic SDK. Controlled SDK requests also verify distinct foreground
and background provider policies, cached-agent refresh, separate-process task
resume and native truncation recovery. These controlled responses establish
configuration propagation, not model quality or measured timeout expiration.

PacoMind's [actual native task fixture](../tests/hermes_adapter/test_native_task_channels.py)
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
3. Pass PacoMind's actual concurrent task fixture, including separate roots,
   ordinary conversation, source receipts, steering and targeted interruption.
4. Update the pinned CI commit and documentation, then select the runtime
   through the deployment's normal reversible upgrade path.

Also retain or verify the named-provider timeout behavior before removing that
part of the compatibility build. Replace the detached observer when upstream
provides an equivalent exact execution-end contract. Provider response hooks and
final output transforms alone do not establish failed or interrupted completion.

Also retain the delegated-return and actual-worker completion regressions.
Remove the stop-gate adjustment when an upstream release satisfies both
using its own dispatcher/delegation ownership boundary.

Keep the regression tests and upstream attribution after removing the fork
selection. Do not carry an old dispatcher diff over a newer implementation
without checking whether the upstream behavior already satisfies the contract.
