"""Run the store conformance suite against the bundled reference store."""

from __future__ import annotations

import sys

from .harness import sqlite_harness
from .suite import run_store_conformance


def main() -> int:
    results = run_store_conformance(sqlite_harness)
    failures = 0
    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        line = "%s %s" % (marker, result.name)
        if result.detail:
            line += " — " + result.detail
        print(line)
        if not result.passed:
            failures += 1
    print(
        "%d/%d conformance cases passed" % (len(results) - failures, len(results))
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
