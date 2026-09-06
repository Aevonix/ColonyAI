"""Render the self-model into a compact prompt brief.

Historical outcome labels describe execution records. They do not establish
output quality, intrinsic ability, or competence after changing models.
"""

from __future__ import annotations

from typing import Any, Dict, List


def self_brief(domains: List[Dict[str, Any]], load: Dict[str, int]) -> str:
    """Compact historical runtime facts for prompt injection. Empty when nothing
    is evidenced yet."""
    records = []
    for d in domains or []:
        n = int(d.get("n") or 0)
        name = d.get("domain", "?")
        if d.get("evidence_available") is False:
            records.append(f"{name}: historical evidence incomplete")
            continue
        if n:
            records.append(f"{name}: {int(d.get('success') or 0)} labeled success, "
                           f"{int(d.get('failure') or 0)} failure, "
                           f"{int(d.get('timeout') or 0)} timeout")

    lines: List[str] = []
    if records:
        lines.append("These historical labels may include legacy or unverified records. "
                     "They do not verify output quality or establish the current model's ability. "
                     "A timeout alone does not establish output quality or its cause.")
        lines.append("Recorded runtime outcomes: " + "; ".join(sorted(records)) + ".")
    total = int((load or {}).get("total") or 0)
    if total or lines:
        lines.append(
            f"Current load: {total} in flight "
            f"({(load or {}).get('active_initiatives', 0)} initiatives, "
            f"{(load or {}).get('active_projects', 0)} projects, "
            f"{(load or {}).get('queued_jobs', 0)} queued jobs).")
    return "\n".join(lines)
