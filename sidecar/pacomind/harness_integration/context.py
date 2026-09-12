"""Write a short, model-independent PacoMind reference into a workspace."""
from pathlib import Path

PACOMIND_CONTEXT_TEMPLATE = """# PacoMind integration

PacoMind provides persistent memory, shared task state and autonomy. PACO means
Persistent Autonomous Cognitive Orchestration. Your agent's private identity is
separate from the platform name.

Use `pacomind_health` to check connectivity and `pacomind_get_context` for relevant,
authenticated context. Check `pacomind_check_commitments` before promising work.
Recall useful evidence with `pacomind_lookup_facts`; cite the returned source and
ask about contradictions. Save durable facts with `pacomind_remember_fact` only
when their source and usefulness are clear. Corrections and forgetting govern
source-backed recall across sessions; do not create diagnostic junk memories.

Use `pacomind doctor` and `pacomind mcp detect` for installation checks. Hermes uses
its native PacoMind plugin and memory provider. Coding harnesses may use the MCP
server. Both connect to the same selected instance over its authenticated API;
sharing an instance does not grant every caller access to every private source.

Use the selected instance's PACOMIND_* configuration and credential
reference. Never guess a key or print credentials into diagnostic transcripts.
"""



def write_pacomind_context(workspace_dir: Path) -> bool:
    if not workspace_dir.exists():
        return False
    try:
        (workspace_dir / "PACOMIND.md").write_text(PACOMIND_CONTEXT_TEMPLATE)
        return True
    except OSError:
        return False
