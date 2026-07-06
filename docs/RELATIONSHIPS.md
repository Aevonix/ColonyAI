# Relationship Intelligence: one person, every channel

Status: DESIGN v1.0 (2026-07-06) — implementation in this release.

Colony's promise is a persistent social memory for an agent: who it talks
to, what those people are like, what standing each relationship has, and how
best to approach them. This document is the durable spec for the identity
and relationship layer that delivers that promise, written against a live
audit of a reference deployment where the machinery existed but attribution
failed (95%+ of non-owner traffic landed on a `default` pseudo-contact and
third parties accumulated zero history).

## Design principles

1. **Attribution before analysis.** Every downstream capability (affect,
   facts, psyche profiles, scoring, cadence, outreach) is only as good as
   knowing WHO each utterance came from. Per-message sender resolution is
   the foundation; everything else already exists and starves without it.
2. **A person is one contact with many handles.** The unit of identity is
   the contact; channels contribute handles (`whatsapp`, `sms`, `rcs`,
   `email`, `voice`, `face`, ...). Matching is deterministic where safe
   (exact handle, cross-gateway phone-key, normalized email), and a
   PROPOSAL where fuzzy (same display name in a shared group scope). Silent
   fuzzy merges are forbidden: a wrong merge poisons two histories at once.
3. **Unknown people become shadow contacts, never nothing.** A sender that
   cannot be matched gets a shadow contact (tier `unknown`,
   `interaction_allowed=false`, provenance recorded) so history accrues
   from first contact. Promotion to a real relationship is the existing
   trust-tier / scope machinery's job.
4. **Machines are not people.** Turns of system origin (cron prompts,
   skill invocations, self-echo) must never write affect, facts,
   engagement observations, or interactions. They attribute to the
   reserved `system` sentinel and are excluded from every relationship
   surface. (Same lesson as the directive self-poisoning incident.)
5. **Stores accept only real contacts.** The ToM APIs validate contact ids
   against the contact store; test strings and free-text names are refused
   rather than silently minting psyche state.

## Architecture

### 1. Sender flows with every turn

`TurnSyncRequest` gains an optional `sender`:

```json
"sender": {
  "platform": "whatsapp",           // channel kind
  "user_id": "1234567@lid",         // per-platform sender identifier
  "display_name": "Sam Rivera",   // best-effort, for shadow naming + fuzzy proposals
  "group_id": "5551212-16999@g.us"  // set when the turn came from a group
}
```

Hosts that cannot supply it lose nothing (legacy behavior); hosts that can
(the Hermes provider passes what `pre_llm_call` already carries) get
authoritative server-side attribution regardless of client caching bugs.

### 2. ParticipantResolver (sidecar, `identity/participants.py`)

`resolve(sender, *, allow_shadow=True) -> Resolution(contact_id, method, created)`

Resolution ladder, first hit wins:
1. **Exact handle**: `contact_handles(gateway=platform, address=user_id)`
   (phones via the existing `phone_key` cross-format matcher).
2. **Cross-gateway phone**: `user_id` parses as a phone → `phone_key`
   match against ANY gateway's handles (the cross-gateway case: an `sms` handle
   matching an `rcs` sender).
3. **Normalized email**: lowercase match for email-shaped ids.
4. **Scoped display-name (PROPOSAL only)**: `display_name` uniquely
   matches one member of the same group scope → return that contact AND
   file a merge proposal linking the new handle, flagged for owner review.
   Confidence below auto-merge never links silently.
5. **Shadow contact**: create (tier `unknown`, `interaction_allowed=false`,
   `met_via=<channel_id>`, `import_source=auto:sender`) with the handle
   attached, when `COLONY_IDENTITY_SHADOW_CONTACTS` (default true).

The resolver also OWNS the machine gate: senderless turns on machine
channels (`cron:`, `api:` prefixes, configurable) or system-origin text
resolve to the `system` sentinel.

### 3. turns/sync becomes the attribution chokepoint

On every synced turn:
- If `sender` present → resolver decides the contact (overriding the
  client-supplied `context.contact_id`, which remains the fallback).
- The resolved contact gets `record_interaction(contact_id, channel_id)`
  (non-owner and owner alike; `system` never).
- The comms ledger row carries the REAL `channel_id` (group vs DM vs voice
  provenance) instead of a collapsed `direct`.
- Affect/facts/engagement extraction runs against the resolved contact;
  `system` turns skip ToM entirely.

### 4. ToM boundary validation

`POST /affect/events`, `POST /mind/facts`, engagement observation writes:
contact must exist in the store (or be the owner). Unknown ids are
rejected with a clear error. (`system` is storable in comms for ops
visibility but refused by ToM.)

### 5. Voice and in-person

Voice is just another channel: the deployment's speaker-identity service
resolves a voice to a canonical contact and the gateway syncs the turn with
`sender={platform:"voice", user_id:<contact_id or enrolled-name>}`. A
`voice` handle kind links enrolled voiceprint names to contacts so kiosk
and call interactions accrue to the same person as their texts. Same for
`face` if a deployment enrolls faces. Colony stays generic: it defines the
handle kinds; deployments supply the recognizers.

### 6. Owner curation tools

- `link_contact(who, gateway, address)` — attach a handle ("that WhatsApp
  is Sam's"). Tool + `POST /contacts/{id}/handles`. SHIPPED.
- `merge_contacts(keep, merge)` — fold one contact into another (reassign
  handles, sum interaction history, soft-delete the loser; audited +
  reversible). Tool + `POST /contacts/merge` + store `merge_contacts`.
  SHIPPED.
- `pending_contact_proposals` — the rung-4 handle proposals awaiting owner
  review. Tool + `GET /contacts/proposals` + store `list_handle_proposals`.
  SHIPPED.

### 7. RelationshipProfiler (`relationships/profiler.py`)

Per contact with enough signal, a compact **RelationshipBrief**:
- **Standing**: trust tier, interaction count/recency/frequency, channels
  used (from comms provenance), cadence state, shared scopes.
- **Psyche**: the engagement extractor's OCEAN dims + qualitative profile
  (motivators, style) — the existing extractor, now fed real per-person
  observations.
- **Affect**: current valence/arousal + trend.
- **Rapport**: top shared-fact topics.
- **Approach guidance** (`COLONY_APPROACH_GUIDANCE`, default true): derived
  suggestions — preferred channel (most-used), best time (interaction-hour
  histogram in the contact's timezone), style notes from the psyche dims
  ("direct and brief", "responds to structured detail"), plus standing
  cautions (recent negative affect trend, overdue cadence).

Refresh: `_phase_relationship_profiling` (autonomy loop) re-profiles
contacts with ≥ `COLONY_RELATIONSHIP_PROFILE_MIN_INTERACTIONS` new
interactions (default 5) since last profile; briefs cached in
`colony-relationships.db`.

Consumers:
- Context assembly injects the brief when the conversation's contact is a
  profiled person (approach section included for non-owner contacts).
- `colony_relationship_brief(name)` tool + `GET /relationships/{contact_id}`.
- `colony_outreach_check` enriched with the approach section.
- The relationship initiative generators finally receive real signals.

### 8. Remediation of poisoned history (deployment runbook)

- `default`: STOP new person-writes (machine gate); exclude it from every
  relationship surface; keep rows for ops history. No deletion (part of it
  is genuine pre-fix owner traffic).
- Test residue (`validate-*` and other non-cid ids): purge from affect/
  facts/engagement stores; the ToM boundary validation prevents recurrence.

### 9. Diagnostics

Doctor gains `server-relationships`:
- WARN when >20% of last-7-day comms attribute to `default`/`system`-like
  ids (attribution regression signal).
- WARN when ToM stores contain ids absent from the contact store.
- INFO summary: contacts with history, profiled contacts, pending merge
  proposals.

## Config

| Env | Default | Meaning |
|---|---|---|
| `COLONY_IDENTITY_SHADOW_CONTACTS` | `true` | Unknown senders become shadow contacts |
| `COLONY_IDENTITY_MACHINE_CHANNELS` | `cron,api,internal` | Channel prefixes whose senderless turns are `system` |
| `COLONY_RELATIONSHIP_PROFILE_MIN_INTERACTIONS` | `5` | New interactions before a (re)profile |
| `COLONY_RELATIONSHIP_PROFILE_REFRESH_SECS` | `21600` | Profiling phase cadence |
| `COLONY_APPROACH_GUIDANCE` | `true` | Include approach guidance in briefs |

## Test plan

- Resolver: every ladder rung; failed match → shadow (and not when
  disabled); machine gate (channel prefix + system-origin text); phone
  cross-gateway; email normalization; display-name rung files a proposal
  and never silently links.
- turns/sync: sender overrides stale client contact; record_interaction
  fires for non-owner; comms rows carry channel; `system` skips ToM.
- ToM validation: unknown ids rejected; owner and real cids accepted.
- Profiler: brief fields from canned stores; approach derivations
  (channel preference, hour histogram, style notes); cache refresh gate.
- Doctor: attribution-regression WARN paths.
- Live E2E (reference deployment): synthetic group turn from a known
  third party attributes + records; unknown sender creates a shadow;
  voice turn accrues to the same contact as their texts.
