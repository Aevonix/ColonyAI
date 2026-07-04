"""Read-only local repo mirrors for Colony's own research (best-of-B).

Owner-designated repos (env/config only, never hardcoded) are cloned/pulled
into a local mirror directory. Colony gets low-latency read access -- list,
read, search -- with NO write credentials anywhere. Every access is
boundary-gated: a standing "leave X alone" blocks even reads of X.

Config: COLONY_REPO_MIRRORS = "name=url[|alias1|alias2][,name2=url2...]"
Mirror dir: <state_dir>/repo-mirrors/<name>
"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_READ_BYTES = 64 * 1024
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def parse_mirror_config(raw: str) -> Dict[str, Dict[str, str]]:
    """Parse COLONY_REPO_MIRRORS into {name: {url, aliases}}."""
    out: Dict[str, Dict[str, str]] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, rest = part.split("=", 1)
        pieces = rest.split("|")
        url = pieces[0].strip()
        aliases = "|".join(a.strip() for a in pieces[1:] if a.strip())
        if name.strip() and url:
            out[name.strip()] = {"url": url, "aliases": aliases}
    return out


class RepoMirrorManager:
    def __init__(
        self,
        mirror_dir: str,
        config: Optional[Dict[str, Dict[str, str]]] = None,
        directive_manager: Any = None,
    ) -> None:
        self._dir = Path(mirror_dir)
        self._config = config if config is not None else parse_mirror_config(
            os.environ.get("COLONY_REPO_MIRRORS", ""))
        self._directives = directive_manager
        self._last_refresh: Dict[str, float] = {}

    def configured(self) -> Dict[str, Dict[str, str]]:
        return dict(self._config)

    def path_for(self, name: str) -> Optional[str]:
        if name not in self._config:
            return None
        p = self._dir / name
        return str(p) if (p / ".git").exists() or (p / "HEAD").exists() else None

    # -- boundary gate --------------------------------------------------
    def _boundary_ok(self, name: str, extra: str = "") -> Any:
        if self._directives is None:
            return None
        try:
            from colony_sidecar.directives import Action
            verdict = self._directives.check(Action(
                kind="repo_read", text=f"{name} {extra}", target=name))
            return verdict if not verdict.allowed else None
        except Exception:
            return None

    # -- sync -------------------------------------------------------------
    def refresh(self, name: str, min_interval_secs: float = 300.0) -> Dict[str, Any]:
        """Clone or pull one mirror (read-only; no credentials added)."""
        info = self._config.get(name)
        if info is None:
            return {"ok": False, "reason": "unknown_repo"}
        blocked = self._boundary_ok(name, "refresh")
        if blocked is not None:
            return {"ok": False, "reason": blocked.reason}
        now = time.time()
        if now - self._last_refresh.get(name, 0) < min_interval_secs and self.path_for(name):
            return {"ok": True, "action": "fresh"}
        self._dir.mkdir(parents=True, exist_ok=True)
        dest = self._dir / name
        try:
            if (dest / ".git").exists():
                subprocess.run(["git", "-C", str(dest), "fetch", "--all", "--prune"],
                               capture_output=True, text=True, timeout=120, check=True)
                subprocess.run(["git", "-C", str(dest), "reset", "--hard", "@{upstream}"],
                               capture_output=True, text=True, timeout=60, check=False)
                action = "pulled"
            else:
                subprocess.run(["git", "clone", info["url"], str(dest)],
                               capture_output=True, text=True, timeout=300, check=True)
                action = "cloned"
            self._last_refresh[name] = now
            return {"ok": True, "action": action}
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "")[:200]
            logger.warning("mirror refresh failed for %s: %s", name, err)
            return {"ok": False, "reason": err}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:200]}

    def refresh_all(self) -> Dict[str, Any]:
        return {name: self.refresh(name) for name in self._config}

    # -- read tools ---------------------------------------------------------
    def list_files(self, name: str, subpath: str = "", limit: int = 200) -> Dict[str, Any]:
        blocked = self._boundary_ok(name, subpath)
        if blocked is not None:
            return {"error": blocked.reason, "status": "boundary_refused"}
        root = self.path_for(name)
        if root is None:
            return {"error": f"repo {name!r} not mirrored", "status": "unavailable"}
        base = Path(root) / subpath.lstrip("/")
        base_r = base.resolve()
        if not str(base_r).startswith(str(Path(root).resolve())):
            return {"error": "path escapes repo", "status": "error"}
        if not base_r.exists():
            return {"error": f"no such path {subpath!r}", "status": "not_found"}
        files: List[str] = []
        for p in sorted(base_r.rglob("*")):
            rel_parts = p.relative_to(root).parts
            if any(part in _SKIP_DIRS for part in rel_parts):
                continue
            if p.is_file():
                files.append(str(p.relative_to(root)))
                if len(files) >= limit:
                    break
        return {"repo": name, "path": subpath or "/", "count": len(files), "files": files}

    def read_file(self, name: str, path: str, max_bytes: int = _MAX_READ_BYTES) -> Dict[str, Any]:
        blocked = self._boundary_ok(name, path)
        if blocked is not None:
            return {"error": blocked.reason, "status": "boundary_refused"}
        root = self.path_for(name)
        if root is None:
            return {"error": f"repo {name!r} not mirrored", "status": "unavailable"}
        f = (Path(root) / path.lstrip("/")).resolve()
        if not str(f).startswith(str(Path(root).resolve())):
            return {"error": "path escapes repo", "status": "error"}
        if not f.is_file():
            return {"error": f"no such file {path!r}", "status": "not_found"}
        try:
            data = f.read_bytes()[: max(1024, min(max_bytes, _MAX_READ_BYTES))]
            return {"repo": name, "path": path, "size": f.stat().st_size,
                    "content": data.decode("utf-8", errors="replace")}
        except Exception as exc:
            return {"error": str(exc)[:200], "status": "error"}

    def search(self, name: str, query: str, glob: str = "",
               max_results: int = 40) -> Dict[str, Any]:
        blocked = self._boundary_ok(name, query)
        if blocked is not None:
            return {"error": blocked.reason, "status": "boundary_refused"}
        root = self.path_for(name)
        if root is None:
            return {"error": f"repo {name!r} not mirrored", "status": "unavailable"}
        if not query:
            return {"error": "query required", "status": "error"}
        try:
            args = ["git", "-C", root, "grep", "-In", "--max-depth", "-1", "-e", query]
            if glob:
                args += ["--", glob]
            out = subprocess.run(args, capture_output=True, text=True, timeout=30)
            lines = [ln for ln in out.stdout.splitlines() if ln.strip()][:max_results]
            return {"repo": name, "query": query, "count": len(lines),
                    "matches": lines}
        except Exception as exc:
            return {"error": str(exc)[:200], "status": "error"}

    # -- world-model feed -----------------------------------------------------
    async def register_entities(self, world_store: Any) -> int:
        """Upsert each owner-designated repo as a Project entity (deterministic,
        config-driven; not extraction guesswork)."""
        if world_store is None:
            return 0
        n = 0
        try:
            from colony_sidecar.world_model.entities import ProjectEntity
            from colony_sidecar.world_model.sqlite.backend import _generate_id
            for name, info in self._config.items():
                existing = await world_store.get_entity_by_external_id("repo_name", name)
                if existing is not None:
                    continue
                ent = ProjectEntity(
                    id=_generate_id("we"), name=name, entity_type="project",
                    confidence=0.95,
                    external_ids={"repo_name": name},
                    properties={"kind": "repo", "url": info.get("url", "")},
                )
                await world_store.upsert_entity(ent)
                n += 1
        except Exception:
            logger.debug("repo entity registration failed", exc_info=True)
        return n
