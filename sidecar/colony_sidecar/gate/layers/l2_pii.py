"""Layer 2 — PII and credential scan. Fully deterministic."""

from __future__ import annotations

import math
import re
from typing import Optional

from colony_sidecar.gate.layers.base import LayerResult

# Reuse patterns from agent/redact.py
from colony_sidecar.redact import (
    _PREFIX_RE,
    _ENV_ASSIGN_RE,
    _JSON_FIELD_RE,
    _AUTH_HEADER_RE,
    _PRIVATE_KEY_RE,
    _SIGNAL_PHONE_RE,
)

# Additional outbound-message patterns
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_CARD_CONTEXT_RE = re.compile(
    r"\b(card|cc|credit|debit|visa|mastercard|amex|payment|pan|cvv|cvc)\b",
    re.IGNORECASE,
)


def _has_card_context(text: str, match_start: int, match_end: int, window: int = 50) -> bool:
    """Return True if a card-context keyword appears within `window` chars of the match."""
    lo = max(0, match_start - window)
    hi = min(len(text), match_end + window)
    return bool(_CARD_CONTEXT_RE.search(text[lo:hi]))
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}(?:[A-Z0-9]?){0,16}\b")

_OUTBOUND_PATTERNS = [
    ("ssn", _SSN_RE),
    ("credit_card", _CARD_RE),
    ("email", _EMAIL_RE),
    ("iban", _IBAN_RE),
    ("api_key", _PREFIX_RE),
    ("env_secret", _ENV_ASSIGN_RE),
    ("json_secret", _JSON_FIELD_RE),
    ("auth_header", _AUTH_HEADER_RE),
    ("private_key", _PRIVATE_KEY_RE),
    ("phone", _SIGNAL_PHONE_RE),
]


def _luhn_valid(number_str: str) -> bool:
    digits = [int(d) for d in number_str if d.isdigit()]
    if len(digits) < 13:
        return False
    odd_digits = digits[-1::-2]
    even_digits = digits[-2::-2]
    total = sum(odd_digits)
    for d in even_digits:
        total += sum(divmod(d * 2, 10))
    return total % 10 == 0


class PIIScanner:
    """Layer 2 — PII and credential scan. Fully deterministic."""

    def __init__(self, config) -> None:
        self._config = config
        # Compile custom PII patterns from config
        self._custom_patterns = []
        for cp in getattr(config, "custom_pii_patterns", []):
            try:
                compiled = re.compile(cp["pattern"])
                self._custom_patterns.append((cp["name"], compiled))
            except (KeyError, re.error):
                pass

    async def check(self, payload) -> LayerResult:
        text = payload.response_text
        all_patterns = list(_OUTBOUND_PATTERNS) + self._custom_patterns

        for pattern_name, pattern in all_patterns:
            match = pattern.search(text)
            if match:
                # Credit card: only block if Luhn-valid and in card context (reduces false positives)
                if pattern_name == "credit_card":
                    digits = re.sub(r"[ -]", "", match.group())
                    if not _luhn_valid(digits):
                        continue
                    if not _has_card_context(text, match.start(), match.end()):
                        continue

                # Owner-shareable exception
                if self._config.is_owner_shareable(match.group(), pattern_name):
                    continue

                # Code-context entropy check in RELAXED mode
                if (
                    self._config.sensitivity == "relaxed"
                    and self._is_dummy_in_code_block(text, match)
                ):
                    continue

                excerpt = self._redact_excerpt(match.group())
                return LayerResult(
                    blocked=True,
                    code="block_pii",
                    reason=f"pattern match: {pattern_name}",
                    flagged_excerpt=excerpt,
                )

        return LayerResult(blocked=False, code="pass")

    def _redact_excerpt(self, raw: str) -> str:
        if len(raw) <= 4:
            return "***"
        return raw[:2] + "***" + raw[-2:]

    def _is_dummy_in_code_block(self, text: str, match: re.Match) -> bool:
        """Heuristic: is this match inside a code fence with low entropy?"""
        start = match.start()
        preceding = text[max(0, start - 200):start]
        if "```" in preceding or "`" in preceding:
            token = match.group()
            if not token:
                return False
            freq: dict = {}
            for c in token:
                freq[c] = freq.get(c, 0) + 1
            entropy = -sum(
                (v / len(token)) * math.log2(v / len(token)) for v in freq.values()
            )
            return entropy < 2.5  # low-entropy dummy tokens
        return False
