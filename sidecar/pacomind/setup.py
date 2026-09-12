"""Shared configuration helpers for the guided Hermes setup and CLI diagnostics."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def _prompt(prompt: str, default: str = "", non_interactive: bool = False, ask=None) -> str:
    """Prompt for input with a default value. Returns default on EOF or non-interactive mode.

    Also checks for PACOMIND_INIT_DEFAULTS env var for scripted defaults.
    Format: PACOMIND_INIT_DEFAULTS='key1=val1,key2=val2'

    ``ask`` is an injectable input callable (defaults to ``input``) so tests
    can script answers without monkeypatching stdin. UX is unchanged.
    """
    if non_interactive:
        return default

    # Check for scripted defaults
    defaults_env = os.environ.get("PACOMIND_INIT_DEFAULTS", "")
    if defaults_env:
        for pair in defaults_env.split(","):
            if "=" in pair:
                key, val = pair.split("=", 1)
                # Map prompt keywords to defaults
                prompt_lower = prompt.lower()
                if key.lower() in prompt_lower or prompt_lower in key.lower():
                    return val

    suffix = f" [{default}]" if default else ""
    try:
        val = (ask or input)(f"{prompt}{suffix}: ").strip()
        return val or default
    except EOFError:
        # Gracefully handle piped input exhaustion
        print()  # Add newline for clean output
        return default
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


def _check_port(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _resolve_hermes_home(hermes_home: str | Path | None = None) -> Path:
    """Resolve the selected Hermes home without guessing profile layouts."""
    selected = hermes_home or os.environ.get("HERMES_HOME")
    return Path(selected or Path.home() / ".hermes").expanduser().resolve()


def _read_hermes_config(config_path: Path) -> tuple[bytes | None, dict]:
    """Parse configuration without exposing YAML values in error messages."""
    import yaml

    class UniqueKeyLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            # Reject ambiguous duplicate/merge keys rather than silently losing
            # configuration. Only the chosen host's configuration is inspected.
            self.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in result:
                    raise ValueError("Duplicate YAML mapping key")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    if config_path.is_symlink():
        raise ValueError("Symlinked Hermes config requires a reviewed migration")
    original = config_path.read_bytes() if config_path.exists() else None
    try:
        config = yaml.load(original.decode("utf-8"), Loader=UniqueKeyLoader) if original else {}
    except (UnicodeError, yaml.YAMLError, TypeError, ValueError):
        raise ValueError("Hermes config is invalid or has ambiguous YAML keys") from None
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("Hermes config must be a YAML mapping")
    return original, config


HERMES_MEMORY_SPILL_CHARS = 65536


def _align_hermes_memory_spill(config: dict) -> bool:
    """Keep native head/tail previews from cutting PacoMind's evidence envelope.

    Selected memory is capped at 24k characters; 64 KiB leaves headroom for its
    citations and other default context sections. This is a
    transfer allowance, not a larger retrieval budget. Custom larger context
    producers must align their own limits. Existing disabled/larger spill
    settings remain the operator's choice.
    """
    hooks = config.get('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Hermes hooks settings must be a YAML mapping')
    spill = hooks.get('output_spill', {})
    if not isinstance(spill, dict):
        raise ValueError('Hermes hook output_spill settings must be a YAML mapping')
    if spill.get('enabled') is False:
        return False
    try:
        maximum = int(spill.get('max_chars', 10000))
    except (ValueError, TypeError, OverflowError):
        maximum = 10000
    if maximum >= HERMES_MEMORY_SPILL_CHARS:
        return False
    config['hooks'] = {**hooks, 'output_spill': {**spill, 'max_chars': HERMES_MEMORY_SPILL_CHARS}}
    return True


def _validate_hermes_binding(config: dict) -> None:
    """Validate existing native selections without renaming or replacing them."""
    from .util.instance import plugin_settings
    plugin_settings(config)
    plugins = config.get('plugins', {})
    for key in ('enabled', 'disabled'):
        if key in plugins and not isinstance(plugins[key], list):
            raise ValueError('Hermes plugins enabled/disabled must be lists')
    selections = [config[key] for key in ('toolsets',) if key in config]
    selections.extend(config.get('platform_toolsets', {}).values())
    agent = config.get('agent', {})
    if isinstance(agent, dict) and 'disabled_toolsets' in agent:
        selections.append(agent['disabled_toolsets'])
    for values in selections:
        if not isinstance(values, list):
            raise ValueError('Hermes toolsets must be lists')
        if any(not isinstance(value, str) for value in values):
            raise ValueError('Hermes toolset names must be strings')


def _prepare_hermes_config(
    config_path: Path, sidecar_url: str, contact_id: str,
) -> tuple[bytes | None, bytes]:
    """Prepare a narrow semantic update; preserve existing secrets and identity.

    PyYAML preserves values, not comments/formatting. The original bytes are
    retained in a private backup when an actual configuration change is made.
    This prepares configuration only: it does not qualify or activate Hermes.
    """
    import copy
    import json
    from urllib.parse import urlsplit
    import yaml

    try:
        url = urlsplit(sidecar_url)
        port = url.port  # Access validates numeric syntax and the 0..65535 range.
        valid_url = (url.scheme in {"http", "https"} and url.hostname
                     and not url.username and not url.password
                     and not url.query and not url.fragment
                     and (port is None or port > 0))
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("Sidecar URL must be HTTP(S), with a valid port and no embedded credentials or query parameters")

    original, config = _read_hermes_config(config_path)
    before = copy.deepcopy(config)
    _validate_hermes_binding(config)

    def mapping(parent: dict, key: str) -> dict:
        if key not in parent:
            parent[key] = {}
        if not isinstance(parent[key], dict):
            raise ValueError("Hermes memory/plugin settings must be YAML mappings")
        parent[key] = dict(parent[key])  # Do not mutate unrelated YAML alias users.
        return parent[key]

    memory = mapping(config, "memory")
    provider = memory.get("provider")
    if provider not in (None, "", "pacomind-memory"):
        raise ValueError("Another memory provider is configured; migrate it explicitly before staging PacoMind")
    memory_config = mapping(memory, "config")
    plugins = mapping(config, "plugins")
    plugin_config = mapping(plugins, "pacomind")
    native_path = config_path.with_name("pacomind-memory.json")
    try:
        native_config = json.loads(native_path.read_text()) if native_path.exists() else {}
    except (OSError, ValueError):
        raise ValueError("Native PacoMind memory configuration is invalid") from None
    if not isinstance(native_config, dict):
        raise ValueError("Native PacoMind memory configuration must be an object")

    # Do not redirect an existing private instance or replace its contact just
    # because the init wizard supplies defaults for a fresh installation.
    for settings in (memory_config, plugin_config, native_config):
        if settings.get("url") not in (None, "", sidecar_url):
            raise ValueError("Existing PacoMind endpoint differs; use a reviewed instance migration")
        if contact_id and settings.get("contact_id") not in (None, "", contact_id):
            raise ValueError("Existing PacoMind contact differs; preserve its identity or migrate explicitly")
    bindings = [plugin_config.get("owner_contact_id"), native_config.get("contact_id"),
                memory_config.get("contact_id"), plugin_config.get("contact_id")]
    selected_contact = contact_id or next((value for value in bindings if value), None)
    if not isinstance(selected_contact, str) or not selected_contact.strip():
        raise ValueError("Provide --contact-name or retain an existing PacoMind contact binding")
    if any(value not in (None, "", selected_contact) for value in bindings):
        raise ValueError("Existing PacoMind contact bindings disagree; reconcile them before staging")

    memory["provider"] = "pacomind-memory"
    for settings in (memory_config, plugin_config):
        if settings.get("url") in (None, ""):
            settings["url"] = sidecar_url
        if settings.get("api_key") in (None, ""):
            settings["api_key"] = "${PACOMIND_API_KEY}"
        if settings.get("contact_id") in (None, ""):
            settings["contact_id"] = selected_contact
    _align_hermes_memory_spill(config)
    # Do not install/select a custom context engine, enable the general plugin,
    # or alter coexistence latches. The supported native spill allowance keeps
    # the already selected evidence and its source revisions together.
    if original is not None and config == before:
        return original, original
    return original, yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode("utf-8")


def _atomic_hermes_config_write(config_path: Path, original: bytes | None, updated: bytes) -> None:
    """Replace config atomically, preserving the previous bytes privately."""
    import stat
    import tempfile

    current = config_path.read_bytes() if config_path.exists() else None
    if config_path.is_symlink() or current != original:
        raise ValueError("Hermes config changed during staging; retry after reconciling it")
    if current == updated:
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(config_path.stat().st_mode) if current is not None else 0o600
    if original is not None:
        with tempfile.NamedTemporaryFile(
            prefix=f".{config_path.name}.pacomind-backup-", dir=config_path.parent, delete=False,
        ) as backup:
            backup.write(original)
            backup.flush()
            os.fsync(backup.fileno())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{config_path.name}.pacomind-stage-", dir=config_path.parent, delete=False,
        ) as staged:
            temporary = Path(staged.name)
            os.fchmod(staged.fileno(), mode)
            staged.write(updated)
            staged.flush()
            os.fsync(staged.fileno())
        if config_path.is_symlink() or (config_path.read_bytes() if config_path.exists() else None) != original:
            raise ValueError("Hermes config changed during staging; retry after reconciling it")
        os.replace(temporary, config_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


OPENAI_COMPAT_PROVIDERS = frozenset({
    "zai", "local", "custom", "lmstudio", "vllm", "openai",
    "openai-compatible", "openai_compatible",
})


def normalize_llm_base_url(url: str, provider: str) -> tuple[str, bool]:
    """Ensure ``baseUrl`` ends with ``/v1`` for OpenAI-compatible providers.

    A bare ``host:port`` base URL silently 404s on chat completions because
    LiteLLM appends ``/chat/completions`` directly. Returns ``(url, changed)``;
    non-OpenAI-compatible providers (e.g. ollama, anthropic) pass through.
    """
    if not url:
        return url, False
    if (provider or "").strip().lower() not in OPENAI_COMPAT_PROVIDERS:
        return url, False
    stripped = url.rstrip("/")
    if stripped.endswith("/v1"):
        return url, False
    return stripped + "/v1", True


def ensure_api_key(cfg: dict) -> tuple[dict, bool]:
    """Never persist an empty ``apiKey`` for OpenAI-compatible providers.

    LiteLLM requires a non-empty api_key even for keyless local servers
    (vLLM, LM Studio, llama.cpp, ...) — an empty string breaks the auth
    header. Defaults to ``"local-no-key"``. Returns ``(cfg, changed)``;
    the input dict is never mutated.
    """
    provider = (cfg.get("provider") or "").strip().lower()
    if provider in OPENAI_COMPAT_PROVIDERS and not (cfg.get("apiKey") or "").strip():
        fixed = dict(cfg)
        fixed["apiKey"] = "local-no-key"
        return fixed, True
    return cfg, False


def apply_llm_config_fixes(cfg: dict) -> tuple[dict, list[str]]:
    """Apply both LLM host-config footgun fixes. Returns ``(cfg, notes)``."""
    notes: list[str] = []
    fixed = dict(cfg)
    url, changed = normalize_llm_base_url(fixed.get("baseUrl", ""), fixed.get("provider", ""))
    if changed:
        fixed["baseUrl"] = url
        notes.append(f"baseUrl did not end with /v1 — normalized to {url}")
    fixed, changed = ensure_api_key(fixed)
    if changed:
        notes.append(
            'apiKey was empty — set to "local-no-key" '
            "(LiteLLM requires a non-empty value even for keyless servers)"
        )
    return fixed, notes


def write_llm_host_config(path: Path, cfg: dict) -> tuple[dict, list[str]]:
    """Persist the normalized LLM host config and return its repair notes."""
    import json
    fixed, notes = apply_llm_config_fixes(cfg)
    path.write_text(json.dumps(fixed, indent=2))
    return fixed, notes


def repair_persisted_llm_config() -> list[str]:
    """Normalize the saved host config for ``pacomind doctor --fix``.

    Return repair notes, or an empty list when no change is needed.
    """
    import json
    from pacomind import get_state_dir

    config_path = get_state_dir() / ".pacomind-llm-config.json"
    if not config_path.exists():
        return []
    cfg = json.loads(config_path.read_text())
    fixed, notes = apply_llm_config_fixes(cfg)
    if notes:
        write_llm_host_config(config_path, fixed)
    return notes


async def build_owner_contact(
    store,
    display_name: str,
    handles: list[tuple[str, str]] | None = None,
    *,
    trust_tier: str = "inner_circle",
    interaction_allowed: bool = True,
    import_source: str = "wizard",
) -> str:
    """Create the owner contact (plus handles) and return its contact_id.

    The first handle becomes primary. A handle already owned by another
    contact is skipped rather than failing the owner record — identity
    fail-closed needs the owner cid to exist either way.
    """
    contact = await store.create(
        display_name=display_name,
        trust_tier=trust_tier,
        interaction_allowed=interaction_allowed,
        import_source=import_source,
    )
    for i, (gateway, address) in enumerate(handles or []):
        try:
            await store.add_handle(
                contact.contact_id,
                gateway=gateway,
                address=address,
                is_primary=(i == 0),
                source="wizard",
                verified=True,
            )
        except ValueError:
            # Address already assigned to another contact — skip it.
            continue
    return contact.contact_id


def run_init(root_dir: str | None = None, args=None) -> int:
    """Run guided setup for the selected Hermes profile and return its exit code."""
    from pacomind.setup_hermes import run
    return run(root_dir, args)
