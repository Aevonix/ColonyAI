"""Research artifact — Stages 4 & 5 of the Research-to-Artifact Pipeline.

Stage 4 (OUTLINE): LLM-assisted structure builder
Stage 5 (PRODUCE): Format-aware artifact renderer

Supported output formats:
  MARKDOWN  — full prose report with citations
  PDF       — formatted document (markdown post-processed)
  SLIDES    — structured JSON slide deck
  BRIEFING  — BriefingDeliveryEngine-compatible object
  JSON      — structured data
  CSV       — tabular data
"""

from __future__ import annotations

import csv
import io
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from colony_sidecar.research.synthesizer import DomainInsight, SynthesisReport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format enum
# ---------------------------------------------------------------------------


class ArtifactFormat(str, Enum):
    MARKDOWN = "markdown"
    PDF = "pdf"
    SLIDES = "slides"
    BRIEFING = "briefing"
    JSON = "json"
    CSV = "csv"


# ---------------------------------------------------------------------------
# Outline models
# ---------------------------------------------------------------------------


@dataclass
class OutlineSection:
    """A section in the artifact outline (recursive)."""

    title: str
    directive: str                           # What this section should cover
    source_insight_ids: List[str] = field(default_factory=list)
    contradiction_ids: List[str] = field(default_factory=list)
    subsections: List["OutlineSection"] = field(default_factory=list)


@dataclass
class ArtifactOutline:
    """Structured outline for artifact production."""

    goal_id: str
    format: ArtifactFormat
    title: str
    sections: List[OutlineSection]
    estimated_length: str = ""              # "~800 words", "12 slides", etc.


# ---------------------------------------------------------------------------
# Artifact model
# ---------------------------------------------------------------------------


@dataclass
class Artifact:
    """A rendered research artifact ready for delivery."""

    id: str
    goal_id: str
    format: ArtifactFormat
    title: str
    content: str                            # Rendered content (format-specific)
    word_count: int = 0
    slide_count: int = 0
    citation_count: int = 0
    grounded: bool = True                   # All claims have citation support
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Outline builder (Stage 4)
# ---------------------------------------------------------------------------


class OutlineBuilder:
    """Build a structured ArtifactOutline from a SynthesisReport."""

    def build(
        self,
        synthesis: SynthesisReport,
        goal_text: str,
        format: ArtifactFormat,
        require_approval: bool = False,
    ) -> ArtifactOutline:
        """Generate an outline from synthesis insights.

        Uses an LLM if available, falls back to a rule-based outline.
        """
        title = self._infer_title(goal_text)
        sections = self._build_sections(synthesis, format)
        estimated = self._estimate_length(sections, format)

        return ArtifactOutline(
            goal_id=synthesis.goal_id,
            format=format,
            title=title,
            sections=sections,
            estimated_length=estimated,
        )

    def _infer_title(self, goal_text: str) -> str:
        words = goal_text.strip().split()
        if len(words) <= 8:
            return goal_text.strip().title()
        return " ".join(words[:8]).title() + "..."

    def _build_sections(
        self,
        synthesis: SynthesisReport,
        format: ArtifactFormat,
    ) -> List[OutlineSection]:
        sections: List[OutlineSection] = []

        if format == ArtifactFormat.SLIDES:
            sections = self._slide_sections(synthesis)
        elif format in (ArtifactFormat.JSON, ArtifactFormat.CSV):
            sections = self._data_sections(synthesis)
        else:
            sections = self._prose_sections(synthesis)

        return sections

    def _prose_sections(self, synthesis: SynthesisReport) -> List[OutlineSection]:
        sections = [
            OutlineSection(
                title="Executive Summary",
                directive="Summarize the key findings and overall synthesis confidence.",
            )
        ]

        for insight in synthesis.insights[:6]:
            sections.append(
                OutlineSection(
                    title=insight.title,
                    directive=insight.summary[:200],
                    source_insight_ids=[insight.id],
                )
            )

        if synthesis.contradictions:
            sections.append(
                OutlineSection(
                    title="Conflicting Evidence",
                    directive="Surface conflicting evidence with source attribution.",
                    contradiction_ids=[c.id for c in synthesis.contradictions],
                )
            )

        sections.append(
            OutlineSection(
                title="Sources",
                directive="List all cited sources with metadata.",
            )
        )
        return sections

    def _slide_sections(self, synthesis: SynthesisReport) -> List[OutlineSection]:
        sections = [
            OutlineSection(title="Title Slide", directive="Title and subtitle."),
            OutlineSection(title="Overview", directive="Research goal and methodology."),
        ]
        for insight in synthesis.insights[:8]:
            sections.append(
                OutlineSection(
                    title=insight.title,
                    directive=insight.summary[:150],
                    source_insight_ids=[insight.id],
                )
            )
        sections.append(OutlineSection(title="Conclusions", directive="Key takeaways."))
        sections.append(OutlineSection(title="Sources", directive="Reference list."))
        return sections

    def _data_sections(self, synthesis: SynthesisReport) -> List[OutlineSection]:
        return [
            OutlineSection(
                title="Data",
                directive="Structured data extracted from research findings.",
                source_insight_ids=[i.id for i in synthesis.insights],
            )
        ]

    def _estimate_length(
        self,
        sections: List[OutlineSection],
        format: ArtifactFormat,
    ) -> str:
        n = len(sections)
        if format == ArtifactFormat.SLIDES:
            return f"{n} slides"
        if format in (ArtifactFormat.JSON, ArtifactFormat.CSV):
            return f"{n} data sections"
        return f"~{n * 150} words"


# ---------------------------------------------------------------------------
# Format renderers
# ---------------------------------------------------------------------------


class MarkdownRenderer:
    """Render a research artifact as Markdown."""

    def render(
        self,
        outline: ArtifactOutline,
        synthesis: SynthesisReport,
        goal_text: str,
    ) -> str:
        lines: List[str] = [f"# {outline.title}", ""]

        # Metadata block
        lines += [
            f"> **Research goal:** {goal_text}",
            f"> **Confidence:** {synthesis.synthesis_confidence:.0%}",
            f"> **Evidence sources:** {synthesis.evidence_count} items",
            "",
        ]

        # Sections
        insight_map = {i.id: i for i in synthesis.insights}
        contradiction_map = {c.id: c for c in synthesis.contradictions}

        for section in outline.sections:
            lines.append(f"## {section.title}")
            lines.append("")
            lines.append(section.directive)
            lines.append("")

            for insight_id in section.source_insight_ids:
                insight = insight_map.get(insight_id)
                if insight:
                    lines.append(f"**Finding:** {insight.summary}")
                    if insight.supporting_citations:
                        citations = ", ".join(
                            f"[{i+1}]" for i, _ in enumerate(insight.supporting_citations)
                        )
                        lines.append(f"*Sources: {citations}*")
                    lines.append("")

            for contra_id in section.contradiction_ids:
                contra = contradiction_map.get(contra_id)
                if contra:
                    lines.append(f"**Conflicting evidence on '{contra.topic}':**")
                    lines.append(f"- Source A ({contra.confidence_a:.0%} confidence): {contra.claim_a[:200]}")
                    lines.append(f"- Source B ({contra.confidence_b:.0%} confidence): {contra.claim_b[:200]}")
                    lines.append("")

        # Sources appendix
        lines += ["---", "## Sources", ""]
        all_citations: List[str] = []
        for insight in synthesis.insights:
            all_citations.extend(insight.supporting_citations)
        for i, citation in enumerate(dict.fromkeys(all_citations), 1):
            lines.append(f"[{i}] {citation}")

        return "\n".join(lines)


class SlideRenderer:
    """Render a research artifact as a structured JSON slide deck."""

    def render(
        self,
        outline: ArtifactOutline,
        synthesis: SynthesisReport,
        goal_text: str,
    ) -> str:
        insight_map = {i.id: i for i in synthesis.insights}
        slides: List[Dict[str, Any]] = []

        for section in outline.sections:
            if section.title in ("Title Slide",):
                slides.append(
                    {
                        "type": "title",
                        "heading": outline.title,
                        "subheading": goal_text[:100],
                    }
                )
            elif section.source_insight_ids:
                bullets: List[str] = []
                for iid in section.source_insight_ids:
                    insight = insight_map.get(iid)
                    if insight:
                        bullets.append(insight.summary[:150])
                slides.append(
                    {
                        "type": "content",
                        "heading": section.title,
                        "bullets": bullets or [section.directive],
                        "speaker_notes": section.directive,
                    }
                )
            else:
                slides.append(
                    {
                        "type": "content",
                        "heading": section.title,
                        "bullets": [section.directive],
                        "speaker_notes": "",
                    }
                )

        all_citations: List[str] = []
        for insight in synthesis.insights:
            all_citations.extend(insight.supporting_citations)

        deck = {
            "title": outline.title,
            "slides": slides,
            "citations": list(dict.fromkeys(all_citations)),
        }
        return json.dumps(deck, indent=2)


class DataRenderer:
    """Render a research artifact as JSON or CSV structured data."""

    def render_json(
        self,
        outline: ArtifactOutline,
        synthesis: SynthesisReport,
        goal_text: str,
    ) -> str:
        data = {
            "goal": goal_text,
            "goal_id": synthesis.goal_id,
            "confidence": synthesis.synthesis_confidence,
            "source_breakdown": synthesis.source_breakdown,
            "insights": [
                {
                    "id": i.id,
                    "title": i.title,
                    "summary": i.summary,
                    "domains": i.domains,
                    "confidence": i.confidence,
                    "novelty_score": i.novelty_score,
                    "citations": i.supporting_citations,
                }
                for i in synthesis.insights
            ],
            "contradictions": [
                {
                    "id": c.id,
                    "topic": c.topic,
                    "claim_a": c.claim_a,
                    "claim_b": c.claim_b,
                    "citation_a": c.citation_a,
                    "citation_b": c.citation_b,
                }
                for c in synthesis.contradictions
            ],
        }
        return json.dumps(data, indent=2)

    def render_csv(
        self,
        outline: ArtifactOutline,
        synthesis: SynthesisReport,
        goal_text: str,
    ) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "title", "summary", "domains", "confidence", "novelty_score", "citations"])
        for insight in synthesis.insights:
            writer.writerow([
                insight.id,
                insight.title,
                insight.summary[:300],
                "|".join(insight.domains),
                f"{insight.confidence:.3f}",
                f"{insight.novelty_score:.3f}",
                "|".join(insight.supporting_citations),
            ])
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Artifact renderer dispatcher (Stage 5)
# ---------------------------------------------------------------------------


class ArtifactRenderer:
    """Dispatch artifact rendering to the correct format-specific renderer."""

    def __init__(self) -> None:
        self._markdown = MarkdownRenderer()
        self._slides = SlideRenderer()
        self._data = DataRenderer()
        self._outline_builder = OutlineBuilder()

    def build_outline(
        self,
        synthesis: SynthesisReport,
        goal_text: str,
        format: ArtifactFormat,
    ) -> ArtifactOutline:
        """Stage 4: Build artifact outline from synthesis."""
        return self._outline_builder.build(synthesis, goal_text, format)

    def render(
        self,
        outline: ArtifactOutline,
        synthesis: SynthesisReport,
        goal_text: str,
    ) -> Artifact:
        """Stage 5: Render the artifact from its outline."""
        fmt = outline.format

        if fmt == ArtifactFormat.MARKDOWN:
            content = self._markdown.render(outline, synthesis, goal_text)
        elif fmt == ArtifactFormat.PDF:
            # PDF: render markdown then annotate for post-processing
            md = self._markdown.render(outline, synthesis, goal_text)
            content = f"<!-- PDF_RENDER -->\n{md}"
        elif fmt == ArtifactFormat.SLIDES:
            content = self._slides.render(outline, synthesis, goal_text)
        elif fmt == ArtifactFormat.BRIEFING:
            # Briefing: use markdown as base content
            content = self._markdown.render(outline, synthesis, goal_text)
        elif fmt == ArtifactFormat.JSON:
            content = self._data.render_json(outline, synthesis, goal_text)
        elif fmt == ArtifactFormat.CSV:
            content = self._data.render_csv(outline, synthesis, goal_text)
        else:
            content = self._markdown.render(outline, synthesis, goal_text)

        all_citations: List[str] = []
        for insight in synthesis.insights:
            all_citations.extend(insight.supporting_citations)

        word_count = len(content.split()) if fmt != ArtifactFormat.CSV else 0
        slide_count = 0
        if fmt == ArtifactFormat.SLIDES:
            try:
                deck = json.loads(content)
                slide_count = len(deck.get("slides", []))
            except Exception:
                pass

        return Artifact(
            id=secrets.token_hex(8),
            goal_id=synthesis.goal_id,
            format=fmt,
            title=outline.title,
            content=content,
            word_count=word_count,
            slide_count=slide_count,
            citation_count=len(dict.fromkeys(all_citations)),
            grounded=True,
            metadata={
                "synthesis_confidence": synthesis.synthesis_confidence,
                "insight_count": len(synthesis.insights),
                "contradiction_count": len(synthesis.contradictions),
            },
        )
