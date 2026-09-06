"""Shared bounded reranking for authorized belief and source candidates."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any, Dict, List


class RecallSelector:
    def __init__(self, rerank_fn=None, *, calibration_metadata=None, logger=None):
        self._rerank_fn = rerank_fn
        self._rerank_calibration_metadata = calibration_metadata
        self.logger = logger or logging.getLogger(__name__)

    async def select_context(self, query, beliefs, quotations, *, limit=5, max_chars=6000):
        """One rank fusion, reranking pass and budget after authority checks."""
        from .recall import fuse_candidates, pack_memory_context
        beliefs = [dict(row, kind="belief") for row in beliefs]
        # Confidence in a belief and certainty that words were quoted are not
        # comparable truth scores. Preserve them as evidence metadata; select
        # across kinds by rank and semantic relevance alone.
        candidates = fuse_candidates(
            beliefs, quotations, limit=len(beliefs) + len(quotations),
            confidence_weighting=False)
        ranked = await self.rerank(
            query, candidates, limit, confidence_weighting=False)
        ranked.sort(key=lambda row: row.get("relevance", 0), reverse=True)
        return pack_memory_context(ranked, limit=limit, max_chars=max_chars)

    async def rerank(
        self,
        query: str,
        memories: List[Dict[str, Any]],
        limit: int,
        *,
        strength_ranking: bool = False,
        confidence_weighting: bool = True,
    ) -> List[Dict[str, Any]]:
        """Cross-encoder rerank of filtered recall candidates, bounded and
        fail-open.

        COLONY_RECALL_RERANK gates it: ``off`` (default) never calls the
        reranker; ``shadow`` scores and logs the rank delta but returns the
        candidate order untouched (measure p95 before flipping); ``on`` replaces the
        vector score in the relevance blend with the rerank score. The call
        is inline but hard-capped by COLONY_RECALL_RERANK_TIMEOUT_MS
        (default 1200). On timeout or error, recall keeps the candidate order
        and marks selection unavailable. A candidate set that already fits
        skips reranking only when no calibrated abstention cutoff is active.
        """
        mode = os.environ.get("COLONY_RECALL_RERANK", "off").strip().lower()
        if mode not in ("shadow", "on"):
            return memories
        min_score = None
        raw_min_score = os.environ.get("COLONY_RECALL_RERANK_MIN_SCORE", "").strip()
        if raw_min_score:
            try:
                value = float(raw_min_score)
                if math.isfinite(value):
                    min_score = value
            except (TypeError, ValueError):
                pass
        if min_score is not None:
            from .recall import calibration_fingerprint
            metadata_fn = getattr(self, "_rerank_calibration_metadata", None)
            try:
                metadata = metadata_fn() if metadata_fn is not None else None
                actual = calibration_fingerprint(metadata) if metadata else None
            except Exception:
                metadata, actual = None, None
            expected = os.environ.get("COLONY_RECALL_RERANK_CALIBRATION", "").strip()
            if not actual or not expected or actual != expected:
                status = "mismatch" if expected and actual else "unverified"
                min_score = None
                warning_key = (actual, expected)
                if getattr(self, "_rerank_calibration_warned", None) != warning_key:
                    self._rerank_calibration_warned = warning_key
                    self.logger.warning("Rerank abstention calibration %s; threshold disabled (current configuration: %s)", status, actual or "unknown")
            else:
                status = ("configuration_verified" if metadata.get("weights_revision")
                          and metadata["weights_revision"] != "unverified"
                          else "configuration_verified_weights_unverified")
            for memory in memories:
                memory["rerank_calibration"] = status
        rerank_fn = getattr(self, "_rerank_fn", None)
        if rerank_fn is None:
            for memory in memories:
                memory["rerank_status"] = "unavailable"
        if rerank_fn is None or not memories or (len(memories) <= limit and min_score is None):
            return memories
        try:
            timeout_ms = float(os.environ.get(
                "COLONY_RECALL_RERANK_TIMEOUT_MS", "1200"))
        except (TypeError, ValueError):
            timeout_ms = 1200.0

        docs = [str(m.get("ranking_text", m.get("content", ""))) for m in memories]
        try:
            results = await asyncio.wait_for(
                rerank_fn(query, docs, top_k=len(docs)),
                timeout=max(timeout_ms, 1.0) / 1000.0,
            )
        except Exception as exc:
            self._warn_rerank_failure(exc)
            for memory in memories:
                memory["rerank_status"] = "unavailable"
            return memories

        scores: Dict[int, float] = {}
        for r in results or []:
            idx = r.get("index") if isinstance(r, dict) else getattr(r, "index", None)
            score = r.get("score") if isinstance(r, dict) else getattr(r, "score", None)
            if idx is not None and score is not None:
                idx, score = int(idx), float(score)
                if 0 <= idx < len(memories) and math.isfinite(score):
                    scores[idx] = score
        if not scores:
            self._warn_rerank_failure(RuntimeError("reranker returned no scores"))
            for memory in memories:
                memory["rerank_status"] = "unavailable"
            return memories

        if mode == "shadow":
            ann_top = [m.get("id") for m in sorted(
                memories, key=lambda m: m.get("relevance", 0),
                reverse=True)][:limit]
            rr_idx = sorted(range(len(memories)),
                            key=lambda i: scores.get(i, float("-inf")),
                            reverse=True)
            rr_top = [memories[i].get("id") for i in rr_idx[:limit]]
            moved = sum(1 for a, b in zip(ann_top, rr_top) if a != b)
            self.logger.info(
                "recall rerank shadow: candidates=%d limit=%d "
                "top_overlap=%d/%d positions_changed=%d",
                len(memories), limit, len(set(ann_top) & set(rr_top)),
                limit, moved)
            return memories

        # mode == "on": rerank score replaces the vector score in the blend;
        # a document the reranker didn't score keeps its ANN relevance.
        for i, mem in enumerate(memories):
            if i not in scores:
                continue
            effective_confidence = (float(
                mem.get("effective_confidence", mem.get("strength", 1.0)))
                if confidence_weighting else 1.0)
            relevance = scores[i] * effective_confidence
            if strength_ranking:
                relevance *= 0.5 + 0.5 * float(mem.get("strength", 1.0))
            mem["relevance"] = relevance
            mem["rerank_score"] = scores[i]
            mem["rerank_status"] = "scored"
        if min_score is not None:
            # Do not fill the context window with unrelated passages merely
            # because there are fewer candidates than requested. Calibration
            # belongs to the serving model/deployment, not a universal constant.
            memories = [mem for i, mem in enumerate(memories)
                        if i in scores and scores[i] >= min_score]
        return memories

    def _warn_rerank_failure(self, exc: BaseException) -> None:
        """Warn on rerank failure at most once per ~5 minutes (fail-open is
        by design; a dead reranker must not turn every recall into a WARNING
        stream)."""
        now = time.monotonic()
        # Sentinel must be None, not 0.0: time.monotonic() is measured from an
        # arbitrary origin (system boot on Linux), so a 0.0 default suppresses
        # the FIRST warning entirely for the first 300s of uptime.
        last = getattr(self, "_rerank_warn_at", None)
        if last is None or now - last >= 300:
            self._rerank_warn_at = now
            self.logger.warning(
                "recall rerank failed (fail-open to ANN order): %s", exc)
        else:
            self.logger.debug(
                "recall rerank failed (fail-open to ANN order): %s", exc)
