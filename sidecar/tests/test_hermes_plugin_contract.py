"""Contract test: the Hermes colony plugin's endpoints must exist in the host API.

The plugin (plugins/hermes-plugin) lives in this repo precisely so its
tool->endpoint mappings stay in lockstep with the API. This test auto-discovers
every `/v1/host/...` path the plugin references and asserts each one matches a
registered route. It exists because a `colony_task_*` tool quietly called a
removed `/v1/host/tasks/...` endpoint for a long time, failing silently in
production — exactly the drift this catches at test time.
"""

import pathlib
import re

import pytest

from colony_sidecar.api.routers.host import router as host_router
from colony_sidecar.api.routers.observations import router as observations_router
from colony_sidecar.api.routers.task_queue import router as task_queue_router
from colony_sidecar.api.routers.executions import router as executions_router
from colony_sidecar.api.routers.commitment_work import router as commitment_work_router
from colony_sidecar.api.routers.initiative_work import router as initiative_work_router

from colony_sidecar.api.routers import social_state, temporal_followups, transport, followup_plans

# Every router the app mounts under /v1/host — the plugin may hit any of them.
_HOST_ROUTERS = (host_router, task_queue_router, observations_router, executions_router, commitment_work_router, initiative_work_router, social_state.router, temporal_followups.router, transport.router, followup_plans.router)

_INTEGRATION = pathlib.Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"
_HOST_PATH = re.compile(r"/v1/host/(?:[A-Za-z0-9/_.-]|\{[^{}\r\n]*\})*")


def _normalize(path: str) -> str:
    path = path.split("?", 1)[0]              # drop query string
    path = path.rstrip("/")                   # ignore trailing slash
    return re.sub(r"\{[^}]*\}", "{}", path)   # path params (incl f-string exprs) -> {}


def _plugin_paths() -> set[str]:
    """Every /v1/host/... path the plugin source references, normalized.

    Quoted expressions inside f-string braces remain intact, while the closing
    quote of either a single- or double-quoted path is not part of the URL.
    """
    paths: set[str] = set()
    for f in sorted(_INTEGRATION.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for raw in _HOST_PATH.findall(src):
            if "..." in raw:        # prose ellipsis in a docstring/prompt, not a real path
                continue
            paths.add(_normalize(raw))
    return paths


@pytest.mark.parametrize("source,expected", [
    ("client.post('/v1/host/memory/sources/forget', timeout=3)", "/v1/host/memory/sources/forget"),
    ('client.get("/v1/host/autonomy/status")', "/v1/host/autonomy/status"),
    ('client.post(f"/v1/host/initiatives/{args[\'initiative_id\']}/finish")', "/v1/host/initiatives/{}/finish"),
])
def test_path_discovery_keeps_expression_quotes_only(source, expected):
    assert [_normalize(path) for path in _HOST_PATH.findall(source)] == [expected]


def _api_paths() -> set[str]:
    return {
        _normalize(r.path)
        for router in _HOST_ROUTERS
        for r in router.routes
        if getattr(r, "path", "").startswith("/v1/host")
    }


def test_integration_dir_present():
    assert _INTEGRATION.is_dir(), f"colony Hermes integration missing at {_INTEGRATION}"


def test_plugin_endpoints_all_exist_in_host_api():
    plugin = _plugin_paths()
    assert plugin, "found no /v1/host paths in the plugin — regex or layout changed?"
    api = _api_paths()
    missing = sorted(p for p in plugin if p not in api)
    assert not missing, (
        "Hermes colony plugin references endpoints that are NOT registered in the host "
        f"API (contract drift — these will 404/405 silently in production):\n  {missing}\n"
        f"Plugin paths checked: {sorted(plugin)}"
    )
