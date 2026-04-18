"""Colony Skills — pattern extraction from task solutions."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ExtractedPattern:
    """The generalized form of a task solution, ready for packaging."""
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    step_sequence: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    env_vars: Set[str] = field(default_factory=set)
    file_paths: List[str] = field(default_factory=list)
    network_domains: List[str] = field(default_factory=list)
    source_code: str = ""
    docstring: str = ""
    tags: List[str] = field(default_factory=list)


class PatternExtractor:
    """Extracts a generalizable pattern from a TaskSolution.

    Three-pass process:
    1. Structural analysis: parse execution trace for tool calls and steps.
    2. Input/output typing: infer parameter types from observed values.
    3. Code synthesis: generate a Python stub function.
    """

    def extract(self, solution: "TaskSolution") -> ExtractedPattern:  # noqa: F821
        """Extract a reusable pattern from a task solution."""
        steps = self._extract_steps(solution.trace)
        deps = self._extract_dependencies(solution.trace)
        env_vars = self._extract_env_vars(solution.trace)
        domains = self._extract_network_domains(solution.trace)
        input_schema = self._infer_input_schema(solution.inputs)
        output_schema = self._infer_output_schema(solution.output)
        source = self._synthesize_source(
            solution.task_description, steps, input_schema, output_schema
        )
        tags = self._extract_tags(solution.task_description)

        return ExtractedPattern(
            input_schema=input_schema,
            output_schema=output_schema,
            step_sequence=steps,
            dependencies=sorted(deps),
            env_vars=env_vars,
            network_domains=domains,
            source_code=source,
            docstring=self._generate_docstring(solution.task_description, steps),
            tags=tags,
        )

    def _extract_steps(self, trace: List[Dict[str, Any]]) -> List[str]:
        return [
            f"{entry.get('tool', 'unknown')}({entry.get('summary', '')})"
            for entry in (trace or [])
            if entry.get("type") == "tool_call"
        ]

    def _extract_dependencies(self, trace: List[Dict[str, Any]]) -> Set[str]:
        deps: Set[str] = set()
        for entry in (trace or []):
            if entry.get("type") == "import":
                pkg = entry.get("package", "").split(".")[0]
                if pkg:
                    deps.add(pkg)
        return deps

    def _extract_env_vars(self, trace: List[Dict[str, Any]]) -> Set[str]:
        return {
            entry["var_name"]
            for entry in (trace or [])
            if entry.get("type") == "env_access" and "var_name" in entry
        }

    def _extract_network_domains(self, trace: List[Dict[str, Any]]) -> List[str]:
        seen: List[str] = []
        for entry in (trace or []):
            if entry.get("type") == "network_request":
                domain = entry.get("domain", "")
                if domain and domain not in seen:
                    seen.append(domain)
        return seen

    def _infer_input_schema(self, inputs: Dict[str, Any]) -> Dict[str, Any]:
        props = {}
        required = []
        for key, value in (inputs or {}).items():
            props[key] = {"type": self._python_type_to_json(type(value))}
            required.append(key)
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }

    def _infer_output_schema(self, output: Any) -> Dict[str, Any]:
        return {"type": self._python_type_to_json(type(output))}

    @staticmethod
    def _python_type_to_json(t: type) -> str:
        mapping = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
            type(None): "null",
        }
        return mapping.get(t, "string")

    def _synthesize_source(
        self,
        description: str,
        steps: List[str],
        input_schema: Dict[str, Any],
        output_schema: Dict[str, Any],
    ) -> str:
        params = ", ".join(
            f"{k}: {v.get('type', 'Any')}"
            for k, v in input_schema.get("properties", {}).items()
        )
        # Join with 16-space indent (matches the template's 16-space indent before dedent strips 12)
        step_comments = "\n                ".join(f"# Step: {s}" for s in steps[:10])
        if not step_comments:
            step_comments = "# No steps recorded"
        return textwrap.dedent(f"""\
            from __future__ import annotations
            from typing import Any

            async def run({params}) -> Any:
                \"\"\"Auto-generated skill.

                Original task: {description}
                \"\"\"
                {step_comments}
                raise NotImplementedError(
                    "Skill body requires human review before activation."
                )
        """)

    def _generate_docstring(self, description: str, steps: List[str]) -> str:
        step_list = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(steps[:8]))
        return f"Solves: {description}\n\nCapture steps:\n{step_list}"

    def _extract_tags(self, description: str) -> List[str]:
        stop = {"a", "the", "and", "or", "to", "in", "of", "for", "with", "that"}
        words = description.lower().split()
        return sorted({w.strip(".,") for w in words if w not in stop and len(w) > 3})[:8]
