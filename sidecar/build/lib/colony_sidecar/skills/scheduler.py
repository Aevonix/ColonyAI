"""Colony Skills — skill scheduler with LRU-Cold eviction."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

from colony_sidecar.skills.budget import ContextBudget
from colony_sidecar.skills.index import SkillIndex
from colony_sidecar.skills.loader import LoadedSkill, SkillLoader

logger = logging.getLogger(__name__)


@dataclass
class SkillsConfig:
    """Configuration for skill scheduling."""

    context_budget_tokens: int = 8192
    retention_policy: str = "lru"       # lru | ttl | manual
    hot_skill_threshold: int = 5        # use_count above which a skill is "hot"
    cold_ttl_secs: int = 1800           # 30 min TTL for cold skills when policy=ttl


class SkillScheduler:
    """Evaluates trigger patterns, manages skill load/evict lifecycle,
    and enforces the context budget.
    """

    def __init__(
        self,
        index: SkillIndex,
        loader: SkillLoader,
        budget: ContextBudget,
        config: SkillsConfig,
    ) -> None:
        self._index = index
        self._loader = loader
        self._budget = budget
        self._config = config

    async def evaluate(self, event_text: str) -> List[LoadedSkill]:
        """Called once per autonomy loop tick.

        1. Match event_text against SkillIndex (no imports).
        2. For each match, if not loaded and budget permits: load.
        3. If budget full: evict least-recently-used cold skill first.
        4. Return list of all currently loaded skills relevant to event.
        """
        candidates = self._index.match(event_text)

        for entry in candidates:
            if entry.skill_id in self._loader.loaded_ids():
                continue  # Already loaded; load_for_event will update last_used_at

            needed = entry.context_tokens_estimate
            current_used = self._loader.token_footprint()

            if not self._budget.has_capacity(needed, current_used):
                freed = await self.maybe_evict_for(needed)
                if not freed:
                    logger.debug(
                        "SkillScheduler: cannot load %s — budget exhausted", entry.skill_id
                    )
                    continue

        loaded = await self._loader.load_for_event(event_text, self._budget)
        return loaded

    async def evict_cold(self) -> int:
        """Evict all cold skills (use_count < hot_threshold AND
        last_used_at older than cold_ttl).

        Returns number of skills evicted.
        """
        if self._config.retention_policy == "manual":
            return 0

        cold_deadline = datetime.now(timezone.utc) - timedelta(
            seconds=self._config.cold_ttl_secs
        )
        evicted = 0
        for ls in self._lru_cold():
            if ls.last_used_at < cold_deadline:
                await self._loader.unload(ls.entry.skill_id)
                evicted += 1
                logger.debug(
                    "SkillScheduler: evicted cold skill %s (last_used=%s)",
                    ls.entry.skill_id,
                    ls.last_used_at.isoformat(),
                )

        if evicted:
            logger.info("SkillScheduler: evicted %d cold skill(s)", evicted)
        return evicted

    async def maybe_evict_for(self, needed_tokens: int) -> bool:
        """Free at least needed_tokens by evicting cold skills in LRU order.

        Returns True if sufficient space was freed.
        """
        if self._config.retention_policy == "manual":
            return False

        candidates = self._lru_cold()
        freed = 0
        for ls in candidates:
            if freed >= needed_tokens:
                break
            freed += ls.entry.context_tokens_estimate
            await self._loader.unload(ls.entry.skill_id)
            logger.debug(
                "SkillScheduler: LRU-evicted skill %s to make room (freed %d tokens)",
                ls.entry.skill_id,
                ls.entry.context_tokens_estimate,
            )

        return freed >= needed_tokens

    def _is_hot(self, skill: LoadedSkill) -> bool:
        return skill.use_count >= self._config.hot_skill_threshold

    def _lru_cold(self) -> List[LoadedSkill]:
        """Cold skills sorted oldest last_used_at first."""
        cold = [ls for ls in self._loader.all_loaded() if not self._is_hot(ls)]
        cold.sort(key=lambda ls: ls.last_used_at)
        return cold
