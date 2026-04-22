"""Colony Vector — auto-captioning for images.

Generates text captions for images using the host's LLM (if configured)
or falls back to EXIF/filename-based descriptions.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from colony_sidecar.vector.multimodal_types import ImageInput

logger = logging.getLogger(__name__)


async def caption_image(
    image: ImageInput,
    llm_config: Optional[dict[str, Any]] = None,
    max_tokens: int = 50,
) -> str:
    """Generate a caption for an image.

    Tries LLM-based captioning first (if credentials available),
    falls back to EXIF/metadata-based caption.
    """
    # Try LLM captioning
    if llm_config and llm_config.get("api_key"):
        try:
            caption = await _llm_caption(image, llm_config, max_tokens)
            if caption:
                return caption
        except Exception as exc:
            logger.warning("LLM captioning failed: %s — falling back to metadata", exc)

    # Fallback: metadata-based caption
    return _metadata_caption(image)


async def _llm_caption(
    image: ImageInput,
    llm_config: dict[str, Any],
    max_tokens: int,
) -> str:
    """Use a vision LLM to generate a caption."""
    import base64
    import httpx

    api_key = llm_config.get("api_key", "")
    base_url = llm_config.get("base_url", "https://open.bigmodel.cn/api/paas/v4")
    model = llm_config.get("vision_model", llm_config.get("model", "glm-4v-flash"))

    b64 = base64.b64encode(image.data).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"Describe this image in {max_tokens} characters or less. Be concise and factual."},
                            {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{b64}"}},
                        ],
                    }
                ],
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


def _metadata_caption(image: ImageInput) -> str:
    """Generate a basic caption from image metadata."""
    parts: list[str] = []

    # Use user-provided caption if available
    if image.caption:
        return image.caption

    # Dimensions
    if image.width and image.height:
        parts.append(f"{image.width}x{image.height}")

    # MIME type
    fmt = image.mime_type.split("/")[-1].upper() if image.mime_type else "Image"
    parts.insert(0, fmt)

    # EXIF data
    exif = image.exif
    if exif.get("captured_at"):
        parts.append(f"taken {exif['captured_at']}")
    if exif.get("camera_make") or exif.get("camera_model"):
        camera = f"{exif.get('camera_make', '')} {exif.get('camera_model', '')}".strip()
        parts.append(f"camera: {camera}")

    # GPS
    if exif.get("gps_lat") and exif.get("gps_lon"):
        parts.append(f"location: {exif['gps_lat']},{exif['gps_lon']}")

    # File path hint
    if image.original_path:
        from pathlib import Path
        name = Path(image.original_path).stem
        if name and not name.startswith("data:"):
            parts.insert(0, name.replace("-", " ").replace("_", " "))

    caption = " — ".join(parts) if parts else "Image"
    return caption[:200]  # Cap length
