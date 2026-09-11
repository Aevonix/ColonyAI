"""Base LayerResult dataclass shared by all gate layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class LayerResult:
    blocked: bool
    code: str
    reason: Optional[str] = None
    flagged_excerpt: Optional[str] = None
    suspicious: bool = False
