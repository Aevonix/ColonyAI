"""Filesystem documents connector (read-only): watches a folder for new or
changed files and normalizes them into "document" observations + entity hints.

Config env-only (COLONY_CONNECTOR_FS_*): PATH (folder), EXTENSIONS (csv),
MAX. Only reads file metadata + a short text snippet (for the world-model
extractor); never writes or deletes.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, List

from colony_sidecar.connectors.base import Connector, EntityHint, Observation

logger = logging.getLogger(__name__)

_TEXT_EXTS = {".txt", ".md", ".rst", ".csv", ".json", ".log"}


class FSDocumentsConnector(Connector):
    name = "fs"
    domain = "document"
    default_poll_secs = 600

    def normalize(self, files: List[Dict[str, Any]]) -> List[Observation]:
        out: List[Observation] = []
        for f in files:
            path = f.get("path", "")
            fname = f.get("name", "") or os.path.basename(path)
            snippet = (f.get("snippet", "") or "")[:400]
            eid = hashlib.sha1(path.encode("utf-8")).hexdigest()[:16] if path else fname
            text = f"Document '{fname}'" + (f": {snippet}" if snippet else "")
            out.append(Observation(
                domain=self.domain, external_id=eid,
                ts=float(f.get("mtime", 0.0)),
                payload={"path": path, "name": fname, "size": f.get("size", 0),
                         "mtime": f.get("mtime", 0.0), "snippet": snippet},
                entities=[EntityHint(kind="document", name=fname,
                                     external_ids={"path": path})],
                text=text))
        return out

    def _fetch(self) -> List[Dict[str, Any]]:
        folder = self.config.get("PATH")
        if not folder or not os.path.isdir(folder):
            return []
        exts = {e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
                for e in self.config.get("EXTENSIONS", "txt,md,pdf,docx").split(",")
                if e.strip()}
        limit = self.config.get_int("MAX", 50)
        since = self._last_poll
        found: List[Dict[str, Any]] = []
        for root, _dirs, names in os.walk(folder):
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if exts and ext not in exts:
                    continue
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_mtime <= since:
                    continue
                snippet = ""
                if ext in _TEXT_EXTS:
                    try:
                        with open(p, "r", errors="replace") as fh:
                            snippet = fh.read(600)
                    except Exception:
                        pass
                found.append({"path": p, "name": n, "size": st.st_size,
                              "mtime": st.st_mtime, "snippet": snippet})
        found.sort(key=lambda d: d["mtime"], reverse=True)
        return found[:limit]

    def poll(self) -> List[Observation]:
        try:
            return self.normalize(self._fetch())
        except Exception:
            logger.debug("fs poll failed", exc_info=True)
            return []
