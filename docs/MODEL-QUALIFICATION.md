# Model qualification

The opt-in `pacomind models` command records bounded observations through existing consumers. It does not select or publish a deployment, start services, change model roles on a running agent, or create an evaluation database.

```sh
pacomind models inspect interactive --config /private/model-config.json
pacomind models evaluate interactive --config /private/model-config.json --roles chat,extraction --output /private/results/candidate-01
pacomind models compare /private/results/incumbent-01 /private/results/candidate-01
```

`--config` is an existing PacoMind host model configuration, including its existing `modelPool`, role and network declarations. Credentials are consumed through that private configuration and are not copied into recipes or result records. Inspect reads declarations without querying endpoints. Evaluate makes model requests through an isolated router configured from a private copy; it never edits the supplied configuration. The selected case role is pinned to the requested binding. Supporting roles, such as semantic memory admission review, retain their configured bindings and appear in call observations. This is a recipe and consumer comparison, not an intrinsic score for isolated model weights.

A case can declare `target_tasks` when its real consumer normally chooses a function through a task mapping. Only those named tasks are redirected to the case's qualification role in the isolated router. For example, memory cases bind `source_claim_extraction` to their extraction candidate even when production routes that task through reasoning. The original routing snapshot, frozen case declarations and per-attempt `qualification_routing` record show that isolation explicitly. Judging and unrelated task mappings remain configured as supplied. A role deadline also remains the qualification role's configured deadline; this is not a replay of a different production role's timing policy.

Use new result directories for predeclared repeated trials. Each selected case has exactly one attempt in a run. There is no automatic resampling, prompt search, model selection or promotion. Existing consumers may themselves make multiple calls, and observations retain their separately resolved roles and fallback history. A useful answer from a fallback cannot make the requested primary pass.

## Coverage and evidence

Every case declares its consumer boundary. `role_completion` cases test direct response semantics through the existing completion router. They do not qualify foreground Hermes conversation, tools, automatic memory injection, streaming latency or shared task behavior. `cognition_consumer` cases must exercise the actual cognition consumer and grade its retained effects. `native_hermes` requires the installed native path and its own evidence. Retrieval, speech and media consumers remain separately labelled. Reports never convert a few passing direct calls into full role qualification.

The initial direct cases check a grounded answer with an unknown value, and structured extraction that retains time and conditional constraints. The cognition cases run actual source formation and semantic admission review, reopen the isolated ledger, then prepare and pack recollection in a later session. They separately grade useful formation, junk/fiction promotion, source grounding, correction, and canonical lineage. Raw source recall cannot hide a failure to form useful memory. These exercise lexical recollection, not embeddings, native injection or a generated native answer.

Four additional direct cases cover specific text decisions:

| Case | What its independent oracle checks | What it does not establish |
| --- | --- | --- |
| `reasoning.task-evidence` | Exact task/generation evidence; acceptance, execution, stopping and terminal outcomes; unknown delivery and process cleanup | Real task execution, stopping or process cleanup |
| `planning.dependencies-and-consent` | Earliest dependency/resource schedule, critical path, withheld publication and a separately conditional consent outcome | A general scheduler, long-horizon planning or production deployment |
| `judging.consequential-claims` | Supported, contradicted and unsupported claims, including untrusted claimed authority | General factual accuracy or the quality of the production memory admission judge |
| `coding.integer-boundary` | A Python boolean/integer validation defect, selection among fixed repairs, and type/range examples | Generated code execution, repository editing, tool use or an entire coding harness |

Select them explicitly; the default remains `chat,extraction`:

```sh
pacomind models evaluate interactive --config /private/model-config.json --roles reasoning,planning,judging,coding --output /private/results/structured-01
```

Each case makes one existing-router completion call, with a 60-second case deadline, 1,024 requested output tokens and a 16 KiB result bound. Four cases declare at most 240 seconds in total, excluding setup and report writing; configured role deadlines can be shorter. No fallback is requested. These limits measure fitness for that declared recipe, not a model's unlimited-time capability. Eight completion calls across two bindings form one small paired comparison; hidden SDK retries are not counted as separate suite attempts. Freeze both runs' source, case/oracle identities and role budgets before observing results, preserve first attempts and compare individual checks. A timeout is a deadline failure, not evidence that another model reasons better.

These are narrow checks, not a broad intelligence benchmark. Independent expected fields are frozen separately from model input. Cases and supporting consumers can add artifact, ledger, input-exposure and answer checks without supplying those oracles to the model. An evaluation model judge is not required; the existing memory admission judge remains part of the consumer being tested.

Memory checks report unexercised behavior as unknown: absent promoted claims do not demonstrate their grounding, and junk sources skipped after an earlier processing failure do not demonstrate useful abstention. An observed junk promotion fails even if another source was skipped. A case without junk inputs omits that inapplicable check.

Memory case version 4 matches finite, independently specified complete representations across subject, subject identity, relation, value, source and category. A value may omit a noun already represented by its relation when the oracle explicitly permits that equivalent form. A wrong subject or negated relation cannot pass merely because its value contains the expected phrase. Selected correction cards must also match their retained claims. These are declared case contracts, not a universal semantic-equivalence classifier; inspect the bounded completion evidence when an unlisted representation fails. Previous cases and reports retain their original grades. Changed evaluator identities prevent treating a measurement correction as a model gain.

Configured model identity, provider-reported response label and declared weight revision are separate. A returned model label is not proof of loaded weights. Unobserved weights, quantization, engine, hardware, tokenizer, first-token timing and missing provider usage stay unknown. Existing-router call duration includes transport/processing; it is not decode throughput, queue time or GPU occupancy. The observer measures consumer `complete` calls, not every hidden physical SDK retry. Native adapters need their own supported request-boundary observations.

`--evidence-mode controlled` labels a controlled fixture run. The default `actual_inference` labels an operator-directed endpoint evaluation, not independent proof that the endpoint served particular weights. Public CI uses deterministic fixtures and local controlled HTTP servers; it does not access private inference servers. No normal operation depends on completing this suite.

## Results and interruption

The result directory contains a frozen `run.json`, per-case `attempts/<id>/started.json` and `result.json`, and numbered JSON/Markdown reports. Case, input, oracle, recipe and implementation hashes identify what ran. Files are published atomically without replacing earlier results. Results retain every declared case, including unavailable capabilities, setup errors, empty outputs, timeouts and interruptions. Private outputs remain in the operator-selected private result directory; do not commit these directories to the public repository.

Explicit qualification runs also retain the final text returned to each consumer, so rejected memory candidates can be diagnosed without changing production memory logging. The aggregate serialized text budget per case is the smaller of its declared output limit and 64 KiB; the run manifest records the sum. Each captured response includes its original `records.encode` hash and size, retained size, and truncation status. Truncation affects evidence only, not the consumer's response. Raw SDK envelopes and arbitrary exception messages are not retained. Known router-generated failure categories may be recorded, while absent serving identity and partial failed responses stay unknown. The candidate label is distinct from the actual selected binding and the observed target or supporting role.

To continue an interrupted run, repeat the same evaluate command with `--resume`. The same recipe, suite and implementation are required. Completed attempts stay byte-identical. A recorded start without a result becomes `interrupted`; it is never resubmitted. Only untouched cases execute. A process lock prevents concurrent execution of one run directory. The maximum declared run is 128 cases and one hour; each case has its own timeout and output bound. Consumers must propagate cancellation and clean up their owned resources. The runner supplies a separate temporary state directory per attempt and removes it on ordinary completion, error or cancellation. Abrupt process death may leave that isolated temporary state; its cleanup remains explicitly unconfirmed rather than claiming a clean shutdown.

### Native elapsed budgets

On the [qualified Hermes build](HERMES-HOOK-COMPATIBILITY.md), `run_budget_seconds` supplies a wrap-up notice and influences implicit stale-response limits. It does not enforce an absolute elapsed deadline. An implicitly configured local non-streaming request can have no stale cutoff, and explicit provider timeout settings take precedence over the run-budget adjustment.

Use Hermes' existing named-provider `request_timeout_seconds` and `stale_timeout_seconds` settings for transport behavior. The request timeout bounds socket operations; a stream making progress can outlive it. Stale detection concerns response progress and may scale with context. `agent.gateway_timeout` is an inactivity limit. None of these settings alone proves that a native task stops at a fixed elapsed time.

A native qualification consumer must keep the blocking native run off the async event loop and retain ownership of its worker. On case cancellation, use the existing `agent.hard_interrupt()` interface and observe that worker's termination before removing its state. Cancelling only an await on a thread does not stop the thread. If cleanup cannot be confirmed, retain that limitation in the result. Do not award completion for a request accepted before the deadline or for tools that continue afterward.

Declare separate foreground and durable-work cases before testing, including representative context, tool rounds, time to useful visible output, final-answer deadline and cancellation evidence. A binding that misses the foreground budget is ineligible for that tested foreground recipe; it can be assessed separately for longer durable work. A longer allowed runtime requires its own declared case and useful final outcome. It cannot turn the original timeout into a pass. Existing router capability limits are deployment declarations, not automatic adoption of benchmark measurements.

Comparisons retain failures in denominators, separate roles and boundaries, and reject paired gain claims when case/oracle/evaluator identities or evidence modes differ. Evaluator module/name/source identity is explicit; a changed rubric cannot masquerade as model improvement. Other consumer implementation changes are flagged as system changes. Durations are grouped by outcome so immediate setup failures cannot reduce a completion median. Latency differences are shown only for comparable successful outcomes; raw failure durations remain visible. Small samples have descriptive medians and counts, without tail-latency or confidence claims. Changed hardware or serving settings make this a recipe comparison, not proof of a gain attributable to weights alone.

## Extending cases

`qualification.records.CaseSpec` is the versioned case record. A consumer is an async function `(inputs, context)` returning `{'output': ..., 'effects': ...}`. It receives an isolated `context.state_dir`, the existing router through `context.router`, and `context.observe(dict)` for additional labelled evidence. It never receives the oracle. An evaluator receives the returned observations and its frozen oracle and returns named `True`, `False` or `None` checks. Unknown checks do not pass. Register these in the existing small case registries; no new service or transport is needed.

Keep fixtures neutral and independent of specific machines or models. Private channel/device adapters and owner examples belong in deployment code. Record any supporting role, actual input, source state or hardware effect required by the case; a successful advertised tool or a model's claimed completion is not a substitute for observing its effect.
