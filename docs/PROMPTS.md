# Colony Prompt Architecture

All internal LLM cognition roles share one composable prompt system:
`colony_sidecar/cognition/charter.py`. This replaces per-module hand-rolled
system prompts that had drifted into inconsistent identities, duplicated
output rules, and no shared agency doctrine.

Current version: **1.0.0** (`charter.PROMPT_VERSION`). Bump on any doctrine
or contract change and add a row to the changelog below.

## Why

A pseudo-AGI agent's behavior is substantially its prompts. Fragmented
prompts mean fragmented judgment: the executor had no evidence discipline,
the thinker had no grounding requirement, nothing carried calibrated
confidence, and every module described "who am I" differently. The charter
gives every role the same spine and lets each role stay a slim overlay.

## Structure

`build_system_prompt(role, ...)` composes XML-tagged sections in order:

| Section | Source | Purpose |
|---|---|---|
| `<charter>` | static | shared identity + the 8-point agency doctrine |
| `<role>` | `ROLE_BLOCKS[role]` | mission + role rules |
| `<self_model>` | caller (self-model brief) | calibrated competence: route, decline, escalate |
| `<boundaries>` | caller (DirectiveGuard `context_brief`) | standing owner directives; cite on refusal |
| `<skills>` | caller (procedure-memory retrieval) | how similar work succeeded before |
| `<corrections>` | caller (post-mortems, owner feedback) | past mistakes, each line `avoid:`-prefixed |
| `<context>` | caller | role-specific context |
| `<output>` | `ROLE_BLOCKS[role]` | output contract (shared JSON rules + schema) |

Sections not supplied are omitted. Every dynamic section is char-budgeted
(`SECTION_BUDGETS`) with an explicit truncation marker so injections can
never crowd out the role's instructions, and truncation is visible rather
than silent.

## The doctrine (summary)

1. Judgment before action: act / ask (with reasoning + confidence) /
   refuse (citing the boundary). The only three moves.
2. Evidence over assertion: verify before claiming completion; a plain
   failure report beats a hopeful guess.
3. Fewest, weakest actions; reversible over irreversible.
4. Continuity first: advance existing work; never redo a recent artifact.
5. Compound: knowledge, skills, capability repair over one-off effort.
6. Outcome-first, brief, true reporting.
7. Quality over quantity: an empty result is a good answer.
8. Stated confidence is data: it trains earned autonomy. Calibrate honestly.

## Output contracts and the trust engine

Every judgment-bearing role schema REQUIRES a `confidence` float. The
graduated-autonomy trust engine consumes these: calibration (stated
confidence vs realized outcome) is what earns or loses autonomous scope.
Do not remove or default these fields.

## Roles

`executor`, `thinker`, `planner`, `observer` (turn/event cognition),
`synthesis` (goal/fact inference), `worker` (queue workers),
`directed_intake` (owner directive -> scoped task). Add new roles as slim
mission/rules/output blocks; put shared behavior in the charter, never in
role blocks.

## Provenance of techniques

- Evidence-over-assertion, persistence/continuity doctrine, truthful
  outcome reporting, modular conditional fragments: Claude Code's system
  prompt architecture.
- XML-tagged section composition, token-budgeted confidence-sorted
  injection, `avoid:`-prefixed correction facts, SOUL/personality
  separation: ByteDance DeerFlow.
- Calibrated-confidence outputs feeding an autonomy trust engine,
  milestone-contract reporting, ground-truth-over-assertion verification:
  operational practice from frontier agentic harnesses.

## Deployment personality

The charter is capability doctrine, not personality. A deployment's persona
(its SOUL) lives in the deployment layer (e.g. the framework's SOUL.md) and
should not duplicate doctrine; conversely the charter never contains
persona. `COLONY_AGENT_NAME` supplies the agent's name at compose time.

## Integration status

New adopters should import `build_system_prompt` and delete their inline
prompt constants. Existing modules migrate as they are touched (see
ROADMAP-COGNITION.md program state for which have adopted).

## Changelog

| Version | Change |
|---|---|
| 1.0.0 | Initial charter: 8-point doctrine, 7 roles, budgeted sections, confidence-mandatory contracts. |
