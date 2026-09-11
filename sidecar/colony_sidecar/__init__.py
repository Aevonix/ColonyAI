"""Compatibility import for the renamed Apsimo distribution."""
from apsimo.compat import register_module_alias

register_module_alias(__name__, 'apsimo')
