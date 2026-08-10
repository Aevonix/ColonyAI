"""Fail-closed slash surface for the governed Colony plugin.

Hermes slash callbacks do not carry the transport/tool-call attestation needed
to construct a ``HermesToolActionIntentV1``.  They remain registered as explicit
disabled notices so an old operator habit cannot silently reach a legacy
mutation helper.
"""

from __future__ import annotations


_DISABLED = (
    "Disabled: use the governed colony_* tools in an attested turn or the "
    "Operator Deck action plane."
)


def _disabled(_args: str = "") -> str:
    return _DISABLED


SLASH_COMMANDS = {
    "autonomy disable": _disabled,
    "autonomy enable": _disabled,
    "autonomy status": _disabled,
    "context": _disabled,
    "events": _disabled,
    "goals": _disabled,
    "status": _disabled,
    "sync": _disabled,
}


__all__ = ["SLASH_COMMANDS"]
