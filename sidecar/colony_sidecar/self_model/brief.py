"""Render the self-model into a compact prompt brief.

The brief states only what the evidence supports: reliable domains (enough
samples, high success), unreliable domains, timeout-prone domains, and the
current load. Domains without enough samples are omitted -- no flattery, no
invented weaknesses.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Evidence thresholds. A claim of reliability needs more samples than a
# warning (asymmetric: it is safer to under-claim competence).
_RELIABLE_MIN_N = 5
_RELIABLE_RATE = 0.8
_WEAK_MIN_N = 3
_WEAK_RATE = 0.4
_TIMEOUT_MIN_N = 3
_TIMEOUT_RATE = 0.3


def self_brief(domains: List[Dict[str, Any]], load: Dict[str, int]) -> str:
    """Compact self-assessment text for prompt injection. Empty when nothing
    is evidenced yet."""
    reliable, weak, slow = [], [], []
    for d in domains or []:
        n = int(d.get("n") or 0)
        rate = d.get("success_rate")
        trate = d.get("timeout_rate")
        name = d.get("domain", "?")
        if n >= _TIMEOUT_MIN_N and trate is not None and trate >= _TIMEOUT_RATE:
            slow.append(f"{name} ({int(round(trate * 100))}% timeouts, n={n})")
        if rate is None:
            continue
        if n >= _RELIABLE_MIN_N and rate >= _RELIABLE_RATE:
            reliable.append(f"{name} (p={rate:.2f}, n={n})")
        elif n >= _WEAK_MIN_N and rate <= _WEAK_RATE:
            weak.append(f"{name} (p={rate:.2f}, n={n})")

    lines: List[str] = []
    if reliable:
        lines.append("You reliably complete: " + "; ".join(sorted(reliable)) + ".")
    if weak:
        lines.append("You often fail at: " + "; ".join(sorted(weak))
                     + ". Prefer smaller scopes or escalate to the owner.")
    if slow:
        lines.append("Timeout-prone: " + "; ".join(sorted(slow))
                     + ". Budget extra time or defer under load.")
    total = int((load or {}).get("total") or 0)
    if total or lines:
        lines.append(
            f"Current load: {total} in flight "
            f"({(load or {}).get('active_initiatives', 0)} initiatives, "
            f"{(load or {}).get('active_projects', 0)} projects, "
            f"{(load or {}).get('queued_jobs', 0)} queued jobs).")
    return "\n".join(lines)
