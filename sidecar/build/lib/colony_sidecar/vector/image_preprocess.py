"""Colony Vector — image preprocessing, EXIF extraction, and thumbnail generation.

Handles all image input normalization: loading from paths, URLs, base64, or raw
bytes. Extracts EXIF metadata, generates thumbnails, and validates format/size.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_IMAGE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_IMAGE_DIMENSION = 4096
THUMBNAIL_SIZE = (128, 128)
THUMBNAIL_QUALITY = 75
SUPPORTED_FORMATS = {"jpeg", "jpg", "png", "webp", "gif"}

# MIME type mapping
MIME_MAP = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG": "image/png",
    b"RIFF": "image/webp",  # WebP starts with RIFF
    b"GIF8": "image/gif",
}


# ---------------------------------------------------------------------------
# Image validation
# ---------------------------------------------------------------------------


def detect_mime(data: bytes) -> str:
    """Detect MIME type from magic bytes."""
    for magic, mime in MIME_MAP.items():
        if data[:len(magic)] == magic:
            return mime
    return ""


def validate_image(data: bytes, max_size: int = MAX_IMAGE_SIZE_BYTES) -> list[str]:
    """Validate image data. Returns list of error messages (empty = valid)."""
    errors: list[str] = []
    if len(data) == 0:
        errors.append("Empty image data")
    if len(data) > max_size:
        errors.append(f"Image size {len(data)} exceeds limit {max_size}")
    mime = detect_mime(data)
    if not mime:
        errors.append("Unrecognized image format (must be JPEG, PNG, WebP, or GIF)")
    return errors


# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------


def _is_url(s: str) -> bool:
    try:
        result = urlparse(s)
        return result.scheme in ("http", "https")
    except Exception:
        return False


def _is_base64(s: str) -> bool:
    """Check if a string looks like base64-encoded data."""
    if s.startswith("data:"):
        return True
    if len(s) < 100:
        return False
    try:
        # Quick check — doesn't need to be perfect
        if len(s) % 4 == 0 and all(c in base64._urlsafe_b64alphabet.decode() or c == '=' for c in s[:100]):
            return True
    except Exception:
        pass
    return False


def _decode_base64(s: str) -> bytes:
    """Decode base64 image data, handling data URIs."""
    if s.startswith("data:"):
        # data:image/jpeg;base64,<data>
        _, _, encoded = s.partition(",")
        if not encoded:
            raise ValueError("Empty base64 data in data URI")
        return base64.b64decode(encoded)
    return base64.b64decode(s)


async def load_image(source: str | bytes, mime_type: str = "") -> tuple[bytes, str]:
    """Load image data from a path, URL, base64 string, or raw bytes.

    Returns (raw_bytes, mime_type).
    """
    import httpx

    if isinstance(source, bytes):
        mime = mime_type or detect_mime(source)
        return source, mime

    if not isinstance(source, str):
        raise ValueError(f"Unsupported image source type: {type(source)}")

    # URL
    if _is_url(source):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(source, follow_redirects=True)
            resp.raise_for_status()
            data = resp.content
            mime = mime_type or resp.headers.get("content-type", "").split(";")[0] or detect_mime(data)
            return data, mime

    # Base64
    if _is_base64(source):
        data = _decode_base64(source)
        mime = mime_type or detect_mime(data)
        return data, mime

    # File path
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Image file not found: {source}")
    data = path.read_bytes()
    mime = mime_type or _mime_from_extension(path) or detect_mime(data)
    return data, mime


def _mime_from_extension(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
    return mime_map.get(ext, "")


# ---------------------------------------------------------------------------
# EXIF extraction
# ---------------------------------------------------------------------------


def extract_exif(data: bytes) -> dict[str, Any]:
    """Extract EXIF metadata from image bytes.

    Uses Pillow when available for comprehensive extraction.
    Falls back to minimal JPEG parser for GPS and date only.
    """
    exif: dict[str, Any] = {}

    # Try Pillow first (comprehensive)
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS

        img = Image.open(io.BytesIO(data))
        raw_exif = img.getexif()
        if not raw_exif:
            return exif

        # Extract standard tags
        tag_map = {
            0x010F: "camera_make",
            0x0110: "camera_model",
            0x0132: "captured_at",
            0x829A: "exposure_time",
            0x920A: "focal_length",
            0x8827: "iso",
        }
        for tag_id, value in raw_exif.items():
            tag_name = TAGS.get(tag_id, tag_map.get(tag_id))
            if tag_name in tag_map.values():
                exif[tag_name] = str(value)
            elif tag_id in tag_map:
                exif[tag_map[tag_id]] = str(value)

        # Extract GPS
        gps_ifd = raw_exif.get_ifd(0x8825)  # GPS IFD tag
        if gps_ifd:
            gps_data = {}
            for tag_id, value in gps_ifd.items():
                tag_name = GPSTAGS.get(tag_id, str(tag_id))
                gps_data[tag_name] = value

            # Convert GPS coordinates to decimal
            lat = _gps_to_decimal(gps_data.get("GPSLatitude"), gps_data.get("GPSLatitudeRef"))
            lon = _gps_to_decimal(gps_data.get("GPSLongitude"), gps_data.get("GPSLongitudeRef"))
            if lat is not None:
                exif["gps_lat"] = lat
            if lon is not None:
                exif["gps_lon"] = lon

        return exif

    except ImportError:
        pass  # Fall through to minimal parser
    except Exception as exc:
        logger.debug("Pillow EXIF extraction failed: %s — trying minimal parser", exc)

    # Fallback: minimal JPEG EXIF parser (GPS + date only)
    if data[:2] != b"\xff\xd8":
        return exif  # Not JPEG

    try:
        i = 2
        while i < len(data) - 4:
            if data[i:i+2] == b"\xff\xe1":  # APP1 marker
                length = struct.unpack(">H", data[i+2:i+4])[0]
                exif_data = data[i+4:i+2+length]
                if exif_data[:6] == b"Exif\x00\x00":
                    exif = _parse_exif_tiff(exif_data[6:])
                break
            elif data[i:i+2] == b"\xff":
                length = struct.unpack(">H", data[i+2:i+4])[0]
                i += 2 + length
            else:
                break
    except Exception as exc:
        logger.debug("Minimal EXIF extraction failed: %s", exc)

    return exif


def _gps_to_decimal(coords, ref) -> Optional[float]:
    """Convert GPS coordinates (degrees, minutes, seconds) to decimal."""
    if not coords or len(coords) < 3:
        return None
    try:
        degrees = float(coords[0])
        minutes = float(coords[1])
        seconds = float(coords[2])
        decimal = degrees + minutes / 60.0 + seconds / 3600.0
        if ref in ("S", "W"):
            decimal = -decimal
        return round(decimal, 6)
    except (TypeError, ValueError, IndexError):
        return None


def _parse_exif_tiff(data: bytes) -> dict[str, Any]:
    """Parse TIFF header from EXIF data, extracting GPS and key fields."""
    result: dict[str, Any] = {}
    try:
        # Byte order
        if data[:2] == b"II":
            order = "<"
        elif data[:2] == b"MM":
            order = ">"
        else:
            return result

        # TIFF magic (42)
        magic = struct.unpack(order + "H", data[2:4])[0]
        if magic != 42:
            return result

        # IFD0 offset
        ifd0_offset = struct.unpack(order + "I", data[4:8])[0]

        # Parse IFD0 entries
        gps_tags = {0x0001: "gps_lat_ref", 0x0002: "gps_lat",
                    0x0003: "gps_lon_ref", 0x0004: "gps_lon"}
        date_tag = 0x0132  # DateTime

        num_entries = struct.unpack(order + "H", data[ifd0_offset:ifd0_offset+2])[0]
        for j in range(num_entries):
            entry_offset = ifd0_offset + 2 + j * 12
            if entry_offset + 12 > len(data):
                break
            tag = struct.unpack(order + "H", data[entry_offset:entry_offset+2])[0]
            fmt = struct.unpack(order + "H", data[entry_offset+2:entry_offset+4])[0]
            count = struct.unpack(order + "I", data[entry_offset+4:entry_offset+8])[0]
            value_offset = data[entry_offset+8:entry_offset+12]

            if tag in gps_tags:
                key = gps_tags[tag]
                if fmt == 2 and count <= 4:  # ASCII
                    result[key] = value_offset[:count].decode("ascii", errors="ignore").strip("\x00")
            elif tag == date_tag:
                if fmt == 2:
                    # Date string may be at offset
                    if count <= 4:
                        result["captured_at"] = value_offset[:count].decode("ascii", errors="ignore").strip("\x00")
                    else:
                        str_offset = struct.unpack(order + "I", value_offset)[0]
                        if str_offset + count <= len(data):
                            result["captured_at"] = data[str_offset:str_offset+count].decode("ascii", errors="ignore").strip("\x00")
    except Exception as exc:
        logger.debug("TIFF/EXIF parsing failed: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Resize / thumbnail
# ---------------------------------------------------------------------------


def resize_image(data: bytes, max_dim: int = MAX_IMAGE_DIMENSION) -> tuple[bytes, int, int]:
    """Resize image if it exceeds max_dim on either axis.

    Returns (resized_bytes, width, height). If no resize needed,
    returns original data with dimensions from PIL (or 0,0 if PIL unavailable).
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        w, h = img.size

        if max(w, h) <= max_dim:
            return data, w, h

        # Resize maintaining aspect ratio
        ratio = max_dim / max(w, h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)

        # Convert to RGB if necessary (for JPEG output)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue(), new_w, new_h

    except ImportError:
        logger.warning("Pillow not installed — cannot resize images")
        return data, 0, 0
    except Exception as exc:
        logger.warning("Image resize failed: %s", exc)
        return data, 0, 0


def generate_thumbnail(data: bytes, size: tuple[int, int] = THUMBNAIL_SIZE, quality: int = THUMBNAIL_QUALITY) -> bytes:
    """Generate a JPEG thumbnail from image data."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail(size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    except ImportError:
        logger.warning("Pillow not installed — cannot generate thumbnails")
        return b""
    except Exception as exc:
        logger.warning("Thumbnail generation failed: %s", exc)
        return b""


# ---------------------------------------------------------------------------
# Image hash and dedup
# ---------------------------------------------------------------------------


def compute_image_hash(data: bytes) -> str:
    """SHA-256 hash of raw image bytes for deduplication."""
    return hashlib.sha256(data).hexdigest()


def strip_gps_exif(data: bytes) -> bytes:
    """Strip GPS data from JPEG EXIF.

    Returns modified bytes, or original if not JPEG or stripping fails.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        img = Image.open(io.BytesIO(data))
        exif_data = img.getexif()
        if not exif_data:
            return data

        # Remove GPS IFD (tag 34853)
        gps_ifd = 34853
        if gps_ifd in exif_data:
            del exif_data[gps_ifd]

        # Save back
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90, exif=exif_data)
        return buf.getvalue()

    except ImportError:
        return data
    except Exception:
        return data
