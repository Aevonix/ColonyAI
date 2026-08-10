#!/usr/bin/env python3
"""Inert compatibility target for the retired initiative webhook poller.

This filename is intentionally retained so an old scheduled invocation lands
on a deterministic no-op after upgrade.  Scheduling effects now enter through
the governed action mediator; this script never reads initiatives or invokes a
Hermes webhook.
"""

from __future__ import annotations

import sys


LEGACY_EFFECT_WORKER_DISABLED = True
_EXIT_DISABLED = 78


def main() -> int:
    print(
        "colony-initiative-poller: disabled; remove the legacy scheduled entry "
        "and use the governed action plane",
        file=sys.stderr,
    )
    return _EXIT_DISABLED


if __name__ == "__main__":
    raise SystemExit(main())
