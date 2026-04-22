"""Layer 5 — Injection detection on the incoming message. Deterministic + heuristic."""

from __future__ import annotations

import base64
import re
import unicodedata

from colony_sidecar.gate.layers.base import LayerResult

# ── Role override patterns ──────────────────────────────────────────────────
_ROLE_OVERRIDE_PATTERNS = [
    re.compile(
        r"(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+(instructions|directives|context)",
        re.IGNORECASE,
    ),
    re.compile(r"you\s+are\s+(now|actually|secretly|really)\s+[A-Z]", re.IGNORECASE),
    re.compile(
        r"(act|behave|respond)\s+as\s+(if\s+)?(you\s+are\s+|a\s+)?(DAN|GPT|jailbreak|unrestricted)",
        re.IGNORECASE,
    ),
    re.compile(r"(system\s+prompt|system\s+message|instructions)\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*(system|admin|root|operator)\s*>", re.IGNORECASE),
]

# ── Gateway redirect patterns ───────────────────────────────────────────────
_REDIRECT_PATTERNS = [
    re.compile(r"send\s+(this|it|the\s+following)\s+to\s+\S+@\S+", re.IGNORECASE),
    re.compile(r"forward\s+(this\s+message\s+)?to\s+", re.IGNORECASE),
    re.compile(r"reply\s+to\s+\S+\s+instead", re.IGNORECASE),
]

# ── Context extraction patterns ─────────────────────────────────────────────
_EXTRACTION_PATTERNS = [
    re.compile(
        r"(print|show|display|output|repeat|reveal)\s+(your\s+)?(system\s+prompt|instructions|memory|context)",
        re.IGNORECASE,
    ),
    re.compile(r"what\s+(are|were)\s+your\s+(original\s+)?instructions", re.IGNORECASE),
]

_ALL_BLOCK_PATTERNS = _ROLE_OVERRIDE_PATTERNS + _REDIRECT_PATTERNS + _EXTRACTION_PATTERNS

# ── Homoglyph Unicode ranges (commonly used Cyrillic/Greek lookalikes) ──────
# Check for non-ASCII chars in ranges that resemble ASCII letters
_HOMOGLYPH_RE = re.compile(
    r"[\u0400-\u04FF\u0370-\u03FF\uFF00-\uFFEF]"  # Cyrillic, Greek, fullwidth
)

# ── Invisible/control characters ────────────────────────────────────────────
_INVISIBLE_CHARS = frozenset([
    "\u200B",  # zero-width space
    "\u200C",  # zero-width non-joiner
    "\u200D",  # zero-width joiner
    "\uFEFF",  # BOM / zero-width no-break space
    "\u00AD",  # soft hyphen
    "\u2060",  # word joiner
    "\u180E",  # Mongolian vowel separator
])

# ── Imperative verbs for density check ─────────────────────────────────────
_IMPERATIVE_RE = re.compile(
    r"\b(ignore|disregard|forget|reveal|print|show|output|send|forward|override|"
    r"execute|run|bypass|dump|extract|list|enumerate|tell|report)\b",
    re.IGNORECASE,
)


def _check_base64_blocks(text: str) -> bool:
    """Detect base64-encoded blocks >= 32 chars and check decoded content."""
    b64_pattern = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")
    for match in b64_pattern.finditer(text):
        try:
            raw = match.group()
            # Correct padding: strip existing padding, then add exact padding needed
            stripped = raw.rstrip("=")
            padding = (4 - len(stripped) % 4) % 4
            padded = stripped + "=" * padding
            decoded_bytes = base64.b64decode(padded)
            # Scan raw bytes as latin-1 (no bytes dropped) to avoid evasion via
            # non-UTF-8 byte injection (SEC-14-H-03)
            decoded = decoded_bytes.decode("latin-1")
            for pattern in _ALL_BLOCK_PATTERNS:
                if pattern.search(decoded):
                    return True
        except Exception:
            pass
    return False


# Minimum absolute count: flag if >= 2 homoglyphs regardless of density
# to catch short injections where density math is misleading.
_HOMOGLYPH_DENSITY_THRESHOLD = 0.02
_HOMOGLYPH_MIN_COUNT = 2


def _check_homoglyphs(text: str) -> bool:
    """Detect Cyrillic/Greek/fullwidth homoglyph substitution.

    Flags if density > 2% OR if there are >= 2 absolute homoglyphs in a
    short string (to catch injections in short inputs where density is
    misleading). Threshold lowered from 5% (SEC-14-H-05).
    """
    if not text:
        return False
    homoglyph_count = len(_HOMOGLYPH_RE.findall(text))
    if homoglyph_count == 0:
        return False
    density = homoglyph_count / len(text)
    return density > _HOMOGLYPH_DENSITY_THRESHOLD or homoglyph_count >= _HOMOGLYPH_MIN_COUNT


def _check_invisible_chars(text: str) -> bool:
    """Detect invisible/control Unicode characters injected into text."""
    return any(c in _INVISIBLE_CHARS for c in text)


def _check_instruction_density(text: str) -> bool:
    """Return True if imperative density > 0.4 (suspicious flag only)."""
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) < 3:
        return False
    imperative_count = sum(1 for s in sentences if _IMPERATIVE_RE.search(s))
    return (imperative_count / len(sentences)) > 0.4


class InjectionDetector:
    """Layer 5 — Injection detection on the incoming message."""

    def __init__(self, config=None) -> None:
        self._config = config

    async def _scan_text(self, text: str) -> "LayerResult | None":
        """Run injection checks on a single text field.

        Returns a blocking/suspicious ``LayerResult`` if a finding is made,
        or ``None`` if the text passes all checks.
        """
        # 5.1 — Pattern matching (hard block)
        for pattern in _ALL_BLOCK_PATTERNS:
            match = pattern.search(text)
            if match:
                excerpt = text[max(0, match.start() - 10):match.end() + 10][:50]
                return LayerResult(
                    blocked=True,
                    code="block_injection",
                    reason="injection pattern detected",
                    flagged_excerpt=excerpt,
                )

        # 5.2 — Encoding tricks (hard block)
        if _check_base64_blocks(text):
            return LayerResult(
                blocked=True,
                code="block_injection",
                reason="base64-encoded injection content detected",
                flagged_excerpt="[encoded content]",
            )

        if _check_homoglyphs(text):
            return LayerResult(
                blocked=True,
                code="block_injection",
                reason="homoglyph substitution detected (density > 0.05)",
                flagged_excerpt="[homoglyph text]",
            )

        if _check_invisible_chars(text):
            return LayerResult(
                blocked=True,
                code="block_injection",
                reason="invisible/control characters detected in message",
                flagged_excerpt="[invisible characters]",
            )

        # 5.3 — Instruction density (suspicious flag only — forwarded to L6)
        sensitivity = getattr(self._config, "sensitivity", "standard") if self._config else "standard"
        if sensitivity != "relaxed" and _check_instruction_density(text):
            return LayerResult(
                blocked=False,
                code="pass",
                suspicious=True,
                reason="high imperative density (INJECTION_SUSPICIOUS)",
            )

        return None

    async def check(self, payload) -> LayerResult:
        # Scan both inbound and outbound text so that injection payloads embedded
        # in AI-generated responses are caught before reaching downstream consumers.
        for text in (payload.incoming_message_text or "", payload.response_text or ""):
            if not text:
                continue
            result = await self._scan_text(text)
            if result is not None:
                return result

        return LayerResult(blocked=False, code="pass")
