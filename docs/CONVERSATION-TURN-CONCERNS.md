# Conversation turn concerns

This optional bridge reduces completed, server-attributed `conversation.turn`
journal records through its own `workspace-turn-concerns-v1` cursor into the
existing Concern -> read-only ThoughtJob -> policy-gated GoalProposal path. It
does not grant a capability, approval, receipt, WorkOrder, or effect authority.

Configuration is deliberately fail-closed:

- `COLONY_TURN_CONCERNS=off|shadow|live` defaults to `off`; invalid values are
  `off`.
- `COLONY_TURN_CONCERNS_CHANNELS=voice,intercom,...` is an explicit list of
  channel lanes (the part before `:`). It is required in `shadow` and `live`.
  Empty or malformed configuration stops before cursor initialization, so a
  corrected configuration can replay the retained event.
- `COLONY_TURN_CONCERNS_EXCLUDED_SESSION_PREFIXES=...` is a
  default-empty denylist checked before channel admission. Empty session IDs
  and malformed prefix lists fail closed.
- `COLONY_TURN_CONCERNS_EXCLUDED_PLATFORMS=...` is a default-empty exact
  source-platform denylist. It is independent of the channel lane, so a
  multiplexed transport cannot become eligible by naming an allowed channel.
- `COLONY_TURN_CONCERNS_BOOTSTRAP=tail|replay` defaults to `tail`.
- `COLONY_TURN_CONCERNS_GAP_POLICY=stop|acknowledge` defaults to `stop`.
- `COLONY_OWNER_PERSON_ID` (or the compatibility
  `COLONY_OWNER_CONTACT_ID`) must be one canonical owner identity. Missing or
  malformed owner configuration stops before cursor initialization or
  advancement; correction replays the retained event.

Internal/API/cron/system/worker lanes are structurally excluded. Colony does
not hard-code deployment-specific transport names. A deployment prevents a
duplicate producer by listing that transport in `EXCLUDED_PLATFORMS`, or by
listing a stable overlapping session prefix when two surfaces share one
transport.

Only a scoped authenticated turn principal can produce an eligible envelope:
the resolved subject must be within its exact person grant, or come from a
server-resolved structured sender on an attested platform. Colony derives the
owner-private or subject-private scope server-side. Legacy/global bearer,
anonymous, client-only contact claims, forged context metadata, and the system
sentinel remain unattested.

Each scoped turn-ingress principal also declares exact lowercase
`turn_ingress_platforms` in the keyring. A structured sender is sealed only
when its normalized platform belongs to that role. A senderless principal can
seal a platform only when its role contains exactly one platform. Channel,
session, host, and body metadata never attest the platform. Dynamic resolved
contacts become eligible only after their bounded contact grant was actually
persisted; static person grants remain independent and retry attribution stays
stable.

An attested turn without a caller-supplied idempotency key receives a stable
`server-turn-<sha256>` lineage ID derived from the server-final principal,
subject, nonempty session, channel, and normalized completed-turn content.
That digest deduplicates an exact retry; content contributes no identity,
privacy, capability, approval, or effect authority.

Start with a short, bounded `shadow` canary plus `tail` and the smallest
required channel list. Verify cursor health, scoped visibility, duplicate
receipts, and retained-row growth before selecting `live`: shadow evidence is
capacity-isolated, but its durable audit rows can still grow until explicitly
managed by the deployment's retention policy.

Before changing from `shadow` to `live`, explicitly owner-promote or resolve
every retained shadow turn concern. A shadow-origin concern deliberately keeps
its shadow provenance after the flag changes and remains ineligible for live
autonomous admission until that exact owner decision. Starting live with an
unreviewed, high-salience shadow backlog can repeatedly select the held item
ahead of newer live concerns, so a zero-unreviewed-shadow-backlog check is an
activation prerequisite.

Lowering the mode to `shadow` or `off` immediately holds existing turn
concerns and their derived Projects as resumable. A canonical WorkOrder that
is still `QUEUED`, or is `CLAIMED` but has not crossed `RUNNING`, moves to a
durable source-runtime hold with its identity and approval/evidence tags
unchanged. Returning to `live` requeues and resumes that exact row. Work that
already crossed `RUNNING` is not killed or silently rewound; its normal
receipt and reconciliation path remains authoritative.

Shadow turn concerns remain observable and replayable but do not decay,
evict, or consume live workspace capacity. A non-owner turn may produce a
ThoughtJob and a durable shadow-accepted GoalProposal, but only the existing
digest-bound owner promotion can create its Project. Owner turns keep the
ordinary live path. Roll back by setting the flag to `off` and restarting.
Retain the additive cursor, receipt, and source-hold rows for audit; older code
ignores them.
