"""Colony memory provider plugin for Hermes."""
from .provider import ColonyMemoryProvider

__all__ = ["ColonyMemoryProvider"]


def register(ctx):
    """Register the Colony memory provider + a pre_llm_call lifecycle hook.

    The hook lives HERE (a Hermes plugin), not in Hermes core, so it survives
    Hermes updates — Hermes core lives under hermes-agent/ and is replaced on
    update, while ~/.hermes/plugins/ is not.

    Hermes (v0.15.x) invokes hook callbacks SYNCHRONOUSLY as ``cb(**kwargs)`` and
    injects any returned ``str`` / ``{"context": str}`` into the user message.
    ``pre_llm_call`` is the only lifecycle hook that carries the message sender
    (``sender_id``) and ``platform``, so BOTH contact resolution and current-time
    injection happen here (the old ``agent:start`` hook is not a valid hook name
    in this Hermes build and was silently dropped).

    - resolve the real contact from the sender → per-contact memory/affect/facts
      engage instead of 'default'
    - inject the authoritative current date/time so the agent never anchors on the
      (cached, stale) session-start date in long-running sessions
    """
    provider = ColonyMemoryProvider()
    ctx.register_memory_provider(provider)

    def _pre_llm_call(**kwargs):
        # 1) Resolve the real Colony contact from the message sender (cached per
        #    sender inside the provider) so per-contact memory engages.
        try:
            provider.resolve_contact(
                platform=str(kwargs.get("platform", "") or ""),
                user_id=str(kwargs.get("sender_id", "") or ""),
            )
        except Exception:
            pass
        # 2) Inject the authoritative current date/time as ephemeral user context.
        try:
            line = provider._current_time_line()
            if line:
                return {
                    "context": (
                        f"\u23f0 CURRENT DATE & TIME, right now: {line}. This is TODAY "
                        "\u2014 greet and reason from THIS. Any 'Conversation started' "
                        "date in your prompt is only when this long-running session "
                        "began (often days ago), NOT today."
                    )
                }
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
