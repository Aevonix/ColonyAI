"""Runtime wheels omit repository tests and retain importable product code."""
import os
import zipfile

from conftest import ROOT, run_python


def test_sidecar_wheel_contains_product_without_embedded_tests(tmp_path):
    run_python("-m", "build", "--wheel", "--no-isolation", "--outdir", tmp_path,
               cwd=ROOT / "sidecar")
    wheel = next(tmp_path.glob("pacomind-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not [name for name in names if any(
            part == "tests" or part.startswith("test_") or part == "conftest.py"
            for part in name.split("/"))]
        for name in ("briefings/aggregators.py", "events/bus.py",
                     "intelligence/components/tool_learner.py",
                     "intelligence/synthesis/insight_deliverer.py"):
            assert "pacomind/" + name in names
    installed = tmp_path / "installed"
    run_python("-m", "pip", "install", "--no-deps", "--no-index", "--target", installed,
               wheel, cwd=tmp_path)
    env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    env.update(HOME=str(tmp_path), PYTHONPATH=str(installed), PYTHON_DOTENV_DISABLED="1")
    run_python("-c", "from pacomind.briefings.aggregators import CalendarAggregator; "
               "from pacomind.events.bus import EventBus; "
               "from pacomind.intelligence.components.tool_learner import ToolLearner; "
               "from pacomind.intelligence.synthesis.insight_deliverer import InsightDeliverer",
               cwd=tmp_path, env=env)
