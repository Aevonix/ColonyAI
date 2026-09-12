"""Load the selected private instance without mixing profile environments."""
from pathlib import Path
import json
import os



def plugin_settings(config):
    """Read the one platform binding without changing a configuration."""
    plugins = config.get('plugins', {}) if isinstance(config, dict) else {}
    if not isinstance(plugins, dict):
        raise ValueError('Hermes plugins must be a mapping')
    selected = plugins.get('pacomind')
    if selected is not None and not isinstance(selected, dict):
        raise ValueError('Hermes PacoMind plugin settings must be a mapping')
    return selected or {}


def load_environment():
    # Startup/explicit instance-selection boundary only. Runtime reads never
    # rewrite the process environment.
    selected_env = dict(os.environ)
    if (selected_env.get('PACOMIND_SKIP_DOTENV', '').lower() in {'1', 'true', 'yes', 'on'}
            and selected_env.get('PACOMIND_INSTANCE_SELECTED') != '1'):
        return
    selected = selected_env.get('PACOMIND_STATE_DIR')
    explicitly_selected = selected_env.get('PACOMIND_INSTANCE_SELECTED') == '1'
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
            raise ValueError('Selected private instance is incomplete or its Hermes binding changed') from None
    # Existing wrappers set ~/.pacomind/data as state but read ~/.pacomind/.env.
    paths = [Path(selected)/'.env'] if managed else [Path.home()/'.pacomind'/'.env', Path.cwd()/'.env']
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
            normalized = dict(values)
            if managed and (normalized.get('PACOMIND_STATE_DIR') != selected
                            or normalized.get('PACOMIND_INSTALL_PROFILE') != 'local'):
                raise ValueError('Selected private environment is incomplete or points to a different instance')
            for key, value in values.items():
                if managed:
                    os.environ[key] = value
                else:
                    os.environ.setdefault(key, value)
            break
