"""Compatibility import; all runtime state belongs to apsimo_hermes."""
from apsimo_hermes.compat import register_module_alias

register_module_alias(__name__, 'apsimo_hermes')
register_module_alias('colony_hermes.colony_hostworker', 'apsimo_hermes.apsimo_hostworker')
