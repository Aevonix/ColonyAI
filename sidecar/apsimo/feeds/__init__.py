"""Colony Feeds — spec-driven intelligence feed pipelines.

A *feed* is a reproducible pipeline instantiated from a single YAML/JSON spec:

    collect  -> scriptable source adapters (X, GitHub, Hugging Face, Discourse
                forums, arXiv, RSS) score + dedup items into a queue
    distill  -> an agent turns the queue into an actionable brief and delivers
                it to the feed's destination (group chat, DM, or file archive)
    digest   -> optional daily rollup
    alerts   -> optional top-priority items pushed between briefs
    discovery-> optional agent run that researches and proposes new sources

Instances are created/managed with ``colony feeds`` (see cli.py) or
conversationally through the ``feeds-manage`` harness plugin.  All state is
namespaced per instance under the harness data dir so any number of feeds on
any topics can coexist.
"""

from .spec import FeedSpec, SpecError  # noqa: F401
