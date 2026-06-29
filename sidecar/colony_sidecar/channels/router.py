"""Channel registration API -- /v1/channels/ endpoints."""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from colony_sidecar.channels.manifest import ChannelManifest
from colony_sidecar.channels.store import ChannelStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/channels", tags=["channels"])

_channel_store: Optional[ChannelStore] = None


def set_channel_store(store: ChannelStore) -> None:
    global _channel_store
    _channel_store = store


def get_channel_store() -> Optional[ChannelStore]:
    return _channel_store


def _require_store() -> ChannelStore:
    if _channel_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Channel store not initialized",
        )
    return _channel_store


# ── Request / Response models ────────────────────────────────────────────


class RegisterResponse(BaseModel):
    channel_key: str
    registered_at: str
    channel_token: str


class ChannelInfo(BaseModel):
    channel_key: str
    display_name: str
    gateway_family: str
    status: str
    registered_at: str
    last_seen_at: Optional[str] = None
    supports_media: bool = False
    supports_reactions: bool = False
    supports_voice: bool = False
    supports_rich_text: bool = False
    max_message_length: Optional[int] = None
    phone_identity_unification: bool = False
    provides_channel_id: bool = False
    delivery_protocol: str = "hermes"
    delivery_aliases: list[str] = []
    home_chat_id: Optional[str] = None


# ── Webhook validation ───────────────────────────────────────────────────


_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fd00::/8"),
    ipaddress.ip_network("fe80::/10"),
]


def _validate_webhook(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Webhook URL must be http or https, got '{parsed.scheme}'",
        )
    if not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Webhook URL has no hostname",
        )

    allow_private = os.environ.get(
        "COLONY_ALLOW_PRIVATE_WEBHOOKS", ""
    ).lower() in ("true", "1", "yes")

    if not allow_private:
        try:
            addr = ipaddress.ip_address(parsed.hostname)
            if any(addr in net for net in _PRIVATE_NETWORKS):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Webhook URL resolves to private address {addr}. "
                        "Set COLONY_ALLOW_PRIVATE_WEBHOOKS=true to allow."
                    ),
                )
        except ValueError:
            pass


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_channel(
    manifest: ChannelManifest,
    x_channel_token: Optional[str] = Header(None),
) -> RegisterResponse:
    store = _require_store()

    if manifest.delivery_webhook:
        _validate_webhook(manifest.delivery_webhook)

    try:
        registered = store.register(manifest, channel_token=x_channel_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )

    return RegisterResponse(
        channel_key=registered.channel_key,
        registered_at=registered.registered_at,
        channel_token=registered.channel_token,
    )


@router.get("", response_model=list[ChannelInfo])
async def list_channels() -> list[ChannelInfo]:
    store = _require_store()
    return [_to_info(ch) for ch in store.list_all()]


@router.get("/{channel_key}", response_model=ChannelInfo)
async def get_channel(channel_key: str) -> ChannelInfo:
    store = _require_store()
    ch = store.get(channel_key)
    if ch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_key}' not found",
        )
    return _to_info(ch)


@router.put("/{channel_key}", response_model=ChannelInfo)
async def update_channel(
    channel_key: str,
    manifest: ChannelManifest,
    x_channel_token: str = Header(...),
) -> ChannelInfo:
    store = _require_store()

    if manifest.channel_key != channel_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="channel_key in body must match URL path",
        )
    if manifest.delivery_webhook:
        _validate_webhook(manifest.delivery_webhook)

    if not store.verify_token(channel_key, x_channel_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid channel token",
        )

    try:
        registered = store.register(manifest, channel_token=x_channel_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )

    return _to_info(registered)


@router.delete("/{channel_key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_key: str,
    x_channel_token: str = Header(...),
) -> None:
    store = _require_store()

    if not store.verify_token(channel_key, x_channel_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid channel token",
        )
    if not store.revoke(channel_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_key}' not found",
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _to_info(ch) -> ChannelInfo:
    m = ch.manifest
    return ChannelInfo(
        channel_key=ch.channel_key,
        display_name=ch.display_name,
        gateway_family=ch.gateway_family,
        status=ch.status,
        registered_at=ch.registered_at,
        last_seen_at=ch.last_seen_at,
        supports_media=m.supports_media,
        supports_reactions=m.supports_reactions,
        supports_voice=m.supports_voice,
        supports_rich_text=m.supports_rich_text,
        max_message_length=m.max_message_length,
        phone_identity_unification=m.phone_identity_unification,
        provides_channel_id=m.provides_channel_id,
        delivery_protocol=m.delivery_protocol,
        delivery_aliases=m.delivery_aliases,
        home_chat_id=m.home_chat_id,
    )
