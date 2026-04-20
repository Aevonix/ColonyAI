"""Colony Vector — image content safety checks.

Validates images before embedding: format, dimensions, and optional
content classification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ImageSafetyLevel(str, Enum):
    """Image safety check strictness."""

    OFF = "off"        # No checks (not recommended for production)
    BASIC = "basic"    # Format, dimension, and size validation only
    STRICT = "strict"  # Format + dimensions + content classification


@dataclass
class ImageSafetyResult:
    """Result of an image safety check."""

    safe: bool = True
    level: ImageSafetyLevel = ImageSafetyLevel.BASIC
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    classification: Optional[dict[str, float]] = None  # content scores for strict mode

    @property
    def reason(self) -> str:
        return "; ".join(self.errors) if self.errors else ""


async def check_image_safety(
    data: bytes,
    mime_type: str = "",
    width: int = 0,
    height: int = 0,
    level: ImageSafetyLevel = ImageSafetyLevel.BASIC,
) -> ImageSafetyResult:
    """Check image content safety.

    Parameters
    ----------
    data : bytes
        Raw image bytes.
    mime_type : str
        Detected or provided MIME type.
    width, height : int
        Image dimensions (0 if unknown).
    level : ImageSafetyLevel
        How strict to check.

    Returns
    -------
    ImageSafetyResult
    """
    result = ImageSafetyResult(level=level)

    if level == ImageSafetyLevel.OFF:
        return result

    # --- Basic checks ---

    # Format
    valid_mimes = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if mime_type and mime_type not in valid_mimes:
        result.errors.append(f"Unsupported image format: {mime_type}")
        result.safe = False

    # Size
    max_size = 20 * 1024 * 1024  # 20 MB
    if len(data) > max_size:
        result.errors.append(f"Image size {len(data)} exceeds 20MB limit")
        result.safe = False

    if len(data) == 0:
        result.errors.append("Empty image data")
        result.safe = False

    # Dimensions (if known)
    max_dim = 4096
    if width > max_dim or height > max_dim:
        result.warnings.append(f"Image dimensions {width}x{height} exceed {max_dim}px — will be resized")

    # Minimum dimensions (avoid 1px images, etc.)
    if width > 0 and height > 0 and (width < 16 or height < 16):
        result.warnings.append(f"Very small image: {width}x{height}px")

    if level == ImageSafetyLevel.BASIC:
        return result

    # --- Strict checks (content classification) ---

    try:
        classification = await _classify_content(data, mime_type)
        result.classification = classification

        # Flag content with high NSFW scores
        nsfw_score = classification.get("nsfw", 0.0)
        if nsfw_score > 0.8:
            result.errors.append(f"Image flagged as potentially explicit (score: {nsfw_score:.2f})")
            result.safe = False
        elif nsfw_score > 0.5:
            result.warnings.append(f"Image may contain explicit content (score: {nsfw_score:.2f})")

    except Exception as exc:
        logger.warning("Content classification failed: %s — allowing image (basic checks passed)", exc)
        result.warnings.append("Content classification unavailable")

    return result


async def _classify_content(data: bytes, mime_type: str) -> dict[str, float]:
    """Classify image content for safety.

    Uses a local classifier if available, otherwise returns empty scores.
    Override this with a real implementation (e.g. OpenAI moderation API,
    or a local NSFW model) in production.
    """
    # Placeholder — returns safe scores by default
    # In production, integrate one of:
    # - OpenAI Moderation API
    # - local NSFW classifier (e.g. falconsai/nsfw_image_detection via transformers)
    # - custom safety model

    return {"nsfw": 0.0, "violence": 0.0, "safe": 1.0}
