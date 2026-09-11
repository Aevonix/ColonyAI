"""Compatibility import for the renamed Apsimo hostworker distribution."""
from apsimo_hostworker.compat import register_module_alias

register_module_alias(__name__, "apsimo_hostworker")
