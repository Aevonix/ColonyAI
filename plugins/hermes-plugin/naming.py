"""Project native discovery names while retaining historical wire operations."""
from copy import deepcopy
from functools import wraps
from collections.abc import Mapping


def preferred(name):
    if name == 'colony':
        return 'apsimo'
    return 'apsimo'+name[6:] if name.startswith(('colony_', 'colony-')) else name


def operation(name):
    if name == 'apsimo':
        return 'colony'
    return 'colony'+name[6:] if name.startswith(('apsimo_', 'apsimo-')) else name


def preferred_schema(schema):
    result = deepcopy(schema)
    result['name'] = preferred(result['name'])
    if isinstance(result.get('description'), str):
        result['description'] = result['description'].replace('colony_', 'apsimo_').replace('Colony', 'Apsimo')
    return result


def plugin_configuration(config):
    plugins = config.get('plugins', {})
    canonical, legacy = plugins.get('apsimo'), plugins.get('colony')
    if isinstance(canonical, Mapping) and isinstance(legacy, Mapping) and canonical != legacy:
        raise ValueError('Conflicting Apsimo and legacy Colony plugin settings')
    value = canonical if isinstance(canonical, Mapping) else legacy
    result = dict(value) if isinstance(value, Mapping) else {}
    for key in ('enabled_action_tools', 'enabled_message_tools', 'enabled_read_tools'):
        if isinstance(result.get(key), list):
            result[key] = [operation(name) if isinstance(name, str) else name for name in result[key]]
        elif isinstance(result.get(key), str):
            result[key] = ','.join(operation(name.strip()) for name in result[key].split(','))
    return result


def selected_name(config):
    plugins = config.get('plugins', {})
    plugin_configuration(config)
    enabled = plugins.get('enabled', [])
    if isinstance(enabled, list):
        selected = {name for name in enabled if name in ('apsimo', 'colony')}
        if len(selected) == 1:
            return selected.pop()
    return 'apsimo' if 'apsimo' in plugins or 'colony' not in plugins else 'colony'


def legacy_native_configuration(config):
    """Read either spelling through unchanged internal profile invariants."""
    result = deepcopy(config)
    plugins = result.setdefault('plugins', {})
    owned = plugin_configuration(result)
    plugins.pop('apsimo', None)
    plugins['colony'] = owned
    if isinstance(plugins.get('enabled'), list):
        plugins['enabled'] = [operation(name) for name in plugins['enabled']]
    if isinstance(result.get('toolsets'), list):
        result['toolsets'] = [operation(name) for name in result['toolsets']]
    for platform, names in result.get('platform_toolsets', {}).items():
        if isinstance(names, list):
            result['platform_toolsets'][platform] = [operation(name) for name in names]
    return result


class NativeNames:
    """Use one native plugin selection and one set of model-visible tools.

    Only native tool metadata is renamed. Handlers, retained action intents,
    source IDs and stored task bodies keep their existing operation names.
    """
    def __init__(self, context, canonical):
        self.context, self.canonical = context, canonical

    def __getattr__(self, name):
        return getattr(self.context, name)

    def register_tool(self, **kwargs):
        if self.canonical:
            kwargs = dict(kwargs)
            kwargs['name'] = preferred(kwargs['name'])
            kwargs['toolset'] = preferred(kwargs.get('toolset', 'colony'))
            if kwargs.get('schema'):
                kwargs['schema'] = preferred_schema(kwargs['schema'])
        return self.context.register_tool(**kwargs)

    def _callback(self, callback):
        if not self.canonical:
            return callback
        @wraps(callback)
        def invoke(*args, **kwargs):
            if isinstance(kwargs.get('tool_name'), str):
                kwargs = dict(kwargs, tool_name=operation(kwargs['tool_name']))
            return callback(*args, **kwargs)
        return invoke

    def register_hook(self, name, callback, *args, **kwargs):
        return self.context.register_hook(name, self._callback(callback), *args, **kwargs)

    def register_middleware(self, name, callback, *args, **kwargs):
        return self.context.register_middleware(name, self._callback(callback), *args, **kwargs)
