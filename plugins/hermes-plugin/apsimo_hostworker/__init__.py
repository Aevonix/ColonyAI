"""Bridge the plugin package to the authoritative host-worker catalog.

In a source checkout, the package path below points at the repository's one
``colony_hostworker`` implementation.  The Hermes installers copy the two
stdlib catalog modules beside this file, so an installed plugin reads the same
catalog snapshot without depending on the sidecar package.
"""

from pathlib import Path


__path__.append(  # type: ignore[name-defined]
    str(
        Path(__file__).parent.parent.parent.parent
        / "hostworker"
        / "colony_hostworker"
    )
)
