# Leveled Cross-Contact Theory of Mind (ToM2)

Colony keeps second-order inferences — *who appears to know / not know
which shared fact* — in a refs-not-content store (`tom/tom2.py`). This
document describes the **leveled rendering system** that decides, per
conversation and per reader, **every turn**, how much of that model may
surface, and how to turn all of it off with one variable.

Everything below ships **default-inert**: with a stock configuration the
assembled context is byte-identical to a build without this system (test:
`tests/test_tom2_wiring.py::test_defaults_byte_identical_to_neutralized_block`).

## Level semantics

| Level | Audience | What renders |
|---|---|---|
| **0** | owner only | Today's behavior: the owner-facing asymmetry section (`COLONY_TOM2_CONTEXT`) and owner API surfaces. Nothing about the model reaches any other reader. |
| **1** | the reader, about themself | Self-reflexive prior: the reader's own `knows` rows (fact text they already own) plus ONE content-free caution line when `unaware_of` rows exist about them. No third party is ever mentioned. |
| **2** | the reader, about third parties | Epistemic topology ("X has not heard: …") through the full eligibility pipeline. By construction (the H3.5 double gate) every rendered fact text is a fact the reader **already owns** — level 2 can add topology, never new content. |

## The effective level (a min-chain of independent brakes)

```
effective = min( COLONY_TOM2_LEVEL          (default 0),
                 COLONY_TOM2_MAX_LEVEL      (default 1),
                 cap(environment risk)      (COLONY_TOM2_RISK_CAPS, default 0:2,1:2,2:1,3:0),
                 2 if live-enforce-evidence else 1,
                 2 if COLONY_TOM2_CROSS_CONTEXT else 1 )
       … and 0 on ANY error anywhere.
```

Environment risk (`gate/env_risk.py`) grades each (conversation, reader)
pair R0–R3, **monotone and fail-closed**: a lower grade needs positive,
verified evidence (declared-private gateway, strong identity resolution,
known census, tier floors); every missing signal — and every error — is
R3. `COLONY_ENV_RISK_GATEWAY_CLASS` must explicitly bless a gateway as
`private` before anything can grade below R3; Colony ships no gateway
names.

Enforce evidence is the resolver's proof that the outbound guard is
actually suppressing on this gateway *right now*: the `tom2_epistemic`
check on the enforce allowlist, the guard circuit breaker closed, and
recent enforce-mode audit rows (`GuardAuditStore.enforce_evidence`).
Silence proves nothing; no proof caps the system at level 1.

## Forced-downgrade list (no human in the loop)

Any one of these drops the level THAT TURN, silently:

- unknown / shadow / weakly-resolved participant sighted → level 0
- group room, or any second non-owner in the window → level ≤ 1
  (any unresolved member → 0)
- public / embodied / unclassified gateway → level 0
- the **subject** of an inference sighted in the conversation → that
  inference is excluded (never model someone into the room they are in)
- the subject is the owner → excluded (owner ignorance is never narrated)
- no enforce evidence, breaker tripped, or check de-allowlisted → ≤ 1
- machine/system turn, unresolved reader → 0
- resolver / classifier / store error of any kind → 0
- exposure budgets exhausted (`COLONY_TOM2_BUDGET_*`) → the row does not
  render
- owner pair-approval missing / expired / revoked → the row does not
  render

The resolution is cached ≤ 60s per (conversation, reader); a decayed
brake takes effect within a minute, everywhere at once.

## The egress net

Every level-2 rendering is **ledger-first**: an exposure row
(`tom/exposure.py`, refs only) and an injection **taint**
(`gate/taint.py`, TTL 900s) are durably recorded before a line may enter
context. While a taint is live, the `tom2_epistemic` guard check
(block-severity, on the default enforce allowlist) blocks replies that
voice an epistemic claim about a tainted subject, make self-referential
modeling claims, or carry tainted fact text into a different
conversation. With no live taint the check is inert — one in-memory clock
comparison, zero findings, zero false positives.

Honest limitation: the net is lexical; a paraphrase escapes it
(test-documented). The structural guarantee lives upstream — the renderer
cannot inject content the reader does not already hold.

## Graduation ladder

1. **Ship dark** (all defaults). Observe presence, `/env-risk`, and
   `/tom2/status` for a while; confirm your private DM surfaces grade
   R0/R1 and the doctor is clean.
2. `COLONY_TOM2_LEVEL=1` — self-reflexive priors only.
3. Ramp the chat guard to enforce on the target gateway and keep
   `tom2_epistemic` allowlisted, so enforce evidence accrues
   (`/response-guard/audit`).
4. `COLONY_TOM2_MAX_LEVEL=2` + `COLONY_TOM2_CROSS_CONTEXT=1` +
   `COLONY_TOM2_LEVEL=2`, with `COLONY_TOM2_L2_APPROVAL=required`
   (default) and per-pair approvals via `POST /v1/host/tom2/approvals`.

The system will not run level 2 without step 3: no enforce evidence, no
level 2 — by construction, not by policy.

## Kill switch / panic

**`COLONY_TOM2_LEVEL=0`** is the single-variable kill: the context wiring
is skipped entirely (level 1 AND 2, every conversation) on the next turn.
Nothing else needs to change; already-registered taints keep protecting
egress until they expire. Verify with `GET /v1/host/tom2/status`
(`configured: 0`) and `colony doctor` (`tom2-level-coherence` reports the
kill switch).

For a full stand-down beyond rendering: revoke pairs
(`POST /v1/host/tom2/approvals` with `action=revoke`) and set
`COLONY_TOM2_CONTEXT=0` to drop the owner section too. Leave the guard
and its allowlist alone — the egress net is protection, not exposure.

## Reversibility

- **What was disclosed:** `GET /v1/host/tom2/exposure` — every level-2
  rendering by reader/subject/fact-ref/conversation (refs only, never
  fact text), plus live budget posture.
- **Un-model a fact:** `DELETE /v1/host/mind/facts/{id}` cascades via
  `Tom2Store.delete_for_fact` — the fact's inferences are dropped, and a
  dangling ref could never render anyway (H3.5 fails closed).
- **Un-approve a pair:** `POST /v1/host/tom2/approvals` `action=revoke`
  (approvals also expire on their own, `COLONY_TOM2_APPROVAL_TTL_DAYS`,
  default 30).

## Observability

- `GET /v1/host/tom2/status` — mode, counts, `{configured, max,
  risk_caps, sample_decision}` (every brake term of a live resolution).
- `GET /v1/host/env-risk?conversation_key=…&contact_id=…` — grade + census.
- `GET /v1/host/tom2/exposure`, `GET /v1/host/tom2/approvals`.
- `GET /v1/host/response-guard/audit` — per-check rates, breaker,
  enforce evidence.
- `colony doctor` — `tom2-cross-context`, `tom2-risk-caps`,
  `tom2-level-coherence`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `COLONY_TOM2_LEVEL` | `0` | Requested level; `0` is the kill switch. |
| `COLONY_TOM2_MAX_LEVEL` | `1` | Hard ceiling. |
| `COLONY_TOM2_RISK_CAPS` | `0:2,1:2,2:1,3:0` | Per-risk level caps; malformed fails closed to all-0. |
| `COLONY_TOM2_CROSS_CONTEXT` | `0` | H3.5 render gate (half of the level-2 requirement). |
| `COLONY_TOM2_L2_APPROVAL` | `required` | Owner pair-approval requirement. |
| `COLONY_TOM2_BUDGET_PAIR_DAY` / `_READER_DAY` / `_GLOBAL_DAY` | `1` / `3` / `10` | Exposure budgets per rolling 24h. |
| `COLONY_TOM2_TAINT_TTL_SECS` | `900` | How long an injection stays hot for the egress net. |
| `COLONY_TOM2_MUTUAL_WINDOW_DAYS` | `30` | Mutual-knowledge co-sighting window. |
| `COLONY_ENV_RISK_GATEWAY_CLASS` | *(empty)* | `gateway:private\|public\|embodied` pairs; unclassified = hostile. |
| `COLONY_ENV_RISK_WINDOW_HOURS` | `48` | Census / subject-presence window. |
| `COLONY_GUARD_ENFORCE_CHECKS` | `secret_leak,tom2_epistemic` | Per-check enforce allowlist. |
| `COLONY_GUARD_DERIVE_CONTEXT` | `1` | Server-side guard-context completion (chat hot path). |
| `COLONY_CONV_PRESENCE` | `on` | Passive conversation census recording. |

## Residual risk (accepted, not hidden)

Level 2 discloses *that* the system models people, to trusted readers, at
a budgeted rate. Irreducibles: implication leaks (behavior divergence is
observable), aggregation over time (slowed by budgets, not stopped),
elicitation (a voiced prior is one paraphrase away — the egress net
narrows this lexically). Structurally impossible through the renderer:
new fact content crossing contexts, owner-ignorance narration,
co-present-subject surfacing, rendering to unknown / group / public
audiences.
