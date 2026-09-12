---
name: apsimo-skill-creator
description: Create or refine Hermes skills for recurring workflows.
---

# Create useful skills

Use this workflow when asked to create or improve a skill, or when an authorized task reveals a reusable procedure worth preserving. Personal facts belong in memory, current work belongs in task state, and runtime defects need code fixes. A skill is useful when it changes a future decision or avoids repeated work.

## Decide what to preserve

Use `skills_list` and load likely matches with `skill_view(name)`. Improve an existing relevant skill before creating an overlapping one. State the recurring request it serves and the concrete lesson, procedure or resource that makes it useful. One successful workaround can justify a narrowly scoped instruction; it does not establish a universal rule.

Keep general capabilities in reusable skills and deployment specifics in the private profile. Discover configured tools, roles and resource locations when needed. Do not embed credentials, personal conversations, machine names or a preferred model in a public skill.

## Write the smallest useful package

Start with one `SKILL.md`. Match its folder and frontmatter name, using lowercase letters, digits and hyphens, at most 64 characters. Include YAML `name` and `description`, followed by a nonempty Markdown body. Keep the description within 60 characters so Hermes can display the complete trigger in its skill index. Put longer scope guidance in the body.

Write instructions that change decisions: when to act, which evidence to inspect, a useful procedure, and what completion looks like. Explain unusual constraints briefly. Remove generic advice, repeated policies, incident logs, filler and speculative edge cases.

Add a supporting file only when it saves repeated work. Use `references/` for conditional detail, `scripts/` for reusable deterministic operations, and `templates/` or `assets/` for output materials. Link each file where it becomes relevant, and load it through `skill_view(name, file_path=...)` only when needed. A short skill needs no extra directories or UI metadata.

## Use Hermes authoring tools

Read the current `skill_manage` schema. Its native call shape is an `operations` array; every operation includes `name` and `action`.

- `create` takes the full frontmatter and body in `content`.
- `patch` with `old_string` and `new_string` makes a targeted edit. `patch` with `content` replaces the whole file; read the existing skill first.
- `write_file` takes a relative `file_path` and `file_content`. Related creation and supporting-file writes can share one atomic batch, with creation first.
- `remove_file` removes a supporting file. `delete` must be the sole operation; check callers before removing a skill.

These tools respect the configured skill location. If they are unavailable, use the authorized repository or profile path and report that native discovery remains unverified. A skill grants no additional authority: follow existing task authorization and deployment policy, preserve protected core boundaries, and do not introduce another approval workflow. A staged write is pending, not installed.

## Check value and finish

Read the saved skill with `skill_view` and check its discovery description with `skills_list`. Successful file creation alone does not prove that an existing session has learned about it. Use the deployment's supported refresh path if immediate availability matters, and verify the next request can discover and load it.

For a substantive procedure, try a representative task in an isolated workspace. Check the resulting artifact or observable outcome; run added scripts on a small fixture. Compare against the previous workflow when claiming an improvement. Parsing and tool delivery prove compatibility, not better judgment or model quality.

Fix demonstrated failures, then stop when the requested behavior works. Record what changed, what was exercised and any remaining limit in the task result. Refine from later usage; remove or merge skills that add no demonstrated value, within the authorized scope. Do not grow a testing service or keep iterating for hypothetical risks.
