"""Colony Vector — multimodal embedding types and input definitions.

Modality-agnostic design: supports text, image, and future modalities
(audio, video) without hardcoding assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Union


class Modality(str, Enum):
    """Supported input modalities for embedding."""

    TEXT = "text"
    IMAGE = "image"
    # Future: AUDIO = "audio", VIDEO = "video"


@dataclass
class EmbedInput:
    """A single embedding input — text, image, or future modalities.

    The `content` field interpretation depends on `modality`:
    - text: raw text string
    - image: file path, URL, or base64-encoded string
    """

    modality: Modality = Modality.TEXT
    content: str = ""
    mime_type: str = ""  # e.g. "image/jpeg", "image/png" — auto-detected if empty
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "modality": self.modality.value,
            "content": self.content[:200] + "..." if len(self.content) > 200 else self.content,
            "mime_type": self.mime_type,
            "metadata": self.metadata,
        }


@dataclass
class ImageInput:
    """Normalized image input for the embedding pipeline.

    Constructed by image_preprocess from raw inputs (path, URL, bytes, base64).
    """

    data: bytes  # Raw image bytes (JPEG or PNG)
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0
    original_path: str = ""  # Source path/URL for reference
    image_hash: str = ""     # SHA-256 of raw bytes
    exif: dict[str, Any] = field(default_factory=dict)  # Extracted EXIF data
    caption: str = ""        # User-provided or auto-generated caption


@dataclass
class EmbedResult:
    """Result of embedding a single input."""

    vector: list[float]
    modality: Modality
    model_id: str
    dims: int
    image_hash: str = ""     # Set for image inputs
    caption: str = ""        # Set for image inputs
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MultimodalSearchResult:
    """Search result with modality awareness."""

    id: str
    score: float
    text: str
    modality: Modality = Modality.TEXT
    image_ref: str = ""
    image_hash: str = ""
    caption: str = ""
    thumbnail_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
