# Toolsmith P5: real evidence and bounded publication authority

Status: implemented in the P5 candidate branch; not deployed by this change.

## Outcome

Toolsmith can still draft and evaluate small pure functions automatically,
but it cannot turn its own test into evidence or publish code because a trust
stage changed. A live tool now requires all of the following:

1. A mined/requested candidate digest bound to its exact provenance.
2. An artifact digest covering code, self-test, schema, provenance, and the
   immutable pure-function capability manifest.
3. Static policy acceptance plus an egress-free, credential-free sandbox
   self-test. This verifies the candidate but earns zero shadow evidence.
4. The configured number of successful comparisons between incumbent and
   candidate on the same bounded captured input. The candidate is run twice;
   it must be deterministic and match the incumbent output.
5. A scoped `toolsmith:graduate` principal bound to the owner lane and a
   one-shot authority envelope for the exact tool/candidate/artifact. The
   authority lifetime is at most 15 minutes.

`act_first` remains useful context for an operator, but is never authority to
publish a capability.

## Small trust boundary

This design intentionally does not add a general policy engine.

- Contained pure evaluations use the sandbox read-only fast path. Toolsmith
  sends `owner_directed=false`; it no longer impersonates owner direction.
- The general sandbox HTTP route derives owner direction from a scoped,
  owner-bound `sandbox:execute` principal (with a legacy-bearer migration
  carve-out). Body fields can no longer self-assert direction or approval.
- Sandbox directive errors fail closed with `boundary_check_error`.
- Inputs and outputs are canonical JSON capped at 32 KiB each.
- The audit database stores digests and receipt metadata, not captured input
  or output bodies.
- `capture_id` is unique per tool. Exact retries are idempotent; a changed
  replay is rejected.
- `authority_id` and `decision_id` are unique. Exact retries return the first
  receipt; either identifier rebound to different content is rejected.
- Publication and authority consumption occur in one SQLite transaction.
- Read-only live tools retain the low-latency sandbox fast path.

## Capability manifest and static policy

P5 Toolsmith artifacts have one fixed capability lane:

```json
{
  "version": "toolsmith.capability-manifest.v1",
  "entrypoint": "run",
  "effects": "none",
  "filesystem": "none",
  "network": "none",
  "subprocess": false,
  "environment": false,
  "deterministic": true
}
```

The lightweight AST check requires one top-level `run` function, restricts
imports to a small pure standard-library allowlist, and blocks dynamic code,
environment/file/process access, dunder traversal, async/generator syntax,
and known nondeterministic calls. Docker containment remains the enforcement
backstop; static inspection is defense in depth, not a claim that Python AST
inspection is a complete sandbox.

## Durable projections

`colony-toolsmith.db` gains additive columns and two tables:

- `toolsmith_shadow_comparisons`: capture/source/principal plus artifact,
  input, incumbent output, first candidate output, and repeat output digests.
- `toolsmith_graduations`: the consumed authority/decision IDs, exact digests,
  server-derived principal/owner, validity window, and authority digest.

`GET /v1/host/self/tools/{tool_id}` returns the exact graduation binding and
digest-only audit projection. The list route exposes receipt counts so Doctor
can flag live tools that predate P5.

Old `shadow_runs` values are retained for forensic compatibility but never
count toward eligibility. Existing live tools are not retroactively blessed;
Doctor reports them until they are retired, compared, and explicitly
re-graduated.

## Scoped API contract

- `POST /v1/host/self/tools/{id}/shadow-compare` requires a non-legacy scoped
  `toolsmith:evaluate` principal. Its credential attests which capture service
  submitted the incumbent pair.
- `POST /v1/host/self/tools/{id}/graduate` requires a non-legacy scoped
  `toolsmith:graduate` principal with the `owner` audience and exact owner
  person binding.

The graduation request carries only narrowing data: `authority_id`,
`decision_id`, expected candidate/artifact digests, `issued_at`, `expires_at`,
and `max_uses=1`. Principal and owner identity always come from authenticated
request state, never the body.

See `docs/runbooks/P5-TOOLSMITH-CANARY-ROLLBACK.md` for migration, canary, and
rollback.

## Intentionally deferred integration work

- The legacy action journal contains descriptions and refs, not trustworthy
  structured input/output pairs. P5 therefore does not pretend to mine real
  cases from it. A deployment capture adapter must submit pairs through the
  scoped comparison route with its source receipt as `capture_id`.
- Existing live tools need operator-led retirement and requalification; they
  are reported, not silently grandfathered or disabled by this code change.
- Static AST policy is intentionally conservative and small. Broader effectful
  tools need a future capability lane and action-plane authority rather than
  exceptions added to the pure lane.
- This candidate does not change an Operator Deck UI. The GET binding,
  graduate route, and retire route are the stable backend contract for that
  surface.

## Candidate verification

- Focused Toolsmith/sandbox/authority/Doctor matrix: 146 passed.
- Full Colony matrix with dotenv loading disabled: 2,604 passed, 118 skipped;
  the 21 warnings are the existing dependency/deprecation and async-resource
  warnings outside P5.
- Local policy overhead probe (10,000 iterations): about 46 microseconds for
  AST validation and 2 microseconds for canonical input validation per call,
  far below sandbox execution latency.
