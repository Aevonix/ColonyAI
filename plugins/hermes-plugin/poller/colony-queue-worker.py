#!/usr/bin/env python3
"""Inert compatibility target for the retired Hermes queue effect worker.

The former wrapper could claim and close lifecycle work outside the action
mediator.  Keeping an inert file at the legacy path makes surviving scheduled
entries harmless while operators remove them.
"""

from __future__ import annotations

import sys


LEGACY_EFFECT_WORKER_DISABLED = True
_EXIT_DISABLED = 78


def main() -> int:
    print(
        "colony-queue-worker: disabled; remove the legacy scheduled entry and "
        "use the governed action plane",
        file=sys.stderr,
    )
    return _EXIT_DISABLED


if __name__ == "__main__":
    raise SystemExit(main())
