"""Install instance guidance and diagnostic skills for supported harnesses."""
from .context import PACOMIND_CONTEXT_TEMPLATE, write_pacomind_context
from .skills import (
    PACOMIND_CHECK_SKILL, PACOMIND_DIAGNOSTIC_SKILL,
    get_skill_config_path, get_skill_path, remove_pacomind_skill,
    write_pacomind_check_skill, write_pacomind_skill,
)

__all__ = [
    "PACOMIND_CONTEXT_TEMPLATE", "write_pacomind_context",
    "PACOMIND_CHECK_SKILL", "PACOMIND_DIAGNOSTIC_SKILL",
    "get_skill_config_path", "get_skill_path", "remove_pacomind_skill",
    "write_pacomind_check_skill", "write_pacomind_skill",
]
