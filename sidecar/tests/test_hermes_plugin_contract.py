"""Contract test: the Hermes colony plugin's endpoints must exist in the host API.

The plugin (integrations/hermes/colony) lives in this repo precisely so its
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

# Every router the app mounts under /v1/host — the plugin may hit any of them.
_HOST_ROUTERS = (host_router, task_queue_router, observations_router)

_INTEGRATION = pathlib.Path(__file__).resolve().parents[2] / "integrations" / "hermes" / "colony"


def _normalize(path: str) -> str:
    path = path.split("?", 1)[0]              # drop query string
    path = path.rstrip("/")                   # ignore trailing slash
    return re.sub(r"\{[^}]*\}", "{}", path)   # path params (incl f-string exprs) -> {}


def _plugin_paths() -> set[str]:
    """Every /v1/host/... path the plugin source references, normalized.

    The char class allows braces, brackets and quotes so an f-string segment like
    {args['initiative_id']} is captured whole, then collapsed to {} by _normalize.
    """
    paths: set[str] = set()
    for f in sorted(_INTEGRATION.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for raw in re.findall(r"/v1/host/[A-Za-z0-9/_.\-{}\[\]']*", src):
            if "..." in raw:        # prose ellipsis in a docstring/prompt, not a real path
                continue
            paths.add(_normalize(raw))
    return paths


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
