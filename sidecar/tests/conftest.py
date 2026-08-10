"""Root pytest configuration.

The ``e2e`` and ``integration`` suites talk to a *running* Colony sidecar over
HTTP (httpx against ``COLONY_URL``). They are skipped unless a live sidecar is
configured (``COLONY_URL`` + ``COLONY_API_KEY``) and actually reachable, so the
default ``pytest tests/`` run exercises the unit suite for real and is
honest-green without any external services.

This replaces an earlier ``tests/integration/conftest.py`` hook that skipped the
*entire* session (not just integration tests) whenever ``COLONY_URL`` was unset —
which silently skipped every unit test in CI.
"""

import atexit
import os
import shutil
import tempfile

import pytest

# Collection imports the production ASGI module, which normally loads the
# operator's ~/.colony/.env and opens state stores immediately.  Unit tests
# must never inherit credentials or mutate live state, and release trees may
# intentionally be read-only.  Establish an isolated, writable process state
# before any test module is imported.
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="colony-pytest-")
atexit.register(shutil.rmtree, _TEST_STATE_DIR, ignore_errors=True)
_TEST_HOME_DIR = os.path.join(_TEST_STATE_DIR, "home")
os.mkdir(_TEST_HOME_DIR, mode=0o700)
os.environ["COLONY_TEST_HOME"] = _TEST_HOME_DIR
os.environ["HOME"] = _TEST_HOME_DIR
for variable, leaf in (
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_CACHE_HOME", "cache"),
):
    directory = os.path.join(_TEST_HOME_DIR, leaf)
    os.mkdir(directory, mode=0o700)
    os.environ[variable] = directory
os.environ["COLONY_SKIP_DOTENV"] = "1"
os.environ["COLONY_STATE_DIR"] = _TEST_STATE_DIR
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# Directories (relative to this file) whose tests require a live sidecar.
_LIVE_DIRS = ("e2e", "integration")


def _live_sidecar_status() -> tuple[bool, str]:
    """Return (enabled, skip_reason) for the live-sidecar test suites."""
    url = os.environ.get("COLONY_URL")
    key = os.environ.get("COLONY_API_KEY")
    if not url or not key:
        return False, (
            "live sidecar tests skipped — set COLONY_URL and COLONY_API_KEY "
            "to a running sidecar to run the e2e/integration suites"
        )
    try:
        import httpx

        # Any HTTP response (even 401/404) means a sidecar is listening.
        httpx.get(url, timeout=2.0)
        return True, ""
    except Exception:
        return False, f"live sidecar tests skipped — no sidecar reachable at {url}"


def pytest_collection_modifyitems(config, items):
    enabled, reason = _live_sidecar_status()
    if enabled:
        return
    tests_root = os.path.dirname(__file__)
    live_roots = tuple(os.path.join(tests_root, d) + os.sep for d in _LIVE_DIRS)
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        path = str(getattr(item, "path", "") or getattr(item, "fspath", ""))
        # str.startswith accepts a tuple of prefixes.
        if path.startswith(live_roots):
            item.add_marker(skip)
