# Model qualification

The opt-in `colony models` command records bounded observations through existing consumers. It does not select or publish a deployment, start services, change model roles on a running agent, or create an evaluation database.

```sh
colony models inspect interactive --config /private/model-config.json
colony models evaluate interactive --config /private/model-config.json --roles chat,extraction --output /private/results/candidate-01
colony models compare /private/results/incumbent-01 /private/results/candidate-01
```

`--config` is an existing Colony host model configuration, including its existing `modelPool`, role and network declarations. Credentials are consumed through that private configuration and are not copied into recipes or result records. Inspect reads declarations without querying endpoints. Evaluate makes model requests through an isolated router configured from a private copy; it never edits the supplied configuration. The selected case role is pinned to the requested binding. Supporting roles, such as semantic memory admission review, retain their configured bindings and appear in call observations. This is a recipe and consumer comparison, not an intrinsic score for isolated model weights.

A case can declare `target_tasks` when its real consumer normally chooses a function through a task mapping. Only those named tasks are redirected to the case's qualification role in the isolated router. For example, memory cases bind `source_claim_extraction` to their extraction candidate even when production routes that task through reasoning. The original routing snapshot, frozen case declarations and per-attempt `qualification_routing` record show that isolation explicitly. Judging and unrelated task mappings remain configured as supplied. A role deadline also remains the qualification role's configured deadline; this is not a replay of a different production role's timing policy.

Use new result directories for predeclared repeated trials. Each selected case has exactly one attempt in a run. There is no automatic resampling, prompt search, model selection or promotion. Existing consumers may themselves make multiple calls, and observations retain their separately resolved roles and fallback history. A useful answer from a fallback cannot make the requested primary pass.

## Coverage and evidence

Every case declares its consumer boundary. `role_completion` cases test direct response semantics through the existing completion router. They do not qualify foreground Hermes conversation, tools, automatic memory injection, streaming latency or shared task behavior. `cognition_consumer` cases must exercise the actual cognition consumer and grade its retained effects. `native_hermes` requires the installed native path and its own evidence. Retrieval, speech and media consumers remain separately labelled. Reports never convert a few passing direct calls into full role qualification.

The initial direct cases check a grounded answer with an unknown value, and structured extraction that retains time and conditional constraints. The cognition cases run actual source formation and semantic admission review, reopen the isolated ledger, then prepare and pack recollection in a later session. They separately grade useful formation, junk/fiction promotion, source grounding, correction, and canonical lineage. Raw source recall cannot hide a failure to form useful memory. These exercise lexical recollection, not embeddings, native injection or a generated native answer.

These are narrow checks, not a broad intelligence benchmark. Independent expected fields are frozen separately from model input. Cases and supporting consumers can add artifact, ledger, input-exposure and answer checks without supplying those oracles to the model. An evaluation model judge is not required; the existing memory admission judge remains part of the consumer being tested.

Memory case version 3 reports an unexercised check as unknown: absent promoted claims do not demonstrate their grounding, and junk sources skipped after an earlier processing failure do not demonstrate useful abstention. An observed junk promotion fails even if another source was skipped. A case without junk inputs omits that inapplicable check. Previous case versions and reports retain their original grades; changed evaluator identities prevent treating this measurement correction as a model gain.

Configured model identity, provider-reported response label and declared weight revision are separate. A returned model label is not proof of loaded weights. Unobserved weights, quantization, engine, hardware, tokenizer, first-token timing and missing provider usage stay unknown. Existing-router call duration includes transport/processing; it is not decode throughput, queue time or GPU occupancy. The observer measures consumer `complete` calls, not every hidden physical SDK retry. Native adapters need their own supported request-boundary observations.

`--evidence-mode controlled` labels a controlled fixture run. The default `actual_inference` labels an operator-directed endpoint evaluation, not independent proof that the endpoint served particular weights. Public CI uses deterministic fixtures and local controlled HTTP servers; it does not access private inference servers. No normal operation depends on completing this suite.

## Results and interruption

The result directory contains a frozen `run.json`, per-case `attempts/<id>/started.json` and `result.json`, and numbered JSON/Markdown reports. Case, input, oracle, recipe and implementation hashes identify what ran. Files are published atomically without replacing earlier results. Results retain every declared case, including unavailable capabilities, setup errors, empty outputs, timeouts and interruptions. Private outputs remain in the operator-selected private result directory; do not commit these directories to the public repository.

To continue an interrupted run, repeat the same evaluate command with `--resume`. The same recipe, suite and implementation are required. Completed attempts stay byte-identical. A recorded start without a result becomes `interrupted`; it is never resubmitted. Only untouched cases execute. A process lock prevents concurrent execution of one run directory. The maximum declared run is 128 cases and one hour; each case has its own timeout and output bound. Consumers must propagate cancellation and clean up their owned resources. The runner supplies a separate temporary state directory per attempt and removes it on ordinary completion, error or cancellation. Abrupt process death may leave that isolated temporary state; its cleanup remains explicitly unconfirmed rather than claiming a clean shutdown.

Comparisons retain failures in denominators, separate roles and boundaries, and reject paired gain claims when case/oracle/evaluator identities or evidence modes differ. Evaluator module/name/source identity is explicit; a changed rubric cannot masquerade as model improvement. Other consumer implementation changes are flagged as system changes. Durations are grouped by outcome so immediate setup failures cannot reduce a completion median. Latency differences are shown only for comparable successful outcomes; raw failure durations remain visible. Small samples have descriptive medians and counts, without tail-latency or confidence claims. Changed hardware or serving settings make this a recipe comparison, not proof of a gain attributable to weights alone.

## Extending cases

`qualification.records.CaseSpec` is the versioned case record. A consumer is an async function `(inputs, context)` returning `{'output': ..., 'effects': ...}`. It receives an isolated `context.state_dir`, the existing router through `context.router`, and `context.observe(dict)` for additional labelled evidence. It never receives the oracle. An evaluator receives the returned observations and its frozen oracle and returns named `True`, `False` or `None` checks. Unknown checks do not pass. Register these in the existing small case registries; no new service or transport is needed.

Keep fixtures neutral and independent of specific machines or models. Private channel/device adapters and owner examples belong in deployment code. Record any supporting role, actual input, source state or hardware effect required by the case; a successful advertised tool or a model's claimed completion is not a substitute for observing its effect.
