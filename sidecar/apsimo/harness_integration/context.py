"""Write a short, model-independent Apsimo reference into a workspace."""
from pathlib import Path

APSIMO_CONTEXT_TEMPLATE = """# Apsimo integration

Apsimo is the cognition and continuity layer, formerly ColonyAI. Your agent's
private identity is separate from the platform name. Old-name searches refer to
the same platform and history.

Use `apsimo_health` to check connectivity and `apsimo_get_context` for relevant,
authenticated context. Check `apsimo_check_commitments` before promising work.
Recall useful evidence with `apsimo_lookup_facts`; cite the returned source and
ask about contradictions. Save durable facts with `apsimo_remember_fact` only
when their source and usefulness are clear. Corrections and forgetting govern
source-backed recall across sessions; do not create diagnostic junk memories.

Use `apsimo doctor` and `apsimo mcp detect` for installation checks. Hermes uses
its native Apsimo plugin and memory provider. Coding harnesses may use the MCP
server. Both connect to the same selected instance over its authenticated API;
sharing an instance does not grant every caller access to every private source.

APSIMO_* configuration names are preferred; existing COLONY_* names remain
accepted. Use the selected instance's actual configuration and credential
reference. Never guess a key or print credentials into diagnostic transcripts.
"""

# Public compatibility aliases point to the same template and writer.
COLONY_CONTEXT_TEMPLATE = APSIMO_CONTEXT_TEMPLATE


def write_apsimo_context(workspace_dir: Path) -> bool:
    if not workspace_dir.exists():
        return False
    try:
        (workspace_dir / "APSIMO.md").write_text(APSIMO_CONTEXT_TEMPLATE)
        return True
    except OSError:
        return False


write_colony_context = write_apsimo_context
