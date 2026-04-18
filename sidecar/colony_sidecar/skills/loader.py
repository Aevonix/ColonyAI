"""Colony Skills — on-demand skill module loader."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import logging
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from colony_sidecar.skills.budget import ContextBudget
from colony_sidecar.skills.index import SkillEntry, SkillIndex

logger = logging.getLogger(__name__)


class SecurityError(Exception):
    """Raised when a skill integrity or security check fails."""


class SchemaError(Exception):
    """Raised when a skill entry is missing required schema fields."""


# Only modules under these prefixes may be loaded via lazy_loader
_ALLOWED_LAZY_LOADER_PREFIXES = frozenset({"colony.skills.", "colony.plugins."})


def _verify_skill_checksum(skill_path: Path, expected_sha256: str) -> bytes:
    """Verify SHA-256 of skill_path matches expected_sha256 before execution.

    Returns the file bytes so the caller can compile from memory without a
    second disk read, eliminating the TOCTOU window (SEC-14-C-03).

    Raises:
        SecurityError: If checksum is missing or does not match.
    """
    if not expected_sha256:
        raise SecurityError(
            "SkillEntry has no checksum — cannot load unverified skill"
        )
    file_bytes = skill_path.read_bytes()
    actual = hashlib.sha256(file_bytes).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise SecurityError(
            f"Skill checksum mismatch for {skill_path}: "
            f"expected {expected_sha256!r}, got {actual!r}"
        )
    return file_bytes


@dataclass
class LoadedSkill:
    """A skill that has been imported and is resident in memory."""

    entry: SkillEntry
    module: types.ModuleType
    loaded_at: datetime
    last_used_at: datetime
    use_count: int = 0


class SkillLoader:
    """Imports and initializes skill modules on demand.

    Maintains a loaded-skill cache keyed by skill_id.
    """

    def __init__(self, index: SkillIndex) -> None:
        self._index = index
        self._loaded: Dict[str, LoadedSkill] = {}
        self._lock = asyncio.Lock()

    async def load(self, skill_id: str) -> Optional[LoadedSkill]:
        """Import the skill module if not already loaded.

        Records load timestamp for LRU eviction.
        Returns None if the skill is not in the index.
        """
        async with self._lock:
            if skill_id in self._loaded:
                ls = self._loaded[skill_id]
                ls.last_used_at = datetime.now(timezone.utc)
                ls.use_count += 1
                return ls

            entry = self._index.get(skill_id)
            if entry is None:
                logger.warning("SkillLoader.load: skill %r not in index", skill_id)
                return None

            module = await self._import_module(entry)
            if module is None:
                return None

            # Call initialize() hook if present
            init_fn = getattr(module, "initialize", None)
            if init_fn is not None:
                try:
                    if asyncio.iscoroutinefunction(init_fn):
                        await init_fn()
                    else:
                        init_fn()
                except Exception as exc:
                    logger.warning("Skill %s initialize() failed: %s", skill_id, exc)

            now = datetime.now(timezone.utc)
            ls = LoadedSkill(
                entry=entry,
                module=module,
                loaded_at=now,
                last_used_at=now,
                use_count=1,
            )
            self._loaded[skill_id] = ls
            logger.debug("SkillLoader: loaded skill %s", skill_id)
            return ls

    async def unload(self, skill_id: str) -> None:
        """Remove skill from loaded cache.

        Does NOT clean up sys.modules (Python limitation), but clears the
        in-context reference so the agent no longer sees it.
        """
        async with self._lock:
            self._loaded.pop(skill_id, None)
        logger.debug("SkillLoader: unloaded skill %s", skill_id)

    async def load_for_event(
        self, text: str, budget: ContextBudget
    ) -> List[LoadedSkill]:
        """Match text against index, load matched skills within budget.

        Returns list of all loaded skills relevant to the event (including
        already-loaded ones). Already-loaded skills do not consume budget again.
        """
        candidates = self._index.match(text)
        result: List[LoadedSkill] = []
        current_used = self.token_footprint()

        for entry in candidates:
            if entry.skill_id in self._loaded:
                ls = self._loaded[entry.skill_id]
                ls.last_used_at = datetime.now(timezone.utc)
                ls.use_count += 1
                result.append(ls)
            elif budget.has_capacity(entry.context_tokens_estimate, current_used):
                ls = await self.load(entry.skill_id)
                if ls is not None:
                    result.append(ls)
                    current_used += entry.context_tokens_estimate
            else:
                logger.debug(
                    "SkillLoader: budget full, skipping skill %s (needs %d, available %d)",
                    entry.skill_id,
                    entry.context_tokens_estimate,
                    budget.tokens_available(current_used),
                )

        return result

    def loaded_ids(self) -> List[str]:
        """Return skill_ids currently in the loaded cache."""
        return list(self._loaded.keys())

    def token_footprint(self) -> int:
        """Sum of context_tokens_estimate for all loaded skills."""
        return sum(ls.entry.context_tokens_estimate for ls in self._loaded.values())

    def get_loaded(self, skill_id: str) -> Optional[LoadedSkill]:
        """Return a loaded skill by ID, or None."""
        return self._loaded.get(skill_id)

    def all_loaded(self) -> List[LoadedSkill]:
        """Return all currently loaded skills."""
        return list(self._loaded.values())

    @staticmethod
    async def _import_module(entry: SkillEntry) -> Optional[types.ModuleType]:
        """Import skill module using lazy_loader or default file-based import."""
        if entry.lazy_loader:
            # Dotted import path: "some.module:factory_fn" — allowlist enforced
            try:
                parts = entry.lazy_loader.split(":", 1)
                mod_path = parts[0]
                fn_name = parts[1] if len(parts) > 1 else None

                if not any(mod_path.startswith(p) for p in _ALLOWED_LAZY_LOADER_PREFIXES):
                    raise SecurityError(
                        f"Disallowed lazy_loader module: {mod_path!r}"
                    )
                if fn_name is not None and not fn_name.isidentifier():
                    raise SecurityError(
                        f"Invalid function name in lazy_loader: {fn_name!r}"
                    )

                mod = importlib.import_module(mod_path)
                if fn_name:
                    fn = getattr(mod, fn_name)
                    result = fn(entry)
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                return mod
            except SecurityError:
                raise
            except Exception as exc:
                logger.error(
                    "Skill %s: lazy_loader %r failed: %s", entry.skill_id, entry.lazy_loader, exc
                )
                return None

        # Default: load skill.py from skill_dir
        skill_path = entry.skill_dir / "skill.py"
        if not skill_path.exists():
            logger.warning("Skill %s: no skill.py at %s", entry.skill_id, skill_path)
            return None

        try:
            # Read once, verify checksum, compile from memory — no second disk read
            # eliminates the TOCTOU window (SEC-14-C-03)
            if not hasattr(entry, "checksum_sha256"):
                raise SchemaError(
                    f"SkillEntry for {entry.skill_id!r} is missing required "
                    f"'checksum_sha256' field — refusing to load unverified skill"
                )
            file_bytes = _verify_skill_checksum(skill_path, entry.checksum_sha256)

            # Compile and execute from the already-verified in-memory bytes
            code = compile(file_bytes, str(skill_path), "exec")
            module = types.ModuleType(f"_skill_{entry.skill_id}")
            module.__file__ = str(skill_path)
            module.__spec__ = None
            exec(code, module.__dict__)  # noqa: S102  # nosec B102  # bytes already verified above
            return module
        except (SecurityError, SchemaError):
            raise
        except Exception as exc:
            logger.error("Skill %s: import from %s failed: %s", entry.skill_id, skill_path, exc)
            return None
