"""PacoMind Research-to-Artifact Pipeline (RAP).

A unified, autonomy-capable pipeline that takes a research goal and produces
a polished, gated deliverable with minimal human intervention.

Six pipeline stages:
  1. DECOMPOSE  — break goal into sub-tasks
  2. GATHER     — collect evidence from web, graph, documents, email
  3. SYNTHESIZE — cross-reference and extract insights
  4. OUTLINE    — build structured artifact outline
  5. PRODUCE    — render artifact in requested format
  6. REVIEW     — quality and safety gate

Usage::

    from pacomind.research import ResearchPipeline, ArtifactFormat

    pipeline = ResearchPipeline()
    result = await pipeline.run(
        goal="Research competitor pricing and produce a report",
        format=ArtifactFormat.MARKDOWN,
    )
"""

from pacomind.research.artifact import ArtifactFormat, Artifact
from pacomind.research.gatherer import EvidenceItem, EvidencePackage, SourceType
from pacomind.research.synthesizer import SynthesisReport
from pacomind.research.pipeline import ResearchPipeline, PipelineRun, PipelineStage, PipelineStatus

__all__ = [
    "ResearchPipeline",
    "PipelineRun",
    "PipelineStage",
    "PipelineStatus",
    "ArtifactFormat",
    "Artifact",
    "EvidenceItem",
    "EvidencePackage",
    "SourceType",
    "SynthesisReport",
]
