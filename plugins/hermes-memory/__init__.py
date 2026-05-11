"""Colony memory provider plugin for Hermes."""
from .provider import ColonyMemoryProvider

__all__ = ["ColonyMemoryProvider"]


def register(ctx):
    """Plugin-style registration for Hermes's memory provider discovery.

    Supports both the class-discovery fallback and the register(ctx) pattern.
    """
    ctx.register_memory_provider(ColonyMemoryProvider())
