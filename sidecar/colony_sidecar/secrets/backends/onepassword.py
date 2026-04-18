"""1Password secrets backend — supports op CLI and 1Password Connect Server API."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field

from colony_sidecar.secrets.backends.base import SecretsBackend
from colony_sidecar.secrets.types import SecretType, VAULT_NAME

logger = logging.getLogger(__name__)


@dataclass
class OPItem:
    """Represents a 1Password item as used by Colony."""

    item_id: str
    title: str       # Secret key (e.g., "OPENAI_API_KEY")
    value: str
    category: str    # Derived from SecretType
    tags: list[str] = field(default_factory=list)


class OnePasswordBackend(SecretsBackend):
    """1Password secrets backend.

    Supports two modes:
    1. CLI mode (default): uses ``op`` CLI — requires 1Password desktop app
       or 1Password CLI with biometric unlock configured.
    2. Connect mode: uses 1Password Connect Server API — suitable for
       headless/server deployments. Requires OP_CONNECT_HOST and OP_CONNECT_TOKEN.

    Vault structure:
      - Default vault name: "Colony" (overridable in config)
      - Items organised by section: LLM Keys, Infrastructure, Messaging
        Gateways, Mesh / Federation, Email, Backup, Push Notifications,
        Colony Core.

    Auto-detects mode based on available configuration.
    """

    name = "1password"

    # 1Password item sections by SecretType
    SECTION_MAP: dict[SecretType, str] = {
        SecretType.LLM_API_KEY: "LLM Keys",
        SecretType.NEO4J_CREDENTIAL: "Infrastructure",
        SecretType.GATEWAY_TOKEN: "Messaging Gateways",
        SecretType.MESH_PAIRING_KEY: "Mesh / Federation",
        SecretType.EMAIL_CREDENTIAL: "Email",
        SecretType.BACKUP_PASSPHRASE: "Backup",
        SecretType.COLONY_TOKEN: "Colony Core",
        SecretType.FEDERATION_CERT: "Mesh / Federation",
        SecretType.PUSH_CREDENTIAL: "Push Notifications",
        SecretType.CALDAV_CREDENTIAL: "Calendar",
        SecretType.HEALTH_CREDENTIAL: "Health",
        SecretType.OTHER: "Miscellaneous",
    }

    def __init__(
        self,
        vault_name: str = VAULT_NAME,
        connect_host: str | None = None,
        connect_token: str | None = None,
    ) -> None:
        self._vault = vault_name
        self._connect_host = connect_host
        self._connect_token = connect_token
        self._use_connect = bool(connect_host and connect_token)
        self._cache: dict[str, str] = {}  # in-process cache

    def is_available(self) -> bool:
        if self._use_connect:
            return bool(self._connect_host and self._connect_token)
        return shutil.which("op") is not None

    def get(self, key: str, default: str | None = None) -> str | None:
        if key in self._cache:
            return self._cache[key]
        if self._use_connect:
            return self._get_via_connect(key, default)
        return self._get_via_cli(key, default)

    def set(self, key: str, value: str, *, secret_type: SecretType | None = None) -> None:
        if self._use_connect:
            self._set_via_connect(key, value, secret_type)
        else:
            self._set_via_cli(key, value, secret_type)
        self._cache[key] = value

    def delete(self, key: str) -> None:
        try:
            self._run_op(["item", "delete", self._item_title(key), f"--vault={self._vault}"])
        except subprocess.CalledProcessError as exc:
            logger.debug("1Password delete failed for %s: %s", key, exc)
        self._cache.pop(key, None)

    def list(self) -> list[str]:
        try:
            result = self._run_op([
                "item", "list",
                f"--vault={self._vault}",
                "--format=json",
                "--tags=colony",
            ])
            items = json.loads(result)
            prefix = "colony:"
            return [
                item["title"][len(prefix):]
                if item["title"].startswith(prefix)
                else item["title"]
                for item in items
            ]
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            logger.debug("1Password list failed: %s", exc)
            return []

    # -------------------------------------------------------------------------
    # CLI helpers
    # -------------------------------------------------------------------------

    def _get_via_cli(self, key: str, default: str | None) -> str | None:
        try:
            result = self._run_op([
                "item", "get", self._item_title(key),
                f"--vault={self._vault}",
                "--fields=password",
                "--format=json",
            ])
            data = json.loads(result)
            # op returns {"value": "..."} or {"password": "..."} depending on version
            value = data.get("value") or data.get("password")
            if value:
                self._cache[key] = value
            return value or default
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return default

    def _set_via_cli(
        self, key: str, value: str, secret_type: SecretType | None
    ) -> None:
        section = self.SECTION_MAP.get(secret_type or SecretType.OTHER, "Miscellaneous")
        title = self._item_title(key)

        # Try update first, create if item not found
        try:
            self._run_op([
                "item", "edit", title,
                f"--vault={self._vault}",
                f"password={value}",
            ])
        except subprocess.CalledProcessError:
            self._run_op([
                "item", "create",
                "--category=login",
                f"--title={title}",
                f"--vault={self._vault}",
                f"--section={section}",
                f"password={value}",
                "--tags=colony",
            ])

    def _run_op(self, args: list[str]) -> str:
        result = subprocess.run(
            ["op"] + args,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()

    @staticmethod
    def _item_title(key: str) -> str:
        """Map a secret key to a 1Password item title."""
        return f"colony:{key}"

    # -------------------------------------------------------------------------
    # 1Password Connect Server API helpers
    # -------------------------------------------------------------------------

    def _get_via_connect(self, key: str, default: str | None) -> str | None:
        """Retrieve via 1Password Connect Server REST API."""
        import urllib.request
        url = f"{self._connect_host}/v1/vaults/{self._vault}/items"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._connect_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                items = json.loads(resp.read())
                target = self._item_title(key)
                for item in items:
                    if item.get("title") == target:
                        fields = item.get("fields", [])
                        if fields:
                            value = fields[0].get("value")
                            if value:
                                self._cache[key] = value
                                return value
        except Exception as exc:
            logger.debug("1Password Connect get failed for %s: %s", key, exc)
        return default

    def _set_via_connect(
        self, key: str, value: str, secret_type: SecretType | None
    ) -> None:
        """Store via 1Password Connect Server REST API."""
        import urllib.request
        section = self.SECTION_MAP.get(secret_type or SecretType.OTHER, "Miscellaneous")
        payload = json.dumps({
            "title": self._item_title(key),
            "category": "LOGIN",
            "tags": ["colony"],
            "sections": [{"label": section}],
            "fields": [{"label": "password", "value": value, "type": "CONCEALED"}],
        }).encode()
        req = urllib.request.Request(
            f"{self._connect_host}/v1/vaults/{self._vault}/items",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._connect_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
