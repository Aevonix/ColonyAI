"""Channel manifest -- what a channel declares about itself on registration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChannelManifest(BaseModel):
    """Describes a channel's capabilities, identity policies, and delivery config."""

    channel_key: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=256)
    gateway_family: str = Field(..., min_length=1, max_length=64)

    supports_media: bool = False
    supports_reactions: bool = False
    supports_voice: bool = False
    supports_rich_text: bool = False
    max_message_length: int | None = None

    phone_identity_unification: bool = False
    session_isolation: bool = False
    provides_channel_id: bool = False

    delivery_webhook: str | None = None
    delivery_protocol: str = "hermes"
    delivery_aliases: list[str] = Field(default_factory=list)
    home_chat_id: str | None = None

    platform_hint: str = ""
    pii_safe: bool = True
