"""Dependency-free final-answer boundary for consumers that persist output."""
from __future__ import annotations


def final_text(response) -> str:
    """Never persist the router's reasoning fallback or a truncated completion.

The router retains its interactive compatibility behavior. Structured consumers
use the provider's final content whenever the raw completion is available.
Adapters without a raw provider envelope supply their own final content.
"""
    raw = getattr(response, "raw", None)
    if raw is not None:
        choices = getattr(raw, "choices", None)
        if not choices:
            raise ValueError("missing_final_answer")
        choice = choices[0]
        if getattr(choice, "finish_reason", None) not in {None, "stop"}:
            raise ValueError("incomplete_final_answer")
        content = getattr(getattr(choice, "message", None), "content", None)
    else:
        content = getattr(response, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("missing_final_answer")
    return content.strip()
