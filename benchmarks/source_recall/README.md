# Frozen source-recall comparison

The measured improvement came from ranking grounded quotations instead of the
full claim JSON. Adding source semantic candidates did **not** improve final
coverage on this corpus. This is a finite retrieval comparison, not a 1.0
acceptance result or a claim that one database or embedding method is best.

## Observed results, 2026-09-06

The same 120 neutral sources, captured extraction state, 96 queries and reranker
cutoff of 0.95 were used before and after `e22398b`. The development set has 72
queries and the holdout 24. No threshold was adjusted after observing these
results. The three retrieval arms tied on final scores within each run.

| Ranking input | Strict development | Strict holdout | Expected sources found, holdout | Conflict bundles, holdout |
| --- | ---: | ---: | ---: | ---: |
| Full claim JSON | 54/72 | 13/24 | 16/24 | 0/3 |
| Grounded quotations | 60/72 | 17/24 | 20/24 | 3/3 |

Strict success requires all expected sources, no ineligible or forbidden source,
provenance, abstention when requested, and an explicit conflict bundle when
required. Finding an expected source alone does not satisfy that definition.
Expected labels are used only by `assessment.py` after selection. In particular,
fixture `supersedes`, `parents`, `claim` and `value` fields never enter extraction
or ranking. Explicit `deleted` requests exercise canonical source erasure;
`indexed=false` models delayed graph projection.

The extractor was a deployment alias `openai/glm52`; embedding and served alias
were `Qwen/Qwen3-Embedding-8B`, 4096 dimensions; the reranker was
`Qwen/Qwen3-Reranker-8B` with the Qwen3 instruction template. Remote immutable
weight revisions were unknown. The extraction pass captured 119 successful
responses and one timeout, accepting 111 claims. Pending work was frozen for the
paired comparison, not repaired using expected answers. A later rerun with these
model names alone cannot promise identical model output.

Remaining strict failures include relative-time interpretation, distinct event
instances being conflated, and an independently imported derived-summary fixture
whose historical parent lineage was never available to the actual store. These
are observable limitations, not permission to resolve uncertainty by ranking.

Three additional image-caption paraphrases were evaluated separately. Semantic
search found the retained caption in all three; the shared selector returned it
in none. Diagnostic reranker scores were 0.05035, 0.00960 and 0.94349, below the
unchanged 0.95 cutoff. The caption describes a red rectangle and blue circle and
was supplied from a previously qualified neutral image loop. This harness tests
caption retrieval, not image understanding, image embeddings or visual reinspection.
Caption calibration remains incomplete. The cutoff is deployment-specific and
must not become a global default.

Small-corpus median times in the quotation run were 88.6 ms for the shared query
embedding, 22.9 ms for semantic source lookup, and 199.6 ms for selection. Arms
run in fixed order and share cached query embeddings. These measurements are not
a causal speed comparison, production latency estimate or large-index ANN test.

## What runs

All arms use the actual canonical SQLite source ledger, source-claim projection,
LanceDB vector store and shared `RecallSelector`, with 5 records and 6,000
characters as the final budget. OpenAI-compatible extraction and embedding calls
and the Cohere/Jina-style `/v1/rerank` transport use operator-supplied endpoints.
Model calls run sequentially. Extraction is attempted once per inserted source;
failures remain reported in the job status rather than silently retried to pass.

| Arm | Candidate inputs |
| --- | --- |
| `lexical_only` | Canonical source FTS, up to 10 hits |
| `existing_hybrid` | Source FTS plus actual graph recall candidate code, up to 25 candidates |
| `source_semantic` | Those inputs plus canonical source semantic search, up to 15 hits |

Neo4j reads are replaced by an explicit scoped **SQLite graph adapter** with a
10-hit FTS lookup. The adapter does not implement Neo4j or a truth oracle. Real
Neo4j query qualification is separate. Corpus records all belong to a synthetic
owner; the six guest cases test abstention for private information. The fixture's
team/public annotations do not prove shared-authority behavior. Synthetic model
generation labels are not an actual model-swap test; real generation mismatch
and erasure races are covered separately by `test_embedding_generations.py` and
`test_source_vectors.py`.

## Reproduce

Run from the repository root in a Python environment with Colony's dependencies
installed, including LanceDB, PyArrow and Pillow. No additional memory framework
or database service is installed by the harness. Use disposable directories.
Never run two harness processes against the same state directory.

Set these environment variables to explicit benchmark model endpoints and names:

| Required | Meaning |
| --- | --- |
| `COLONY_BENCH_CHAT_BASE_URL`, `COLONY_BENCH_CHAT_MODEL` | Local OpenAI chat endpoint ending in `/v1`, extraction model |
| `COLONY_BENCH_EMBED_BASE_URL`, `COLONY_BENCH_EMBED_MODEL`, `COLONY_BENCH_EMBED_DIMS` | Embedding endpoint, model, full dimensions |
| `COLONY_BENCH_RERANKER_BASE_URL`, `COLONY_BENCH_RERANKER_MODEL` | Rerank endpoint base **without** `/v1`, model |

Optional keys are `COLONY_BENCH_CHAT_API_KEY`, `COLONY_BENCH_EMBED_API_KEY` and
`COLONY_BENCH_RERANKER_API_KEY`. Optional `COLONY_BENCH_LOCAL_HOSTS` declares
comma-separated LAN hostnames for extraction; loopback and private IP ranges
already follow the runtime router's local policy. There is no cloud fallback.
`COLONY_BENCH_RERANKER_PROMPT_STYLE=qwen3` selects the Qwen3 template.
`COLONY_BENCH_CHAT_WEIGHT_REVISION` records a known revision, otherwise unknown.
`COLONY_BENCH_EMBED_QUERY_INSTRUCTION` overrides the query prefix; its default is
`Instruct: Given a search query, retrieve relevant memories that answer it\nQuery: `
with an actual newline. Do not use shell tracing when setting API keys.

```sh
python benchmarks/source_recall/run.py --state-dir /tmp/recall-comparison \
  --output /tmp/recall-before.json --threshold 0.95 --ranking-format verbose-claim-json
python benchmarks/source_recall/run.py --state-dir /tmp/recall-comparison \
  --output /tmp/recall-after.json --threshold 0.95 --ranking-format grounded-quotation-bundles-v1
python -m pytest benchmarks/source_recall/test_harness.py -q
```

The first command extracts the neutral corpus; the second reuses that exact
prepared state. The fixture, extraction declarations and observed embedding
identity are checked before reuse. The harness refuses an existing unmarked
state directory, never loads deployment credentials, and omits endpoints and
API keys from result artifacts. Its two smoke tests use a controlled local HTTP
model and real SQLite/Lance to check transport, resume and assessment behavior;
their results are not semantic-quality evidence.

The frozen fixture SHA-256 is
`00d30ffd6829013445c766ba2244d55142247e99f7b4e68714c1c022ea8dcdff`.
All names, places, source text and example access phrases are synthetic.
`reference-results.json` contains the measured per-query assessments, without
private infrastructure configuration. Use a new directory for another model or
fixture. These previously inspected holdout questions are now a regression set;
a future claim of generalization needs a fresh held-out set.
