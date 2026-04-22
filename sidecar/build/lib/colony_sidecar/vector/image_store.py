"""Colony Vector — local image storage.

Stores images on the local filesystem with dedup by hash,
thumbnail generation, and metadata tracking.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from colony_sidecar.vector.multimodal_types import ImageInput

logger = logging.getLogger(__name__)


class ImageStorageMode(str):
    LOCAL = "local"
    EMBED_ONLY = "embed_only"


@dataclass
class StoredImage:
    """Reference to a stored image."""

    image_hash: str
    path: str          # Full path to stored image
    thumbnail_path: str  # Full path to thumbnail
    mime_type: str
    width: int
    height: int
    size_bytes: int


class LocalImageStore:
    """Store images on the local filesystem.

    Directory layout:
        $COLONY_STATE_DIR/images/
            originals/{hash}.jpg
            thumbs/{hash}.jpg
    """

    def __init__(self, state_dir: Optional[str] = None) -> None:
        base = Path(state_dir or os.environ.get("COLONY_STATE_DIR", "."))
        self._base_dir = base / "images"
        self._originals_dir = self._base_dir / "originals"
        self._thumbs_dir = self._base_dir / "thumbs"

    def _ensure_dirs(self) -> None:
        self._originals_dir.mkdir(parents=True, exist_ok=True)
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)

    def _original_path(self, image_hash: str, mime_type: str) -> Path:
        ext = "jpg" if "jpeg" in mime_type else "png"
        return self._originals_dir / f"{image_hash}.{ext}"

    def _thumbnail_path(self, image_hash: str) -> Path:
        return self._thumbs_dir / f"{image_hash}.jpg"

    async def store(self, image: ImageInput) -> StoredImage:
        """Store an image and its thumbnail. Returns reference info.

        If image already exists (same hash), skips writing but returns reference.
        """
        self._ensure_dirs()

        original_path = self._original_path(image.image_hash, image.mime_type)
        thumbnail_path = self._thumbnail_path(image.image_hash)

        # Store original (skip if already exists — dedup)
        if not original_path.exists():
            original_path.write_bytes(image.data)

        # Generate and store thumbnail
        if not thumbnail_path.exists():
            from colony_sidecar.vector.image_preprocess import generate_thumbnail
            thumb_data = generate_thumbnail(image.data)
            if thumb_data:
                thumbnail_path.write_bytes(thumb_data)

        return StoredImage(
            image_hash=image.image_hash,
            path=str(original_path),
            thumbnail_path=str(thumbnail_path) if thumbnail_path.exists() else "",
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            size_bytes=len(image.data),
        )

    async def exists(self, image_hash: str) -> bool:
        """Check if an image with this hash is already stored."""
        # Check all possible extensions
        for ext in ("jpg", "png", "jpeg", "webp", "gif"):
            if (self._originals_dir / f"{image_hash}.{ext}").exists():
                return True
        return False

    async def get(self, image_hash: str) -> Optional[bytes]:
        """Retrieve original image bytes by hash."""
        for ext in ("jpg", "png", "jpeg", "webp", "gif"):
            path = self._originals_dir / f"{image_hash}.{ext}"
            if path.exists():
                return path.read_bytes()
        return None

    async def get_thumbnail(self, image_hash: str) -> Optional[bytes]:
        """Retrieve thumbnail bytes by hash."""
        path = self._thumbnail_path(image_hash)
        if path.exists():
            return path.read_bytes()
        return None

    async def delete(self, image_hash: str) -> bool:
        """Delete an image and its thumbnail."""
        deleted = False
        for ext in ("jpg", "png", "jpeg", "webp", "gif"):
            path = self._originals_dir / f"{image_hash}.{ext}"
            if path.exists():
                path.unlink()
                deleted = True
        thumb = self._thumbnail_path(image_hash)
        if thumb.exists():
            thumb.unlink()
            deleted = True
        return deleted

    async def count(self) -> int:
        """Count stored images."""
        if not self._originals_dir.exists():
            return 0
        return len(list(self._originals_dir.iterdir()))

    async def total_size_bytes(self) -> int:
        """Total storage used by originals + thumbnails."""
        total = 0
        for d in (self._originals_dir, self._thumbs_dir):
            if d.exists():
                for f in d.iterdir():
                    if f.is_file():
                        total += f.stat().st_size
        return total


class EmbedOnlyStore:
    """No-op store — images are embedded but not persisted."""

    async def store(self, image: ImageInput) -> StoredImage:
        return StoredImage(
            image_hash=image.image_hash,
            path="",
            thumbnail_path="",
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            size_bytes=len(image.data),
        )

    async def exists(self, image_hash: str) -> bool:
        return False

    async def get(self, image_hash: str) -> Optional[bytes]:
        return None

    async def get_thumbnail(self, image_hash: str) -> Optional[bytes]:
        return None

    async def delete(self, image_hash: str) -> bool:
        return False

    async def count(self) -> int:
        return 0

    async def total_size_bytes(self) -> int:
        return 0


def make_image_store(mode: str = "", state_dir: Optional[str] = None):
    """Create an image store based on config."""
    mode = mode or os.environ.get("COLONY_IMAGE_STORAGE", "local")

    if mode == ImageStorageMode.EMBED_ONLY:
        return EmbedOnlyStore()
    else:
        return LocalImageStore(state_dir=state_dir)
