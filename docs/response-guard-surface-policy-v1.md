# ResponseGuard Surface Policy V1

Status: source contract implemented; not deployed by this change.

`ResponseGuardSurfacePolicyV1` makes guard applicability an exact property of
the outbound content surface. It never infers authority from a transport or
gateway label. This keeps the operating model small: one global guard mode and
one versioned static surface map.

## Surface contract

| Family | Exact surfaces | Applicability |
|---|---|---|
| Text | `api_text`, `cold_text`, `cron_text`, `meeting_text`, `proactive_text`, `text_chat`, `text_message` | Guarded |
| Artifact | `artifact` | Guarded |
| Real-time speech | `meeting_speech`, `realtime_voice` | Excluded |

Unknown, misspelled, normalized, or missing surfaces are rejected at the host
API boundary. A gateway named `voice` cannot bypass a `text_chat` check. A
`realtime_voice` surface bypasses even when its gateway is named `sms`.

The host's custom voice core is unchanged. It does not call ResponseGuard, and
this policy adds no work to call-connect, speech turns, barge-in, first audio,
phone, intercom, or meeting-speech paths. A completed voice turn may be
observed asynchronously elsewhere, but it is not synchronously gated here.

## Mode and outage behavior

The server has one configured mode: `shadow` or `enforce`. A request may
strengthen `shadow` to `enforce` for a guarded call; it cannot weaken configured
`enforce` to `shadow`.

| Surface | Effective mode | Configured check unavailable | Delivery result |
|---|---|---|---|
| Text/artifact | `shadow` | Audit as degraded warning | Allow |
| Text/artifact | `enforce` | Audit as degraded block | Block |
| Speech | `excluded` | No check is run | Allow/bypass |

The default Hermes plugin mode remains `off`; its existing opt-in shadow mode
remains asynchronous and fail-open. On a host that explicitly advertises
post-hook mutation and explicitly enables enforce, an invalid or unavailable
guard verdict withholds that text reply. Such a host checks the entire reply,
requires the returned candidate digest to match the exact UTF-8 text, and
withholds replies over the adapter's 8,000-character synchronous-check limit
instead of checking only a prefix. Current Hermes deployments that cannot
mutate post-hook replies are still honestly downgraded to shadow. This is
Colony plugin behavior and does not patch Hermes core.

The in-process proactive delivery path has the same mode-specific outage
contract: an unavailable or malformed guard allows in shadow and blocks in
enforce. Its owner exemption is derived from Colony's identity resolver, not
from message content or a caller-supplied boolean.

## Integration points

- The host request requires `surface` and returns the policy id, policy digest,
  candidate digest, surface family, applicability, and guard status with every
  verdict.
- The legacy host-request `authorized` field is non-authoritative. The public
  endpoint always evaluates it as false; only trusted in-process code may pass
  an owner exemption after deriving identity from server-owned state.
- The Colony Hermes plugin declares `text_chat`.
- Colony proactive outreach declares `proactive_text`, including its single
  rejection/regeneration retry.
- Audit rows persist the exact surface, policy id, policy digest, candidate
  digest, and guard status. These rows prove only that a candidate was
  evaluated. They do **not** prove a transport withheld the candidate or
  emitted a checked revision, regardless of row count or verdict.
- Colony deliberately leaves the Tom2 applied-enforcement evidence probe unset,
  so evaluation logs cannot unlock level 2. A future mediator must persist a
  digest-bound receipt for the exact applied output before that cap can lift.
- `COLONY_GUARD_EXCLUDED_GATEWAYS` is no longer consumed. The constructor
  accepts the old argument only as a logged, ignored compatibility input.

New adapters must choose one existing exact surface or deliberately revise the
versioned policy and its regression-locked digest. They must not construct a
surface from an untrusted inbound field.

## Rollout and rollback

This source change performs no service restart, rebind, or live configuration
mutation. Before a later canary, keep `COLONY_GUARD_MODE=shadow` and the Hermes
chat mode `off` or `shadow`; verify audit classification before enabling text
enforcement. Do not enable Tom2 level 2 from verdict counts; first ship and
verify the applied-output receipt mediator described above.

Operational rollback is configuration-first: set the Hermes chat mode to
`off` and Colony guard mode to `shadow`, then roll back the pinned Colony
artifact if needed. No voice rollback is involved because the host voice core
and its routing are outside this policy and remain untouched.
