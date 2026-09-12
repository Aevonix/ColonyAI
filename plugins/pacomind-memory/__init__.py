"""PacoMind memory provider plugin for Hermes."""
# Hermes also loads installed providers under a synthetic source namespace.
# Reuse the canonical class only when it is this exact source. A selected
# standalone profile must not silently import a different installed version.
from importlib.util import find_spec
from pathlib import Path

_canonical = find_spec("pacomind_memory")
if _canonical is not None and _canonical.origin and Path(_canonical.origin).resolve() == Path(__file__).resolve():
    from pacomind_memory.provider import PacoMindMemoryProvider
else:
    from .provider import PacoMindMemoryProvider

__all__ = ["PacoMindMemoryProvider"]


def register(ctx):
    """Register the PacoMind memory provider + a pre_llm_call lifecycle hook.

    The hook lives HERE (a Hermes plugin), not in Hermes core, so it survives
    Hermes updates — Hermes core lives under hermes-agent/ and is replaced on
    update, while ~/.hermes/plugins/ is not.

    Hermes injects returned ``str`` / ``{"context": str}`` into the user
    message's API content, which it also retains for later history replay.
    ``pre_llm_call`` is the only lifecycle hook that carries the message sender
    (``sender_id``) and ``platform``, so BOTH contact resolution and current-time
    injection happen here (the old ``agent:start`` hook is not a valid hook name
    in this Hermes build and was silently dropped).

    - resolve the real contact from the sender → per-contact memory/affect/facts
      engage instead of 'default'
    - attach a clock scoped to this user turn, so retained clocks are not
      mistaken for the present time on later turns
    """
    provider = PacoMindMemoryProvider()
    ctx.register_memory_provider(provider)

    def _pre_llm_call(**kwargs):
        # 1) Resolve the real PacoMind contact from the message sender (cached per
        #    sender inside the provider) so per-contact memory engages.
        try:
            provider.resolve_contact(
                platform=str(kwargs.get("platform", "") or ""),
                user_id=str(kwargs.get("sender_id", "") or ""),
            )
        except Exception:
            pass
        # 2) Scope the clock to its owning turn: native api_content is replayed.
        try:
            context = provider._turn_clock_context()
            if context:
                return {"context": context}
        except Exception:
            pass
        return None

    if hasattr(ctx, "register_hook"):
        try:
            from hermes_cli.plugins import VALID_HOOKS as _VALID
        except Exception:
            _VALID = None
        if _VALID is None or "pre_llm_call" in _VALID:
            try:
                ctx.register_hook("pre_llm_call", _pre_llm_call)
            except Exception:
                pass
