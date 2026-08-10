"""Release tests must never observe the operator's real home directory."""

import os
from pathlib import Path


def test_pytest_process_has_an_explicit_isolated_home():
    isolated_home = os.environ.get("COLONY_TEST_HOME")
    assert isolated_home, "the root test harness must declare its isolated HOME"
    assert Path.home().resolve() == Path(isolated_home).resolve()
    assert Path(isolated_home).is_dir()

