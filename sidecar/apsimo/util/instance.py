"""Load one private instance, retaining legacy deployment environment rules."""
from pathlib import Path
import json
import os

from apsimo.environment import (
    apply_environment_aliases, clear_environment_aliases, normalize_environment,
)


def plugin_settings(config):
    """Read the one platform binding without changing a configuration."""
    plugins = config.get('plugins', {}) if isinstance(config, dict) else {}
    if not isinstance(plugins, dict):
        raise ValueError('Hermes plugins must be a mapping')
    old, new = plugins.get('colony'), plugins.get('apsimo')
    if old is not None and new is not None and old != new:
        raise ValueError('Conflicting ColonyAI and Apsimo instance bindings')
    selected = new if new is not None else old
    if selected is not None and not isinstance(selected, dict):
        raise ValueError('Hermes Apsimo plugin settings must be a mapping')
    return selected or {}


def load_environment():
    # Startup/explicit instance-selection boundary only. Runtime reads never
    # rewrite the process environment.
    clear_environment_aliases()
    selected_env = normalize_environment()
    if (selected_env.get('COLONY_SKIP_DOTENV', '').lower() in {'1', 'true', 'yes', 'on'}
            and selected_env.get('COLONY_INSTANCE_SELECTED') != '1'):
        apply_environment_aliases()
        return
    selected = selected_env.get('COLONY_STATE_DIR')
    explicitly_selected = selected_env.get('COLONY_INSTANCE_SELECTED') == '1'
    if not selected:
        import yaml
        home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes').expanduser()
        config_path = home / 'config.yaml'
        if config_path.is_file():
            config = yaml.safe_load(config_path.read_text()) or {}
            selected = plugin_settings(config).get('instance_dir')
            explicitly_selected = bool(selected)
    managed = bool(selected and (explicitly_selected or
        (Path(selected).expanduser()/'instance.json').is_file()))
    if managed:
        selected = str(Path(selected).expanduser().resolve())
        try:
            import yaml
            manifest = json.loads((Path(selected)/'instance.json').read_text())
            home = Path(manifest['hermes_home']).expanduser().resolve()
            config = yaml.safe_load((home/'config.yaml').read_text())
            if (manifest.get('version') != 1 or manifest.get('profile') != 'local'
                    or plugin_settings(config).get('instance_dir') != selected
                    or not (Path(selected)/'.env').is_file()):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
            raise ValueError('Selected private instance is incomplete or its Hermes binding changed; no legacy fallback is allowed') from None
    # Existing wrappers set ~/.colony/data as state but read ~/.colony/.env.
    paths = [Path(selected)/'.env'] if managed else [Path.home()/'.colony'/'.env', Path.cwd()/'.env']
    for path in paths:
        if path.is_file():
            if managed:
                from dotenv import dotenv_values
                values = {key: value for key, value in dotenv_values(path, interpolate=False).items()
                          if value is not None}
            else:
                values = {}
                for line in path.read_text().splitlines():
                    if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        values[key.strip()] = value.strip()
            normalized = normalize_environment(values)
            if managed and (normalized.get('COLONY_STATE_DIR') != selected
                            or normalized.get('COLONY_INSTALL_PROFILE') != 'local'):
                raise ValueError('Selected private environment is incomplete or points to a different instance')
            for key, value in values.items():
                if managed:
                    # A selected private file owns both spellings of each field;
                    # inherited aliases must not retain another instance's key.
                    if key.startswith(('APSIMO_', 'COLONY_')):
                        suffix = key[7:]
                        os.environ.pop('APSIMO_' + suffix, None)
                        os.environ.pop('COLONY_' + suffix, None)
                    os.environ[key] = value
                else:
                    alias = ('COLONY_' + key[7:] if key.startswith('APSIMO_') else
                             'APSIMO_' + key[7:] if key.startswith('COLONY_') else key)
                    if alias not in os.environ:
                        os.environ.setdefault(key, value)
            break
    apply_environment_aliases()
