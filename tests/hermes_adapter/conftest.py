"""Build isolated adapter artifacts for native Hermes contract tests."""

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_python(*args, cwd, env=None):
    result = subprocess.run(
        [sys.executable, *map(str, args)], cwd=cwd, env=env,
        text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory):
    output = tmp_path_factory.mktemp("adapter-artifacts")
    run_python("-m", "build", "--no-isolation", "--outdir", output, cwd=ROOT)
    wheel = next(output.glob("*.whl"))
    source = next(output.glob("*.tar.gz"))
    installed = output / "installed"
    run_python(
        "-m", "pip", "install", "--no-deps", "--no-index", "--target",
        installed, wheel, cwd=output,
    )
    return output, wheel, source, installed
