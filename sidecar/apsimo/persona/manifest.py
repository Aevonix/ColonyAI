"""Persona manifest schema -- parsed from persona.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class HostConfig(BaseModel):
    type: str
    config_overlay: Optional[str] = None
    env_overlay: Optional[str] = None
    identity: Optional[str] = None
    skin: Optional[str] = None
    plugins: list[PluginDef] = Field(default_factory=list)


class PluginDef(BaseModel):
    name: str
    source: str


class ColonyConfig(BaseModel):
    env_overlay: Optional[str] = None
    channels_config: Optional[str] = None
    seed_data: Optional[str] = None


class ServiceDef(BaseModel):
    name: str
    script: Optional[str] = None
    binary: Optional[str] = None
    type: str = "daemon"
    schedule: Optional[dict[str, int]] = None
    env: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    service_template: Optional[str] = None

    @model_validator(mode="after")
    def check_script_or_binary(self):
        if not self.script and not self.binary:
            raise ValueError(f"Service '{self.name}' must define script or binary")
        return self


class ChannelManifestDef(BaseModel):
    display_name: str
    gateway_family: str
    supports_media: bool = False
    supports_voice: bool = False
    supports_reactions: bool = False
    supports_rich_text: bool = False
    session_isolation: bool = False
    provides_channel_id: bool = False
    delivery_webhook: Optional[str] = None
    platform_hint: Optional[str] = None
    pii_safe: bool = True


class CompanionAppDef(BaseModel):
    name: str
    source: str
    channel_key: str
    channel_manifest: ChannelManifestDef


class TunnelDef(BaseModel):
    name: str
    local_port: int
    remote: str
    jump: Optional[str] = None
    tool: str = "autossh"


class SecretDef(BaseModel):
    name: str
    target: str
    description: Optional[str] = None
    required: bool = True


class BackupConfig(BaseModel):
    host_state: list[str] = Field(default_factory=list)
    custom: list[str] = Field(default_factory=list)


class VariableDef(BaseModel):
    name: str
    prompt: str
    default: Optional[str] = None
    env_var: Optional[str] = None


# Allow HostConfig to reference PluginDef (forward ref)
HostConfig.model_rebuild()


class PersonaManifest(BaseModel):
    """Top-level persona.yaml schema."""

    manifest_schema: int = 1
    name: str = Field(..., min_length=1, max_length=64)
    version: str = "0.1.0"

    host: Optional[HostConfig] = None
    colony: Optional[ColonyConfig] = None

    services: list[ServiceDef] = Field(default_factory=list)
    companion_apps: list[CompanionAppDef] = Field(default_factory=list)
    tunnels: list[TunnelDef] = Field(default_factory=list)
    secrets: list[SecretDef] = Field(default_factory=list)
    backup: Optional[BackupConfig] = None
    variables: list[VariableDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_schema_version(self):
        if self.manifest_schema > 1:
            raise ValueError(
                f"Unsupported manifest_schema {self.manifest_schema} "
                "(this Colony supports schema version 1)"
            )
        return self


def load_manifest(repo_path: str | Path) -> PersonaManifest:
    """Load and validate a persona.yaml manifest from a repo directory."""
    repo = Path(repo_path)
    manifest_path = repo / "persona.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No persona.yaml found in {repo}"
        )

    import yaml

    data = yaml.safe_load(manifest_path.read_text())
    return PersonaManifest.model_validate(data)
