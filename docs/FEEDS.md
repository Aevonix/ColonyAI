# Colony Feeds — spec-driven intelligence pipelines

Turn "stay on top of TOPIC and brief DESTINATION" into a reproducible pipeline
you instantiate from one YAML file. Any number of feeds on any topics coexist;
each is fully described by its spec.

```
collect (script, no LLM)  ->  distill (agent)   ->  destination
  source adapters              actionable brief      group chat, DM,
  score + dedup                with source links     or file archive
  overwrite queue
                     optional: digest (daily rollup)
                               alerts (P0 pushes between briefs)
                               discovery (agent researches new sources)
```

## Quickstart

```bash
colony feeds validate my-feed.yaml     # clear, itemized errors
colony feeds create   my-feed.yaml     # data dirs + prompts + scheduled jobs
colony feeds run my-feed collect       # first collection, inline
colony feeds run my-feed distill       # first brief, in the background
colony feeds status my-feed
colony feeds pause|resume|delete my-feed [--purge]
```

Without an installed package: `PYTHONPATH=<repo>/sidecar python3 -m
colony_sidecar.feeds.cli ...`. A commented example spec lives at
`sidecar/colony_sidecar/feeds/example-feed.yaml`.

## Spec schema

Required: `name` (slug), `topic`, `destination`, `cadence.collect`,
`cadence.distill`, and at least one source adapter.

| Section | What it controls |
|---|---|
| `name`, `title`, `topic` | Identity + the topic charter injected into every agent prompt |
| `audience` | `personal` or `group`; group adds community-input handling (human-shared links, promotion rules, one-way-broadcast rules) |
| `destination` | `kind: deliver` (harness delivery target like `origin` or `platform:chat_id`), `kind: command` (brief is piped to `send_command`; `read_back` tells the distill agent how to read the group), or `kind: file` (archive only) |
| `llm` | `{provider, model}` pin applied to the agent jobs so global inference-config drift can never silently skip them |
| `cadence` | Harness schedule strings per stage; omit `digest`/`alerts`/`discovery` to disable them |
| `sources` | Adapters: `x_searches` (xurl CLI), `hf_keywords`, `github_search`+`github_keywords`, `forum_urls` (Discourse), `arxiv {categories, query_terms, keywords}`, `rss [{name,url}]`. Empty = disabled |
| `registry` | Tiered (P0/P1/P2) source registry rendered into prompts and used by the promotion rules |
| `scoring` | `keyword_categories` ({keywords, points} buckets), engagement/code/benchmark weights, `thresholds` for P0/P1/P2 |
| `brief` | `framing` (the "why it matters" question every item must answer), `standard_cap`, `context_files`, `extra_instructions` |
| `privacy` | `forbidden` list + optional `aliases`/`note`, rendered as hard rules into every prompt |
| `learning_loop` | Log content-weight promotions of unknown sources to `_registry_deltas.log` so the registry evolves |
| `storage` | Per-path overrides; the default namespaces everything under `~/.hermes/data/feeds/<name>/` |

## How it runs

- **collect** and **alerts** are plain-python cron jobs (`--no-agent`): a
  generated per-instance shim points the shared, stdlib-only engine
  (`engine.py`, deployed as `colony-feed-engine.py`) at the instance config.
  Items are deduped against a per-feed sqlite seen-ledger; the queue is
  overwritten wholesale with never-seen items only, so a brief never repeats.
- **distill/digest/discovery** are agent cron jobs whose prompts are rendered
  from the spec (see `template.py`). Delivery follows the destination kind:
  native harness delivery, an explicit pipe command, or archive-only.
- Alert delivery: with `kind: command` the engine pipes alert text through
  `send_command` itself; otherwise the job's stdout is delivered by the
  scheduler (empty stdout = silent).

## Conversational management

Enable the `feeds-manage` plugin (see `plugins/feeds-manage/`) to give the
agent `feed_create / feed_list / feed_status / feed_pause / feed_resume /
feed_run / feed_delete` tools. "Keep me informed about X" becomes a spec the
agent authors; when the request names no destination, briefs deliver to the
conversation the request came from. Plugin config lives in
`~/.colony-feeds.json` (python interpreter, PYTHONPATH to this repo,
specs dir).

## Operational notes

- Provider pinning edits the scheduler's `jobs.json` directly (timestamped
  backup first) because the cron CLI has no pin flag. Pin provider and model
  as SEPARATE fields.
- `hermes cron run <id>` blocks for the whole agent run — the framework
  triggers agent stages detached for exactly that reason.
- A feed with `storage.config_path` set keeps an externally-owned collector
  config (e.g. one mutated by your own learning-loop scripts); the engine
  also accepts the legacy pre-framework config shape.
- Auxiliary per-feed enrichment (leaderboard snapshots, chat-intel ingestion)
  stays outside the framework: write files their own way and reference them
  via `brief.context_files`.
