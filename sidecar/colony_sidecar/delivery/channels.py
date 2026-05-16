"""ChannelRegistry — resolves per-person delivery channels from multiple sources.

Resolution priority (highest first):
1. Environment variables (COLONY_CHANNEL_*)
2. JSON config file ({COLONY_STATE_DIR}/data/channels.json)
3. Contact handles (phone → chat platform DM inference, configurable mapping)
4. Home channel fallback (WHATSAPP_HOME_CHANNEL, TELEGRAM_HOME_CHANNEL, etc.)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Channel:
    """A resolved delivery channel."""

    platform: str        # "whatsapp", "telegram", "discord", ...
    chat_id: str         # platform-specific chat identifier
    channel_type: str    # "dm" | "home" | "work" | "custom"


class ChannelRegistry:
    """Resolves per-person delivery channels from multiple sources."""

    # Default gateway-to-platform mapping (override via COLONY_CHANNEL_GATEWAY_MAP)
    DEFAULT_GATEWAY_MAP = {
        "imessage": "whatsapp",
        "sms": "whatsapp",
        "telegram": "telegram",
        "signal": "signal",
        # "email" excluded — not a chat platform
    }

    def __init__(
        self,
        env_channels: Dict[str, Dict[str, Channel]],
        json_channels: Dict[str, Dict[str, Channel]],
        fallback_channels: Dict[str, Channel],
        handle_inference: bool,
        gateway_map: Dict[str, str],
        contacts_store: Optional[Any] = None,
    ) -> None:
        self._env = env_channels
        self._json = json_channels
        self._fallback = fallback_channels
        self._handle_inference = handle_inference
        self._gateway_map = gateway_map
        self._contacts_store = contacts_store
        self._json_path_value: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        person_id: str,
        channel_type: str = "home",
    ) -> Optional[Channel]:
        """Resolve a delivery channel for ``person_id`` and ``channel_type``.

        Resolution order:
        1. Env vars (COLONY_CHANNEL_DM_* / COLONY_CHANNEL_HOME_*)
        2. JSON config (contacts section)
        3. JSON fallback
        4. Contact handle inference (DM only)
        5. Home channel env vars (home only)

        Returns ``None`` if no channel can be resolved.
        """
        normalized_id = self._normalize_person_id(person_id)

        # 1. Env vars (highest priority)
        env_person = self._env.get(normalized_id, {})
        if channel_type in env_person:
            return env_person[channel_type]

        # 2. JSON config (contacts section)
        json_person = self._json.get(normalized_id, {})
        if channel_type in json_person:
            return json_person[channel_type]

        # 3. JSON fallback
        if channel_type in self._fallback:
            return self._fallback[channel_type]

        # 4. Contact handle inference (DM only)
        if channel_type == "dm" and self._handle_inference and self._contacts_store:
            inferred = self._infer_from_handles(normalized_id)
            if inferred:
                return inferred

        # 5. Home channel env vars (home only, lowest priority)
        if channel_type == "home":
            home = self._resolve_home_from_env()
            if home:
                return home

        return None

    def reload(self) -> None:
        """Re-read env vars and JSON config without restarting the sidecar.

        This is a partial reload — contact handles are not re-fetched.
        """
        env_prefix = "COLONY_CHANNEL_"
        self._env, self._json, self._fallback = self._load_sources(
            json_path=self._json_path(),
            env_prefix=env_prefix,
        )
        logger.info("ChannelRegistry reloaded")

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def load(
        cls,
        json_path: Optional[str] = None,
        env_prefix: str = "COLONY_CHANNEL_",
        handle_inference: Optional[bool] = None,
        gateway_map: Optional[Dict[str, str]] = None,
        contacts_store: Optional[Any] = None,
    ) -> "ChannelRegistry":
        """Load a ChannelRegistry from all configured sources.

        Args:
            json_path: Path to channels.json. Defaults to {COLONY_STATE_DIR}/data/channels.json.
            env_prefix: Prefix for env var scanning.
            handle_inference: Enable contact handle inference. Defaults to env var COLONY_CHANNEL_INFER_FROM_HANDLES.
            gateway_map: Override gateway-to-platform mapping. Defaults to env var COLONY_CHANNEL_GATEWAY_MAP.
            contacts_store: Optional contact store for handle inference.
        """
        if json_path is None:
            state_dir = os.environ.get("COLONY_STATE_DIR", str(Path.home() / ".colony"))
            json_path = os.path.join(state_dir, "data", "channels.json")

        if handle_inference is None:
            handle_inference = os.environ.get("COLONY_CHANNEL_INFER_FROM_HANDLES", "true").lower() == "true"

        if gateway_map is None:
            raw_map = os.environ.get("COLONY_CHANNEL_GATEWAY_MAP", "")
            if raw_map:
                try:
                    gateway_map = json.loads(raw_map)
                except json.JSONDecodeError:
                    logger.warning("Invalid COLONY_CHANNEL_GATEWAY_MAP JSON, using defaults")
                    gateway_map = dict(cls.DEFAULT_GATEWAY_MAP)
            else:
                gateway_map = dict(cls.DEFAULT_GATEWAY_MAP)

        # Normalize gateway_map
        if gateway_map is None:
            gateway_map = dict(cls.DEFAULT_GATEWAY_MAP)

        env_channels, json_channels, fallback = cls._load_sources(json_path, env_prefix)

        registry = cls(
            env_channels=env_channels,
            json_channels=json_channels,
            fallback_channels=fallback,
            handle_inference=handle_inference,
            gateway_map=gateway_map,
            contacts_store=contacts_store,
        )
        registry._json_path_value = json_path  # stash for reload()

        logger.info(
            "ChannelRegistry loaded: %d env entries, %d JSON entries, inference=%s",
            sum(len(v) for v in env_channels.values()),
            sum(len(v) for v in json_channels.values()),
            handle_inference,
        )
        return registry

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_person_id(person_id: str) -> str:
        """Normalize a person ID: lowercase, spaces → underscores."""
        return person_id.lower().strip().replace(" ", "_")

    @classmethod
    def _load_sources(
        cls,
        json_path: str,
        env_prefix: str,
    ) -> tuple[Dict[str, Dict[str, Channel]], Dict[str, Dict[str, Channel]], Dict[str, Channel]]:
        """Load channels from env vars and JSON file."""
        env_channels = cls._load_from_env(env_prefix)
        json_channels, fallback = cls._load_from_json(json_path)
        return env_channels, json_channels, fallback

    @classmethod
    def _load_from_env(
        cls,
        prefix: str,
    ) -> Dict[str, Dict[str, Channel]]:
        """Scan env vars matching COLONY_CHANNEL_DM_* and COLONY_CHANNEL_HOME_*."""
        channels: Dict[str, Dict[str, Channel]] = {}
        dm_prefix = f"{prefix}DM_"
        home_prefix = f"{prefix}HOME_"

        for key, value in os.environ.items():
            person_id: Optional[str] = None
            channel_type: Optional[str] = None

            if key.startswith(dm_prefix):
                person_id = key[len(dm_prefix):]
                channel_type = "dm"
            elif key.startswith(home_prefix):
                # HOME_ could be global (HOME) or per-person (HOME_owner)
                remainder = key[len(home_prefix):]
                if remainder:
                    person_id = remainder
                else:
                    person_id = "__global__"
                channel_type = "home"
            elif key == f"{prefix}HOME":
                person_id = "__global__"
                channel_type = "home"
            else:
                continue

            if not value:
                continue

            platform, chat_id = cls._parse_channel_value(value)
            if platform and chat_id:
                normalized_id = cls._normalize_person_id(person_id)
                if normalized_id not in channels:
                    channels[normalized_id] = {}
                channels[normalized_id][channel_type] = Channel(
                    platform=platform,
                    chat_id=chat_id,
                    channel_type=channel_type,
                )

        return channels

    @classmethod
    def _load_from_json(
        cls,
        path: str,
    ) -> tuple[Dict[str, Dict[str, Channel]], Dict[str, Channel]]:
        """Load channels from JSON config file."""
        channels: Dict[str, Dict[str, Channel]] = {}
        fallback: Dict[str, Channel] = {}

        try:
            if not Path(path).exists():
                return channels, fallback
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load channels.json: %s", exc)
            return channels, fallback

        # contacts section
        for person_id, person_channels in data.get("contacts", {}).items():
            normalized_id = cls._normalize_person_id(person_id)
            channels[normalized_id] = {}
            for channel_type, info in person_channels.items():
                platform = info.get("platform", "")
                chat_id = info.get("chat_id", "")
                if platform and chat_id:
                    channels[normalized_id][channel_type] = Channel(
                        platform=platform,
                        chat_id=chat_id,
                        channel_type=channel_type,
                    )

        # fallback section
        for channel_type, info in data.get("fallback", {}).items():
            platform = info.get("platform", "")
            chat_id = info.get("chat_id", "")
            if platform and chat_id:
                fallback[channel_type] = Channel(
                    platform=platform,
                    chat_id=chat_id,
                    channel_type=channel_type,
                )

        return channels, fallback

    @classmethod
    def _parse_channel_value(cls, value: str) -> tuple[Optional[str], Optional[str]]:
        """Parse a channel value like 'telegram:@username' or 'whatsapp:+1555...'."""
        if not value:
            return None, None
        if ":" in value:
            platform, chat_id = value.split(":", 1)
            return platform.strip().lower(), chat_id.strip()
        # If no platform prefix, assume it's a chat ID and platform is unknown
        return None, value.strip()

    def _resolve_home_from_env(self) -> Optional[Channel]:
        """Scan for {PLATFORM}_HOME_CHANNEL env vars."""
        pattern = re.compile(r"^(\w+)_HOME_CHANNEL$")
        for key in sorted(os.environ.keys()):
            match = pattern.match(key)
            if match:
                platform = match.group(1).lower()
                chat_id = os.environ[key]
                if chat_id:
                    return Channel(
                        platform=platform,
                        chat_id=chat_id,
                        channel_type="home",
                    )
        return None

    def _infer_from_handles(self, person_id: str) -> Optional[Channel]:
        """Infer DM channel from contact handles."""
        if self._contacts_store is None:
            return None

        try:
            # Try to get handles for this contact
            handles = self._contacts_store.get_handles(person_id)
        except Exception:
            return None

        if not handles:
            return None

        for handle in handles:
            gateway = getattr(handle, "gateway", "")
            address = getattr(handle, "address", "")
            if not gateway or not address:
                continue

            platform = self._gateway_map.get(gateway)
            if not platform:
                continue

            # Normalize phone numbers
            if platform in ("whatsapp", "signal"):
                address = self._normalize_phone(address)

            return Channel(
                platform=platform,
                chat_id=address,
                channel_type="dm",
            )

        return None

    @staticmethod
    def _normalize_phone(address: str) -> str:
        """Normalize a phone number address."""
        # Strip spaces, ensure leading +
        cleaned = address.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if cleaned.startswith("++"):
            cleaned = cleaned[1:]
        if not cleaned.startswith("+") and cleaned.isdigit():
            cleaned = "+" + cleaned
        return cleaned

    def _json_path(self) -> str:
        """Return the JSON config path (stashed during load)."""
        return getattr(self, "_json_path_value", "")
