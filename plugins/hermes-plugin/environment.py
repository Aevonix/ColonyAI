"""Normalize public environment names at process and instance load boundaries.

Stored configuration remains unchanged. Internal COLONY_* readers receive
explicit APSIMO_* values; conflicting explicit aliases are configuration errors.
"""
from __future__ import annotations

from collections.abc import Mapping
import os

_generated: dict[str, str] = {}


def normalize_environment(values: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a normalized copy, with no mutation or secret values in errors."""
    result = dict(os.environ if values is None else values)
    for name, value in tuple(result.items()):
        if not name.startswith('APSIMO_'):
            continue
        legacy = 'COLONY_' + name[len('APSIMO_'):]
        if legacy in result and result[legacy] != value:
            raise ValueError(f'Conflicting environment names: {name} and {legacy}')
        result[legacy] = value
    return result


def clear_environment_aliases() -> None:
    """Remove only aliases this module generated, before selecting an instance."""
    for name, value in _generated.items():
        if os.environ.get(name) == value:
            os.environ.pop(name, None)
    _generated.clear()


def apply_environment_aliases() -> None:
    """Refresh generated aliases; removed canonical keys cannot leave stale reads."""
    clear_environment_aliases()
    original = dict(os.environ)
    normalized = normalize_environment(original)
    for name, value in normalized.items():
        if name not in original:
            os.environ[name] = value
            _generated[name] = value
