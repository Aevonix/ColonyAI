"""Persona deployment engine -- orchestrates setup, backup, restore, services."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TEMPLATE_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _resolve_templates(text: str, variables: dict[str, str]) -> str:
    def replacer(m):
        key = m.group(1)
        if key in variables:
            return variables[key]
        return m.group(0)
    return _TEMPLATE_VAR.sub(replacer, text)


class PersonaEngine:
    """Orchestrates persona deployment operations."""

    def __init__(
        self,
        manifest,
        repo_path: Path,
        state_dir: Optional[Path] = None,
        colony_url: str = "http://127.0.0.1:7777",
        colony_api_key: str = "",
    ) -> None:
        self._manifest = manifest
        self._repo = repo_path
        self._state_dir = state_dir or Path.home() / ".colony" / "data"
        self._colony_url = colony_url
        self._colony_api_key = colony_api_key

        self._persona_dir = Path.home() / ".colony" / "persona" / manifest.name
        self._persona_dir.mkdir(parents=True, exist_ok=True)

        self._variables: dict[str, str] = {}
        self._secrets: dict[str, str] = {}

    @property
    def persona_dir(self) -> Path:
        return self._persona_dir

    # ── Validate ─────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """Validate manifest and paths. Returns list of issues."""
        issues: list[str] = []
        m = self._manifest

        for svc in m.services:
            if svc.script:
                path = self._repo / svc.script
                if not path.exists():
                    issues.append(f"Service '{svc.name}': script not found: {svc.script}")
            if svc.binary:
                path = self._repo / svc.binary
                if not path.exists():
                    issues.append(f"Service '{svc.name}': binary not found: {svc.binary}")

        for app in m.companion_apps:
            path = self._repo / app.source
            if not path.exists():
                issues.append(f"Companion app '{app.name}': source not found: {app.source}")

        if m.host:
            if m.host.config_overlay:
                path = self._repo / m.host.config_overlay
                if not path.exists():
                    issues.append(f"Host config overlay not found: {m.host.config_overlay}")
            if m.host.identity:
                path = self._repo / m.host.identity
                if not path.exists():
                    issues.append(f"Host identity document not found: {m.host.identity}")
            for plugin in m.host.plugins:
                path = self._repo / plugin.source
                if not path.exists():
                    issues.append(f"Plugin '{plugin.name}': source not found: {plugin.source}")

        dep_names = {s.name for s in m.services} | {"hermes", "colony"}
        for svc in m.services:
            for dep in svc.depends_on:
                if dep not in dep_names:
                    issues.append(f"Service '{svc.name}': unknown dependency '{dep}'")

        if _has_circular_deps(m.services):
            issues.append("Circular service dependency detected")

        return issues

    # ── Setup ────────────────────────────────────────────────────────────

    def setup(
        self,
        variables: Optional[dict[str, str]] = None,
        secrets: Optional[dict[str, str]] = None,
        interactive: bool = True,
    ) -> dict[str, Any]:
        """Run full persona setup.

        Returns a summary of what was done.
        """
        summary: dict[str, Any] = {"name": self._manifest.name, "steps": []}

        issues = self.validate()
        if issues:
            return {"name": self._manifest.name, "errors": issues}

        self._load_or_prompt_variables(variables, interactive)
        self._load_or_prompt_secrets(secrets, interactive)

        if self._manifest.host:
            self._apply_host_config()
            summary["steps"].append("host_config")

        if self._manifest.colony:
            self._apply_colony_config()
            summary["steps"].append("colony_config")

        self._install_services()
        summary["steps"].append("services")

        self._register_channels()
        summary["steps"].append("channels")

        self._save_state()
        summary["steps"].append("state_saved")

        return summary

    # ── Variables and secrets ────────────────────────────────────────────

    def _load_or_prompt_variables(
        self,
        provided: Optional[dict[str, str]],
        interactive: bool,
    ) -> None:
        saved_vars = self._persona_dir / "vars.yaml"
        saved = {}
        if saved_vars.exists():
            try:
                import yaml
                saved = yaml.safe_load(saved_vars.read_text()) or {}
            except Exception:
                pass

        for vdef in self._manifest.variables:
            if provided and vdef.name in provided:
                self._variables[vdef.name] = provided[vdef.name]
            elif vdef.name in saved:
                self._variables[vdef.name] = saved[vdef.name]
            elif vdef.env_var and os.environ.get(vdef.env_var):
                self._variables[vdef.name] = os.environ[vdef.env_var]
            elif interactive:
                default = vdef.default or ""
                prompt = f"  {vdef.prompt}"
                if default:
                    prompt += f" [{default}]"
                prompt += ": "
                val = input(prompt).strip() or default
                self._variables[vdef.name] = val
            elif vdef.default is not None:
                self._variables[vdef.name] = vdef.default

    def _load_or_prompt_secrets(
        self,
        provided: Optional[dict[str, str]],
        interactive: bool,
    ) -> None:
        secrets_file = self._persona_dir / "secrets.env"
        saved = {}
        if secrets_file.exists():
            for line in secrets_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    saved[k.strip()] = v.strip()

        for sdef in self._manifest.secrets:
            if provided and sdef.name in provided:
                self._secrets[sdef.name] = provided[sdef.name]
            elif sdef.name in saved:
                self._secrets[sdef.name] = saved[sdef.name]
            elif os.environ.get(sdef.name):
                self._secrets[sdef.name] = os.environ[sdef.name]
            elif interactive:
                import getpass
                desc = sdef.description or sdef.name
                val = getpass.getpass(f"  {desc}: ")
                if val:
                    self._secrets[sdef.name] = val
                elif sdef.required:
                    raise ValueError(f"Required secret '{sdef.name}' not provided")

    def _save_state(self) -> None:
        try:
            import yaml
            vars_path = self._persona_dir / "vars.yaml"
            vars_path.write_text(yaml.dump(self._variables, default_flow_style=False))
        except ImportError:
            vars_path = self._persona_dir / "vars.json"
            vars_path.write_text(json.dumps(self._variables, indent=2))

        secrets_file = self._persona_dir / "secrets.env"
        lines = [f"{k}={v}" for k, v in sorted(self._secrets.items())]
        secrets_file.write_text("\n".join(lines) + "\n")
        secrets_file.chmod(0o600)

        manifest_snapshot = self._persona_dir / "manifest.json"
        manifest_snapshot.write_text(
            self._manifest.model_dump_json(indent=2) + "\n"
        )

    # ── Host config ──────────────────────────────────────────────────────

    def _apply_host_config(self) -> None:
        host = self._manifest.host
        if not host:
            return

        if host.identity:
            src = self._repo / host.identity
            if src.exists():
                logger.info("Host identity document: %s", src)

        if host.config_overlay:
            src = self._repo / host.config_overlay
            if src.exists():
                logger.info("Host config overlay: %s", src)

        for plugin in host.plugins:
            src = self._repo / plugin.source
            if src.exists():
                logger.info("Host plugin: %s -> %s", plugin.name, src)

    # ── Colony config ────────────────────────────────────────────────────

    def _apply_colony_config(self) -> None:
        colony = self._manifest.colony
        if not colony:
            return

        if colony.channels_config:
            src = self._repo / colony.channels_config
            if src.exists():
                dest = self._state_dir / "channels.json"
                shutil.copy2(src, dest)
                logger.info("Copied channels config to %s", dest)

    # ── Service management ───────────────────────────────────────────────

    def _install_services(self) -> None:
        sys_platform = platform.system().lower()
        for svc in self._manifest.services:
            if svc.platforms and sys_platform not in [p.lower() for p in svc.platforms]:
                logger.info("Skipping service '%s' (not for %s)", svc.name, sys_platform)
                continue
            self._install_one_service(svc)

    def _install_one_service(self, svc) -> None:
        sys_platform = platform.system().lower()
        if sys_platform == "darwin":
            self._install_launchagent(svc)
        elif sys_platform == "linux":
            self._install_systemd(svc)
        else:
            logger.warning("Unsupported platform for service install: %s", sys_platform)

    def _install_launchagent(self, svc) -> None:
        label = f"colony.persona.{self._manifest.name}.{svc.name}"
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / f"{label}.plist"

        log_dir = Path.home() / ".colony" / "logs" / "persona"
        log_dir.mkdir(parents=True, exist_ok=True)

        script_path = self._repo / svc.script if svc.script else self._repo / svc.binary
        resolved_env = {
            k: _resolve_templates(v, self._variables)
            for k, v in svc.env.items()
        }
        resolved_env.update(self._secrets)

        env_dict_xml = "\n".join(
            f"      <key>{k}</key>\n      <string>{v}</string>"
            for k, v in resolved_env.items()
        )

        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{shutil.which("python3") or "/usr/bin/python3"}</string>
        <string>{script_path}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{self._repo}</string>
    <key>EnvironmentVariables</key>
    <dict>
{env_dict_xml}
    </dict>
    <key>KeepAlive</key>
    <{'true' if svc.type == 'daemon' else 'false'}/>
    <key>StandardOutPath</key>
    <string>{log_dir / svc.name}.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / svc.name}.err</string>
</dict>
</plist>"""

        if svc.type == "scheduled" and svc.schedule:
            interval = svc.schedule.get("interval", 900)
            plist = plist.replace(
                "<key>KeepAlive</key>\n    <false/>",
                f"<key>StartInterval</key>\n    <integer>{interval}</integer>",
            )

        plist_path.write_text(plist)
        logger.info("Installed LaunchAgent: %s", plist_path)

    def _install_systemd(self, svc) -> None:
        unit_name = f"colony-persona-{self._manifest.name}-{svc.name}"
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / f"{unit_name}.service"

        script_path = self._repo / svc.script if svc.script else self._repo / svc.binary
        resolved_env = {
            k: _resolve_templates(v, self._variables)
            for k, v in svc.env.items()
        }

        env_lines = "\n".join(f"Environment={k}={v}" for k, v in resolved_env.items())
        env_file = self._persona_dir / "secrets.env"

        unit = f"""[Unit]
Description=Colony persona {self._manifest.name}: {svc.name}

[Service]
Type=simple
ExecStart={shutil.which("python3") or "/usr/bin/python3"} {script_path}
WorkingDirectory={self._repo}
{env_lines}
EnvironmentFile=-{env_file}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

        if svc.type == "scheduled" and svc.schedule:
            timer_path = unit_dir / f"{unit_name}.timer"
            interval = svc.schedule.get("interval", 900)
            timer = f"""[Unit]
Description=Timer for {unit_name}

[Timer]
OnBootSec=60
OnUnitActiveSec={interval}
AccuracySec=10

[Install]
WantedBy=timers.target
"""
            timer_path.write_text(timer)
            unit = unit.replace("Type=simple", "Type=oneshot")

        unit_path.write_text(unit)
        logger.info("Installed systemd unit: %s", unit_path)

    # ── Channel registration ─────────────────────────────────────────────

    def _register_channels(self) -> None:
        for app in self._manifest.companion_apps:
            self._register_one_channel(app)

    def _register_one_channel(self, app) -> None:
        try:
            import httpx
        except ImportError:
            logger.warning("httpx not installed, skipping channel registration")
            return

        m = app.channel_manifest
        payload = {
            "channel_key": app.channel_key,
            "display_name": m.display_name,
            "gateway_family": m.gateway_family,
            "supports_media": m.supports_media,
            "supports_voice": m.supports_voice,
            "supports_reactions": m.supports_reactions,
            "supports_rich_text": m.supports_rich_text,
            "session_isolation": m.session_isolation,
            "provides_channel_id": m.provides_channel_id,
            "pii_safe": m.pii_safe,
        }
        if m.delivery_webhook:
            payload["delivery_webhook"] = _resolve_templates(
                m.delivery_webhook, self._variables
            )

        headers = {}
        if self._colony_api_key:
            headers["Authorization"] = f"Bearer {self._colony_api_key}"

        token_file = self._persona_dir / f"channel-token-{app.channel_key}.txt"
        if token_file.exists():
            headers["x-channel-token"] = token_file.read_text().strip()

        try:
            resp = httpx.post(
                f"{self._colony_url}/v1/channels/register",
                json=payload,
                headers=headers,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                if "channel_token" in data:
                    token_file.write_text(data["channel_token"])
                    token_file.chmod(0o600)
                logger.info("Registered channel: %s", app.channel_key)
            else:
                logger.warning(
                    "Channel registration failed for %s: %s %s",
                    app.channel_key, resp.status_code, resp.text,
                )
        except Exception as exc:
            logger.warning("Channel registration error for %s: %s", app.channel_key, exc)

    # ── Service lifecycle ────────────────────────────────────────────────

    def services_status(self) -> list[dict[str, str]]:
        results = []
        sys_platform = platform.system().lower()
        for svc in self._manifest.services:
            status = "not-installed"
            if sys_platform == "darwin":
                label = f"colony.persona.{self._manifest.name}.{svc.name}"
                try:
                    out = subprocess.run(
                        ["launchctl", "list", label],
                        capture_output=True, text=True,
                    )
                    status = "running" if out.returncode == 0 else "stopped"
                except Exception:
                    pass
            elif sys_platform == "linux":
                unit = f"colony-persona-{self._manifest.name}-{svc.name}"
                try:
                    out = subprocess.run(
                        ["systemctl", "--user", "is-active", unit],
                        capture_output=True, text=True,
                    )
                    output = out.stdout.strip()
                    if output == "active":
                        status = "running"
                    elif output == "failed":
                        status = "failed"
                    else:
                        status = "stopped"
                except Exception:
                    pass
            results.append({"name": svc.name, "status": status})
        return results

    def services_start(self) -> list[dict[str, str]]:
        order = _topo_sort(self._manifest.services)
        results = []
        for svc in order:
            result = self._start_service(svc)
            results.append({"name": svc.name, "result": result})
        return results

    def services_stop(self) -> list[dict[str, str]]:
        order = list(reversed(_topo_sort(self._manifest.services)))
        results = []
        for svc in order:
            result = self._stop_service(svc)
            results.append({"name": svc.name, "result": result})
        return results

    def _start_service(self, svc) -> str:
        sys_platform = platform.system().lower()
        try:
            if sys_platform == "darwin":
                label = f"colony.persona.{self._manifest.name}.{svc.name}"
                plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
                if not plist.exists():
                    return "not-installed"
                subprocess.run(["launchctl", "load", str(plist)], check=True)
                return "started"
            elif sys_platform == "linux":
                unit = f"colony-persona-{self._manifest.name}-{svc.name}"
                subprocess.run(
                    ["systemctl", "--user", "start", unit], check=True,
                )
                return "started"
        except Exception as exc:
            logger.warning("Failed to start %s: %s", svc.name, exc)
            return f"error: {exc}"
        return "unsupported-platform"

    def _stop_service(self, svc) -> str:
        sys_platform = platform.system().lower()
        try:
            if sys_platform == "darwin":
                label = f"colony.persona.{self._manifest.name}.{svc.name}"
                plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
                if plist.exists():
                    subprocess.run(["launchctl", "unload", str(plist)], check=True)
                return "stopped"
            elif sys_platform == "linux":
                unit = f"colony-persona-{self._manifest.name}-{svc.name}"
                subprocess.run(
                    ["systemctl", "--user", "stop", unit], check=True,
                )
                return "stopped"
        except Exception as exc:
            logger.warning("Failed to stop %s: %s", svc.name, exc)
            return f"error: {exc}"
        return "unsupported-platform"

    def services_uninstall(self) -> list[dict[str, str]]:
        self.services_stop()
        results = []
        sys_platform = platform.system().lower()
        for svc in self._manifest.services:
            if sys_platform == "darwin":
                label = f"colony.persona.{self._manifest.name}.{svc.name}"
                plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
                if plist.exists():
                    plist.unlink()
                    results.append({"name": svc.name, "result": "removed"})
                else:
                    results.append({"name": svc.name, "result": "not-installed"})
            elif sys_platform == "linux":
                unit = f"colony-persona-{self._manifest.name}-{svc.name}"
                unit_path = Path.home() / ".config" / "systemd" / "user" / f"{unit}.service"
                timer_path = unit_path.with_suffix(".timer")
                for p in (unit_path, timer_path):
                    if p.exists():
                        p.unlink()
                results.append({"name": svc.name, "result": "removed"})
        return results


# ── Dependency graph helpers ─────────────────────────────────────────────


def _has_circular_deps(services) -> bool:
    graph = {s.name: set(s.depends_on) for s in services}
    visited: set[str] = set()
    path: set[str] = set()

    def visit(node: str) -> bool:
        if node in path:
            return True
        if node in visited:
            return False
        visited.add(node)
        path.add(node)
        for dep in graph.get(node, set()):
            if dep in graph and visit(dep):
                return True
        path.discard(node)
        return False

    return any(visit(name) for name in graph)


def _topo_sort(services) -> list:
    graph = {s.name: set(s.depends_on) for s in services}
    svc_map = {s.name: s for s in services}
    result = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited or name not in svc_map:
            return
        visited.add(name)
        for dep in graph.get(name, set()):
            visit(dep)
        result.append(svc_map[name])

    for s in services:
        visit(s.name)
    return result
