"""Legacy .env file secrets backend."""

from __future__ import annotations

import os
from pathlib import Path

from colony_sidecar.secrets.backends.base import SecretsBackend
from colony_sidecar.secrets.types import SecretType


class EnvBackend(SecretsBackend):
    """Legacy .env file backend.

    Reads from environment variables (which may be loaded from .env by
    python-dotenv at startup). Writes update the .env file in place.

    This backend is maintained for backward compatibility and for users
    in containerized environments who inject secrets via environment variables.
    It is the DEFAULT backend when no other backend is configured, ensuring
    zero-friction upgrade from V1.

    The .env file is always written with mode 0o600 (SEC-007).
    """

    name = "env"

    def __init__(self, env_path: str | None = None) -> None:
        self._env_path = env_path or str(
            Path.home() / ".colony" / ".env"
        )

    def is_available(self) -> bool:
        return True  # Always available

    def get(self, key: str, default: str | None = None) -> str | None:
        return os.environ.get(key, default)

    def set(self, key: str, value: str, *, secret_type: SecretType | None = None) -> None:
        """Write to .env file with 0o600 permissions (SEC-007 pattern)."""
        env_path = Path(self._env_path)
        env_path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

        key_found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                key_found = True
                break
        if not key_found:
            lines.append(f"{key}={value}\n")

        # Write atomically with 0o600 (SEC-007: no TOCTOU window)
        fd = os.open(
            str(env_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)

        os.environ[key] = value

    def delete(self, key: str) -> None:
        env_path = Path(self._env_path)
        if not env_path.exists():
            os.environ.pop(key, None)
            return
        lines = [
            line
            for line in env_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith(f"{key}=")
        ]
        fd = os.open(
            str(env_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.environ.pop(key, None)

    def list(self) -> list[str]:
        env_path = Path(self._env_path)
        if not env_path.exists():
            return []
        keys = []
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                keys.append(stripped.split("=", 1)[0])
        return keys
