"""Feed instance spec: schema, defaults, validation.

A spec is a plain YAML (or JSON) mapping.  Everything that distinguishes one
feed from another lives here — topic, sources, scoring, cadence, destination,
privacy — so an instance is fully reproducible from its spec file.

Minimal spec::

    name: rust-embedded
    title: Rust Embedded Feed
    topic: Rust on microcontrollers — new crates, HALs, RTIC releases.
    destination: {kind: deliver, deliver: origin}
    cadence: {collect: "every 240m", distill: "every 120m"}
    sources:
      github_search: "embedded rust hal OR rtic"
      arxiv: {categories: [cs.SE], keywords: [embedded]}
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

DESTINATION_KINDS = ("command", "deliver", "file")

#: Adapter keys under ``sources:`` — at least one must be configured.
ADAPTER_KEYS = ("x_searches", "hf_keywords", "github_search", "forum_urls", "arxiv", "rss")

DEFAULT_FRAMING = "What changes for us because of this?"

DEFAULT_SCORING = {
    "account_whitelist": 10,
    "engagement": {"per_100_likes": 1, "per_1000_likes": 2, "max": 10},
    "human_curated": 20,
    "benchmark_numbers": 15,
    "code_release": 10,
    "open_weights": 10,
    "thresholds": {"p0": 50, "p1": 35, "p2": 20},
}

DEFAULT_PRIVACY_FORBIDDEN = [
    "private infrastructure details (hardware counts, cluster topology, hostnames, addresses)",
    "names of the operator, their contacts, or internal agent/system codenames",
]


class SpecError(ValueError):
    """Raised when a spec fails validation; ``.errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid feed spec:\n  - " + "\n  - ".join(errors))


@dataclass
class FeedSpec:
    """Validated, defaulted view over a raw spec mapping."""

    raw: dict = field(repr=False)
    name: str = ""
    title: str = ""
    topic: str = ""
    audience: str = "personal"          # personal | group
    destination: dict = field(default_factory=dict)
    llm: dict = field(default_factory=dict)          # {provider, model} pin
    cadence: dict = field(default_factory=dict)
    storage: dict = field(default_factory=dict)      # per-path overrides
    sources: dict = field(default_factory=dict)
    registry: list = field(default_factory=list)     # tiered source registry
    scoring: dict = field(default_factory=dict)
    brief: dict = field(default_factory=dict)
    privacy: dict = field(default_factory=dict)
    learning_loop: bool = True
    budget: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str) -> "FeedSpec":
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            text = f.read()
        if path.endswith(".json"):
            raw = json.loads(text)
        else:
            try:
                import yaml  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise SpecError([f"PyYAML unavailable ({exc}); use a .json spec"]) from exc
            raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise SpecError(["spec root must be a mapping"])
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "FeedSpec":
        spec = cls(raw=raw)
        errors: list[str] = []

        spec.name = str(raw.get("name", "")).strip()
        if not _SLUG_RE.match(spec.name):
            errors.append("name: required kebab-case slug, e.g. 'ai-inference'")
        spec.title = str(raw.get("title") or spec.name.replace("-", " ").title())
        spec.topic = str(raw.get("topic", "")).strip()
        if not spec.topic:
            errors.append("topic: required — one paragraph describing what this feed tracks and why")

        spec.audience = raw.get("audience", "personal")
        if spec.audience not in ("personal", "group"):
            errors.append("audience: must be 'personal' or 'group'")

        dest = raw.get("destination") or {}
        kind = dest.get("kind")
        if kind not in DESTINATION_KINDS:
            errors.append(f"destination.kind: required, one of {DESTINATION_KINDS}")
        elif kind == "command" and not dest.get("send_command"):
            errors.append("destination.send_command: required for kind=command "
                          "(shell command the brief is piped to)")
        elif kind == "deliver" and not dest.get("deliver"):
            errors.append("destination.deliver: required for kind=deliver "
                          "(harness delivery target, e.g. 'origin' or 'platform:chat_id')")
        spec.destination = dest

        cadence = raw.get("cadence") or {}
        for req in ("collect", "distill"):
            if not cadence.get(req):
                errors.append(f"cadence.{req}: required (harness schedule string, e.g. 'every 120m')")
        spec.cadence = cadence

        sources = raw.get("sources") or {}
        if not any(sources.get(k) for k in ADAPTER_KEYS):
            errors.append(f"sources: at least one adapter of {ADAPTER_KEYS} must be configured")
        spec.sources = sources

        spec.registry = raw.get("registry") or []
        for i, entry in enumerate(spec.registry):
            if not isinstance(entry, dict) or not entry.get("name"):
                errors.append(f"registry[{i}]: each entry needs at least a 'name'")
            elif entry.get("weight", "P1") not in ("P0", "P1", "P2"):
                errors.append(f"registry[{i}] ({entry.get('name')}): weight must be P0/P1/P2")

        spec.scoring = _merged(DEFAULT_SCORING, raw.get("scoring") or {})
        spec.llm = raw.get("llm") or {}
        spec.storage = raw.get("storage") or {}
        spec.brief = raw.get("brief") or {}
        spec.brief.setdefault("framing", DEFAULT_FRAMING)
        spec.brief.setdefault("standard_cap", 5)
        spec.privacy = raw.get("privacy") or {}
        spec.privacy.setdefault("forbidden", list(DEFAULT_PRIVACY_FORBIDDEN))
        spec.learning_loop = bool(raw.get("learning_loop", True))
        spec.budget = raw.get("budget") or {}

        if errors:
            raise SpecError(errors)
        return spec

    # ------------------------------------------------------------- storage
    def data_root(self, base: str = "~/.hermes/data/feeds") -> str:
        root = self.storage.get("root") or f"{base}/{self.name}"
        return os.path.expanduser(root)

    def path(self, key: str, default_name: str) -> str:
        """Resolve a storage path, honouring per-key spec overrides."""
        override = self.storage.get(key)
        if override:
            return os.path.expanduser(override)
        return os.path.join(self.data_root(), default_name)

    @property
    def queue_path(self) -> str:
        return self.path("queue_path", "queue.json")

    @property
    def seen_db(self) -> str:
        return self.path("seen_db", "seen.db")

    @property
    def briefs_dir(self) -> str:
        return self.path("briefs_dir", "briefs")

    @property
    def alert_path(self) -> str:
        return self.path("alert_path", "alert.json")

    @property
    def deltas_log(self) -> str:
        return self.path("deltas_log", "briefs/_registry_deltas.log")

    # -------------------------------------------------------- engine config
    def engine_config(self) -> dict[str, Any]:
        """Render the self-contained collector-engine config for this feed."""
        s = self.sources
        arxiv = s.get("arxiv") or {}
        sc = self.scoring
        cfg: dict[str, Any] = {
            "feed_name": self.name,
            "queue_path": self.queue_path,
            "seen_db": self.seen_db,
            "alert_path": self.alert_path,
            # adapters (empty list / missing = adapter disabled)
            "x_searches": s.get("x_searches") or [],
            "x_results_per_query": s.get("x_results_per_query", 15),
            "key_accounts": s.get("key_accounts") or [],
            "hf_keywords": s.get("hf_keywords") or [],
            "github_search": s.get("github_search") or "",
            "github_keywords": s.get("github_keywords") or [],
            "forum_urls": s.get("forum_urls") or [],
            "forum_title_keywords": s.get("forum_title_keywords") or [],
            "arxiv_categories": arxiv.get("categories") or [],
            "arxiv_query_terms": arxiv.get("query_terms") or [],
            "arxiv_keywords": arxiv.get("keywords") or [],
            "rss": s.get("rss") or [],
            # scoring
            "keyword_categories": sc.get("keyword_categories") or [],
            "scoring": {
                "account_whitelist": sc.get("account_whitelist", 10),
                "engagement_per_100_likes": sc.get("engagement", {}).get("per_100_likes", 1),
                "engagement_per_1000_likes": sc.get("engagement", {}).get("per_1000_likes", 2),
                "engagement_max": sc.get("engagement", {}).get("max", 10),
                "human_curated": sc.get("human_curated", 20),
                "benchmark_numbers": sc.get("benchmark_numbers", 15),
                "code_release": sc.get("code_release", 10),
                "open_weights": sc.get("open_weights", 10),
            },
            "benchmark_regex": sc.get(
                "benchmark_regex",
                r"\d+\s*(tok/s|tokens?/?s|t/s|tflops?|ms|latency|throughput)",
            ),
            "priority_thresholds": sc.get("thresholds", DEFAULT_SCORING["thresholds"]),
            # alert delivery for the alert stage (kind=command only)
            "send_command": (self.destination.get("send_command")
                             if self.destination.get("kind") == "command" else ""),
            "alert_min_priority": (self.brief.get("alert_min_priority", "P0")),
        }
        # Legacy compatibility: a spec may point config_path at a hand-written
        # engine config; then that file wins entirely (see manager.create).
        return cfg


def _merged(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out
