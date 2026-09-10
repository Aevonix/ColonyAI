# Standing directives and task instructions

Ordinary task instructions stay in the canonical conversation and current task context. They do not automatically become standing boundaries. For example, “For this task, do not deploy widget-service” must not restrict an unrelated later task.

Automatic capture requires an explicit lasting opener such as “From now on”, “Going forward”, “Always” or “Never”, with a supported imperative clause. “From now on, do not deploy widget-service” becomes an action boundary. “Never read secrets.env” explicitly creates an observation boundary. Unrelated reading instructions elsewhere in the message cannot change the boundary level.

The parser is deliberately narrow. It skips messages explicitly marked temporary, factual statements, quoted examples, unsupported requirements, conditional rules and negative complements that the keyword guard cannot faithfully enforce. It does not turn “Never deploy unless I approve” into an unconditional prohibition, or “Never forget to check the backup” into a prohibition on checking. Mixed temporary and standing instructions should be separated into distinct messages when lasting capture is intended. These instructions remain available as ordinary task evidence even when they are not promoted.

Only the exact rule clause is injected. Explanations, subsequent sentences, credentials and unrelated task details outside that clause are excluded. This is scope selection, not a general secret detector; an instruction that itself contains sensitive text is still sensitive canonical evidence.

## Source and correction

New automatic rules store a reference to the attributed owner source, source version, message hash and clause span in the existing directive metadata column. The existing directive row stores no second copy of the rule prose or match terms. Enforcement and context resolve the current canonical source each time. A correction or erasure of that source withdraws the rule, pending acknowledgment, pending lift and cached block presentation. Capture events contain IDs; source-backed capture and guard logs omit rule prose.

Identical active restatements are deduplicated against the first source. Erasing or correcting that source withdraws the rule even if another conversation repeated it. A later new explicit statement can create a new source-bound rule. This repair does not reconstruct provenance for old rows or erase historical logs and backups.

The authorized manual directive API remains explicit operator intent and does not require a standing opener or a conversation source. Its existing revoke API remains the way to retire a manual rule. The standalone “pause autonomy” command keeps its existing meaning. No approval service or additional model call is introduced. The optional legacy LLM interface cannot create unsupported rules or bypass source admission, and skips unavailable or duplicate candidates.

## Compatibility

The SQLite schema is unchanged. Existing manual/configuration and legacy rows remain readable. New automatic source-backed rows have blank prose and match terms, so an older backend cannot hydrate or enforce them on rollback. Keep this limitation visible during rollback; do not restore duplicate text to hide it. Recovery of proven old task instructions is an explicit, scoped retirement through the existing revoke API, not an automatic sweep of owner rules.
