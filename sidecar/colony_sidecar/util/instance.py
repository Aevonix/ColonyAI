"""Load one private instance, retaining legacy deployment environment rules."""
from pathlib import Path
import os


def load_environment():
    if os.environ.get('COLONY_SKIP_DOTENV', '').lower() in {'1', 'true', 'yes', 'on'}:
        return
    selected = os.environ.get('COLONY_STATE_DIR')
    explicitly_selected = os.environ.get('COLONY_INSTANCE_SELECTED') == '1'
    if not selected:
        # A native profile owns its Colony binding. No scan of other profiles.
        import yaml
        home = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes').expanduser()
        config_path = home / 'config.yaml'
        if config_path.is_file():
            config = yaml.safe_load(config_path.read_text()) or {}
            plugins = config.get('plugins') if isinstance(config, dict) else None
            colony = plugins.get('colony') if isinstance(plugins, dict) else None
            selected = colony.get('instance_dir') if isinstance(colony, dict) else None
            explicitly_selected = bool(selected)
    managed = bool(selected and (explicitly_selected or
        (Path(selected).expanduser()/'instance.json').is_file()))
    if managed:
        selected = str(Path(selected).expanduser().resolve())
        os.environ['COLONY_STATE_DIR'] = selected
    # Existing wrappers set ~/.colony/data as state but read ~/.colony/.env.
    # That legacy case keeps launch environment precedence via setdefault.
    paths = [Path(selected)/'.env'] if managed else [Path.home()/'.colony'/'.env', Path.cwd()/'.env']
    for path in paths:
        if path.is_file():
            values = {}
            for line in path.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    values[key.strip()] = value.strip()
            if managed and values.get('COLONY_STATE_DIR', selected) != selected:
                raise ValueError('Selected instance environment points to a different state directory')
            for key, value in values.items():
                if managed:
                    os.environ[key] = value
                else:
                    os.environ.setdefault(key, value)
            break
