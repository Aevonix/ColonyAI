#!/usr/bin/env python3
"""Inert marker for the retired direct-message activity monitor."""

from __future__ import annotations

import sys


LEGACY_EFFECT_WORKER_DISABLED = True


def main() -> int:
    print(
        "colony-activity-monitor: disabled; route operator notifications "
        "through the governed action plane",
        file=sys.stderr,
    )
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
