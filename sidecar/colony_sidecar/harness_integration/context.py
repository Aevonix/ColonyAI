"""Write COLONY.md context file to harness workspaces."""

from __future__ import annotations

from pathlib import Path


COLONY_CONTEXT_TEMPLATE = """# Colony Integration

Colony is a cognitive substrate providing shared memory across your agents and coding tools.

## Quick Reference

### MCP Tools (for coding harnesses)

| Tool | Purpose |
|------|---------|
| `colony_health` | Check sidecar status |
| `colony_lookup_facts` | Search stored facts |
| `colony_remember_fact` | Store a new fact |
| `colony_get_context` | Get assembled context for a contact |
| `colony_check_commitments` | List active commitments |
| `colony_create_commitment` | Create a new commitment |
| `colony_fulfill_commitment` | Mark commitment fulfilled |
| `colony_cancel_commitment` | Cancel a commitment |
| `colony_search_world` | Search world model entities |
| `colony_get_patterns` | Get learned patterns |
| `colony_record_surprise` | Record a surprise event |
| `colony_forget_fact` | Remove a fact |

### API Endpoints (for plugins)

Base URL: `http://127.0.0.1:7777/v1/host/`

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/mind/facts` | GET, POST | List/store facts |
| `/commitments` | GET, POST | List/create commitments |
| `/context/assemble` | POST | Get context scoped to the authenticated viewer and contact |
| `/capabilities` | GET | List all capabilities |

Authentication: `Authorization: Bearer {api_key}`

### Context Flow

```
┌─────────────┐                    ┌─────────────┐
│  OpenClaw   │                    │    Crush    │
│  (Plugin)   │                    │    (MCP)    │
└──────┬──────┘                    └──────┬──────┘
       │                                  │
       ▼                                  ▼
┌─────────────────────────────────────────────────┐
│              Colony Sidecar (:7777)             │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐           │
│  │  Facts  │ │Commit-  │ │  World  │  ...      │
│  │         │ │ments    │ │  Model  │           │
│  └─────────┘ └─────────┘ └─────────┘           │
└─────────────────────────────────────────────────┘

Harnesses share canonical sources. Retrieval is scoped to the authenticated
viewer and conversation; private facts are not automatically shared with
every harness. Corrections and forgetting govern derived recall.
```

## Configuration

Your Colony configuration is in `~/.colony/.env`:

```bash
COLONY_API_KEY=your-key
COLONY_SIDECAR_HOST=127.0.0.1
COLONY_SIDECAR_PORT=7777
```

## Troubleshooting

Run `colony doctor` to diagnose issues.

See the `colony-diagnose` skill for detailed troubleshooting steps.
"""


def write_colony_context(workspace_dir: Path) -> bool:
    """Write COLONY.md to the harness workspace.
    
    Args:
        workspace_dir: Path to the harness workspace (e.g., ~/.openclaw/workspace)
    
    Returns:
        True if written successfully, False otherwise
    """
    if not workspace_dir.exists():
        return False
    
    colony_md = workspace_dir / "COLONY.md"
    
    try:
        colony_md.write_text(COLONY_CONTEXT_TEMPLATE)
        return True
    except Exception:
        return False
