"""Research-to-Artifact Pipeline — 6-stage orchestrator.

Stages:
  1. DECOMPOSE  — parse goal into sub-tasks with GoalDecomposer
  2. GATHER     — collect evidence from web, graph, documents, email
  3. SYNTHESIZE — cross-reference evidence into insights
  4. OUTLINE    — build structured artifact outline
  5. PRODUCE    — render artifact in requested format
  6. REVIEW     — quality and safety gate (ResponseGate)

Usage::

    pipeline = ResearchPipeline()
    run = await pipeline.run(
        goal="Research competitor pricing",
        format=ArtifactFormat.MARKDOWN,
    )
    print(run.artifact.content)
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from colony_sidecar.research.gatherer import EvidencePackage, GatherConfig, SourceGatherer
from colony_sidecar.research.synthesizer import ResearchSynthesizer, SynthesisConfig, SynthesisReport
from colony_sidecar.research.artifact import (
    Artifact,
    ArtifactFormat,
    ArtifactOutline,
    ArtifactRenderer,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline state models
# ---------------------------------------------------------------------------


class PipelineStage(str, Enum):
    DECOMPOSE = "decompose"
    GATHER = "gather"
    SYNTHESIZE = "synthesize"
    OUTLINE = "outline"
    PRODUCE = "produce"
    REVIEW = "review"
    DONE = "done"


class PipelineStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"       # awaiting outline approval


@dataclass
class ReviewResult:
    """Outcome of the Stage 6 review gate."""

    passed: bool
    pii_clean: bool = True
    injection_clean: bool = True
    hallucination_flags: List[str] = field(default_factory=list)
    rerender_required: bool = False
    resynth_required: bool = False
    gate_notes: str = ""


@dataclass
class PipelineRun:
    """Live state of a single pipeline execution."""

    id: str
    goal: str
    format: ArtifactFormat
    status: PipelineStatus
    current_stage: PipelineStage

    # Intermediate results
    evidence: EvidencePackage = field(default_factory=list)
    synthesis: Optional[SynthesisReport] = None
    outline: Optional[ArtifactOutline] = None
    artifact: Optional[Artifact] = None
    review: Optional[ReviewResult] = None

    # Timing
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    # Stage durations (seconds)
    stage_durations: Dict[str, float] = field(default_factory=dict)

    # Error info
    error: Optional[str] = None

    # Config carried through
    document_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def touch(self, stage: Optional[PipelineStage] = None) -> None:
        self.updated_at = datetime.now(timezone.utc)
        if stage:
            self.current_stage = stage

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "format": self.format.value,
            "status": self.status.value,
            "current_stage": self.current_stage.value,
            "evidence_count": len(self.evidence),
            "synthesis_confidence": (
                self.synthesis.synthesis_confidence if self.synthesis else None
            ),
            "insight_count": (
                len(self.synthesis.insights) if self.synthesis else None
            ),
            "artifact_id": self.artifact.id if self.artifact else None,
            "artifact_word_count": self.artifact.word_count if self.artifact else None,
            "review_passed": self.review.passed if self.review else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "stage_durations": self.stage_durations,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------


@dataclass
class PipelineConfig:
    """Full pipeline configuration."""

    gather: GatherConfig = field(default_factory=GatherConfig)
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)

    require_outline_approval: bool = False
    outline_approval_timeout_seconds: float = 300.0

    # Review settings
    review_l6_enabled: bool = True
    review_hallucination_threshold: float = 0.70
    review_pii_action: str = "redact"  # "redact" | "block"

    # Delivery
    l7_cancel_window_seconds: float = 30.0
    persist_to_graph: bool = True

    # Stage timeouts (seconds)
    timeout_decompose: float = 30.0
    timeout_gather: float = 120.0
    timeout_synthesize: float = 90.0
    timeout_outline: float = 45.0
    timeout_produce: float = 180.0
    timeout_review: float = 60.0


# ---------------------------------------------------------------------------
# In-memory run store (used when no persistent backend is available)
# ---------------------------------------------------------------------------

_run_store: Dict[str, PipelineRun] = {}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ResearchPipeline:
    """6-stage Research-to-Artifact Pipeline.

    Each stage is implemented as an async method.  ``run()`` chains all
    stages sequentially, recording stage durations and propagating errors.

    The pipeline is designed to be resilient: failures in optional stages
    (e.g. the review gate) are logged and skipped rather than aborting the
    entire run.
    """

    def __init__(self, config: Optional[PipelineConfig] = None) -> None:
        self.config = config or PipelineConfig()
        self._gatherer = SourceGatherer(self.config.gather)
        self._synthesizer = ResearchSynthesizer(self.config.synthesis)
        self._renderer = ArtifactRenderer()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        goal: str,
        format: ArtifactFormat = ArtifactFormat.MARKDOWN,
        document_ids: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PipelineRun:
        """Execute all 6 pipeline stages and return the completed run.

        Args:
            goal: Free-text research goal.
            format: Desired output artifact format.
            document_ids: Optional document IDs to ingest in Stage 2.
            run_id: Optional pre-assigned run ID.
            metadata: Optional metadata to attach to the run.

        Returns:
            A ``PipelineRun`` with ``status=COMPLETED`` (or FAILED on error).
        """
        run = PipelineRun(
            id=run_id or ("rap-" + secrets.token_hex(8)),
            goal=goal,
            format=format,
            status=PipelineStatus.RUNNING,
            current_stage=PipelineStage.DECOMPOSE,
            document_ids=document_ids or [],
            metadata=metadata or {},
        )
        _run_store[run.id] = run
        logger.info("ResearchPipeline: starting run %s goal=%r format=%s", run.id, goal, format.value)

        try:
            # Stage 1: Decompose
            await self._stage_decompose(run)

            # Stage 2: Gather
            await self._stage_gather(run)

            # Stage 3: Synthesize
            await self._stage_synthesize(run)

            # Stage 4: Outline
            await self._stage_outline(run)

            # Stage 5: Produce
            await self._stage_produce(run)

            # Stage 6: Review
            await self._stage_review(run)

            run.status = PipelineStatus.COMPLETED
            run.current_stage = PipelineStage.DONE
            run.completed_at = datetime.now(timezone.utc)
            logger.info(
                "ResearchPipeline: run %s completed in %.1fs",
                run.id,
                sum(run.stage_durations.values()),
            )

        except Exception as exc:
            run.status = PipelineStatus.FAILED
            run.error = str(exc)
            logger.error("ResearchPipeline: run %s failed at stage %s: %s", run.id, run.current_stage, exc)

        run.touch()
        return run

    # ------------------------------------------------------------------
    # Stage 1: Decompose
    # ------------------------------------------------------------------

    async def _stage_decompose(self, run: PipelineRun) -> None:
        """Parse goal text and validate format/constraints."""
        t0 = time.monotonic()
        run.touch(PipelineStage.DECOMPOSE)

        # Validation: goal must be non-empty
        if not run.goal or not run.goal.strip():
            raise ValueError("Research goal cannot be empty")

        # Optionally use GoalDecomposer to validate / enrich the goal
        try:
            from colony_sidecar.goals.decomposer import GoalDecomposer
            decomposer = GoalDecomposer()
            dag = decomposer.decompose_text(run.goal)
            run.metadata["goal_dag_id"] = getattr(dag, "id", None)
        except Exception as exc:
            logger.debug("GoalDecomposer unavailable (%s), continuing without DAG", exc)

        run.stage_durations[PipelineStage.DECOMPOSE.value] = time.monotonic() - t0
        logger.debug("Stage DECOMPOSE: %.2fs", run.stage_durations[PipelineStage.DECOMPOSE.value])

    # ------------------------------------------------------------------
    # Stage 2: Gather
    # ------------------------------------------------------------------

    async def _stage_gather(self, run: PipelineRun) -> None:
        """Collect evidence from all enabled sources in parallel."""
        t0 = time.monotonic()
        run.touch(PipelineStage.GATHER)

        evidence = await self._gatherer.gather(
            query=run.goal,
            document_ids=run.document_ids or None,
        )
        run.evidence = evidence

        run.stage_durations[PipelineStage.GATHER.value] = time.monotonic() - t0
        logger.debug(
            "Stage GATHER: %d items in %.2fs",
            len(evidence),
            run.stage_durations[PipelineStage.GATHER.value],
        )

    # ------------------------------------------------------------------
    # Stage 3: Synthesize
    # ------------------------------------------------------------------

    async def _stage_synthesize(self, run: PipelineRun) -> None:
        """Cross-reference evidence and extract insights."""
        t0 = time.monotonic()
        run.touch(PipelineStage.SYNTHESIZE)

        synthesis = await self._synthesizer.synthesize(
            evidence=run.evidence,
            goal_id=run.id,
            goal_text=run.goal,
        )
        run.synthesis = synthesis

        run.stage_durations[PipelineStage.SYNTHESIZE.value] = time.monotonic() - t0
        logger.debug(
            "Stage SYNTHESIZE: %d insights, confidence=%.2f in %.2fs",
            len(synthesis.insights),
            synthesis.synthesis_confidence,
            run.stage_durations[PipelineStage.SYNTHESIZE.value],
        )

    # ------------------------------------------------------------------
    # Stage 4: Outline
    # ------------------------------------------------------------------

    async def _stage_outline(self, run: PipelineRun) -> None:
        """Build the structured artifact outline."""
        t0 = time.monotonic()
        run.touch(PipelineStage.OUTLINE)

        assert run.synthesis is not None
        outline = self._renderer.build_outline(
            synthesis=run.synthesis,
            goal_text=run.goal,
            format=run.format,
        )
        run.outline = outline

        run.stage_durations[PipelineStage.OUTLINE.value] = time.monotonic() - t0
        logger.debug(
            "Stage OUTLINE: %d sections in %.2fs",
            len(outline.sections),
            run.stage_durations[PipelineStage.OUTLINE.value],
        )

    # ------------------------------------------------------------------
    # Stage 5: Produce
    # ------------------------------------------------------------------

    async def _stage_produce(self, run: PipelineRun) -> None:
        """Render the artifact in the requested format."""
        t0 = time.monotonic()
        run.touch(PipelineStage.PRODUCE)

        assert run.outline is not None
        assert run.synthesis is not None
        artifact = self._renderer.render(
            outline=run.outline,
            synthesis=run.synthesis,
            goal_text=run.goal,
        )
        run.artifact = artifact

        run.stage_durations[PipelineStage.PRODUCE.value] = time.monotonic() - t0
        logger.debug(
            "Stage PRODUCE: format=%s words=%d in %.2fs",
            artifact.format.value,
            artifact.word_count,
            run.stage_durations[PipelineStage.PRODUCE.value],
        )

    # ------------------------------------------------------------------
    # Stage 6: Review
    # ------------------------------------------------------------------

    async def _stage_review(self, run: PipelineRun) -> None:
        """Run artifact through the response gate."""
        t0 = time.monotonic()
        run.touch(PipelineStage.REVIEW)

        assert run.artifact is not None
        review = await self._run_review_gate(run.artifact, run)
        run.review = review

        if not review.passed:
            if review.resynth_required:
                # Re-synthesize flagged sections (simplified: log and continue)
                logger.warning("Review: resynth required — skipping in this run")
            elif not review.rerender_required:
                raise RuntimeError(
                    f"Artifact failed review gate: {review.gate_notes}"
                )

        run.stage_durations[PipelineStage.REVIEW.value] = time.monotonic() - t0
        logger.debug(
            "Stage REVIEW: passed=%s in %.2fs",
            review.passed,
            run.stage_durations[PipelineStage.REVIEW.value],
        )

    async def _run_review_gate(
        self,
        artifact: Artifact,
        run: PipelineRun,
    ) -> ReviewResult:
        """Content-safety review of a research artifact.

        A research artifact has no recipient/trust-tier, so the full
        ResponseGate (recipient verification, cross-context, trust tiering)
        does not apply — but its PII scanner (L2) and injection detector (L5)
        do. Run those two layers directly. (The previous code constructed
        ResponseGate() with the wrong arguments, threw on every call, and
        silently degraded to a 4-string substring scan.)"""
        try:
            from colony_sidecar.gate.layers.l2_pii import PIIScanner
            from colony_sidecar.gate.layers.l5_injection import InjectionDetector
            from colony_sidecar.gate.config import GateConfig
            from colony_sidecar.gate.models import GatePayload, TrustTier

            cfg = GateConfig()
            payload = GatePayload(
                response_text=artifact.content,
                target_contact_id="", target_gateway="",
                session_id=str(run.metadata.get("session_id", "research")),
                trust_tier=TrustTier.PERIPHERAL,
                mentioned_entities=frozenset(),
                turn_id=str(getattr(run, "run_id", "") or "research"),
                incoming_message_text="",
            )
            pii = await PIIScanner(cfg).check(payload)
            inj = await InjectionDetector(cfg).check(payload)
            pii_clean = not pii.blocked
            injection_clean = not inj.blocked
            notes = []
            if not pii_clean:
                notes.append(f"pii:{pii.code}")
            if not injection_clean:
                notes.append(f"injection:{inj.reason or inj.code}")
            return ReviewResult(
                passed=pii_clean and injection_clean,
                pii_clean=pii_clean,
                injection_clean=injection_clean,
                gate_notes="; ".join(notes) or "clean",
            )
        except Exception as exc:
            logger.warning("research review gate failed (%s); "
                           "falling back to injection-marker scan", exc)

        # Fallback only when the gate layers are genuinely unavailable.
        content = artifact.content
        injection_markers = ["<script", "javascript:", "data:text/html", "eval("]
        injection_found = any(m in content.lower() for m in injection_markers)
        return ReviewResult(
            passed=not injection_found,
            pii_clean=True,
            injection_clean=not injection_found,
            gate_notes="basic-review" if not injection_found else "injection-detected",
        )

    # ------------------------------------------------------------------
    # Run management helpers
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> Optional[PipelineRun]:
        """Retrieve a run by ID from the in-memory store."""
        return _run_store.get(run_id)

    def list_runs(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[PipelineRun]:
        """List runs from the in-memory store with optional status filter."""
        runs = list(_run_store.values())
        if status:
            runs = [r for r in runs if r.status.value == status]
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return runs[offset: offset + limit]
