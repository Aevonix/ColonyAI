"""Structure-aware text chunking.

Splits text along natural boundaries — fenced code blocks stay atomic,
then markdown headings, then blank-line paragraphs — and greedily packs
blocks into chunks near a target token size, with configurable overlap
between consecutive chunks so facts straddling a boundary survive
retrieval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from colony_sidecar.contextgate.estimate import estimate_tokens

__all__ = ["Chunk", "chunk_text"]

_FENCE_RE = re.compile(r"^(```|~~~)", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


@dataclass
class Chunk:
    """One packed chunk of a source text."""

    index: int          # 0-based position in document order
    text: str           # chunk content (includes any overlap prefix)
    start: int          # char offset of the core (non-overlap) content
    end: int            # char offset one past the core content
    tokens: int = 0     # estimated tokens of ``text``


def _split_blocks(text: str) -> list[tuple[int, str]]:
    """Split *text* into (offset, block) pairs along structural boundaries.

    Fenced code blocks are kept whole. Outside fences, headings start new
    blocks and blank lines separate paragraphs.
    """
    blocks: list[tuple[int, str]] = []
    pos = 0
    n = len(text)

    while pos < n:
        fence = _FENCE_RE.search(text, pos)
        prose_end = fence.start() if fence else n

        # Prose region: split on blank lines and headings
        prose = text[pos:prose_end]
        if prose.strip():
            offset = pos
            # Insert split points before headings so each heading opens a block
            paragraphs = re.split(r"(\n\s*\n)", prose)
            cursor = 0
            for part in paragraphs:
                if part.strip() and not re.fullmatch(r"\n\s*\n", part):
                    # Further split on headings inside the paragraph run
                    last = 0
                    for m in _HEADING_RE.finditer(part):
                        if m.start() > last and part[last:m.start()].strip():
                            blocks.append((offset + cursor + last, part[last:m.start()]))
                        last = m.start()
                    if part[last:].strip():
                        blocks.append((offset + cursor + last, part[last:]))
                cursor += len(part)

        if fence is None:
            break

        # Fenced block: find the closing fence of the same kind
        marker = fence.group(1)
        close = text.find("\n" + marker, fence.end())
        if close == -1:
            blocks.append((fence.start(), text[fence.start():]))
            break
        # Include through the end of the closing-fence line
        fend = text.find("\n", close + 1 + len(marker))
        fend = n if fend == -1 else fend + 1
        blocks.append((fence.start(), text[fence.start():fend]))
        pos = fend

    return blocks


def _hard_split(offset: int, block: str, target_tokens: int) -> list[tuple[int, str]]:
    """Split an oversized block on sentence/newline boundaries, then windows."""
    limit_chars = max(1, target_tokens * 4)
    pieces: list[tuple[int, str]] = []
    cursor = 0
    n = len(block)
    while cursor < n:
        end = min(cursor + limit_chars, n)
        if end < n:
            # Back up to the last sentence end or newline in the window
            window = block[cursor:end]
            cut = max(window.rfind(". "), window.rfind("\n"))
            if cut > limit_chars // 4:
                end = cursor + cut + 1
        pieces.append((offset + cursor, block[cursor:end]))
        cursor = end
    return pieces


def chunk_text(
    text: str,
    target_tokens: int = 1024,
    overlap_tokens: int = 128,
) -> list[Chunk]:
    """Chunk *text* into ~*target_tokens* pieces along structural boundaries.

    Consecutive chunks share an overlap of roughly *overlap_tokens* (the
    tail of the previous chunk is prepended to the next), so information
    spanning a boundary remains retrievable. ``start``/``end`` offsets
    always refer to the core (non-overlap) content.
    """
    if not text.strip():
        return []

    blocks: list[tuple[int, str]] = []
    for offset, block in _split_blocks(text):
        if estimate_tokens(block) > target_tokens:
            blocks.extend(_hard_split(offset, block, target_tokens))
        else:
            blocks.append((offset, block))

    chunks: list[Chunk] = []
    cur_parts: list[tuple[int, str]] = []
    cur_tokens = 0

    def _flush() -> None:
        nonlocal cur_parts, cur_tokens
        if not cur_parts:
            return
        start = cur_parts[0][0]
        last_off, last_text = cur_parts[-1]
        end = last_off + len(last_text)
        core = text[start:end]
        prefix = ""
        if chunks and overlap_tokens > 0:
            prev = chunks[-1]
            tail_chars = overlap_tokens * 4
            tail = text[max(prev.start, prev.end - tail_chars):prev.end]
            # Cut at a word boundary so the overlap reads cleanly
            sp = tail.find(" ")
            if 0 <= sp < len(tail) - 1:
                tail = tail[sp + 1:]
            prefix = tail + "\n"
        body = prefix + core
        chunks.append(
            Chunk(
                index=len(chunks),
                text=body,
                start=start,
                end=end,
                tokens=estimate_tokens(body),
            )
        )
        cur_parts = []
        cur_tokens = 0

    for offset, block in blocks:
        btok = estimate_tokens(block)
        if cur_parts and cur_tokens + btok > target_tokens:
            _flush()
        cur_parts.append((offset, block))
        cur_tokens += btok
    _flush()

    return chunks
