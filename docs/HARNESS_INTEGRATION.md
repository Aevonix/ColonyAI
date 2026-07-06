# Colony Harness Integration Guide

Colony integrates with multiple agent harnesses to provide shared cognitive context across your tools.

## Architecture Overview

Colony supports two integration paths:

```
┌─────────────────────────────────────────────────────────────────┐
│                      Colony Sidecar (:7777)                      │
│                                                                  │
│  ┌──────────────────┐              ┌──────────────────┐         │
│  │   Plugin API     │              │    MCP Server    │         │
│  │  (HTTP REST)     │              │    (stdio/HTTP)  │         │
│  └────────┬─────────┘              └────────┬─────────┘         │
│           │                                  │                   │
└───────────┼──────────────────────────────────┼───────────────────┘
            │                                  │
            ▼                                  ▼
    ┌───────────────┐                  ┌───────────────┐
    │  Agent Hosts  │                  │  Coding Tools │
    │   (Hermes)    │                  │  (Claude Code,│
    │               │                  │   Codex, ...) │
    └───────────────┘                  └───────────────┘
```

**Plugin path** — for orchestrator/chat agents (Hermes):
- Direct HTTP API access via host plugins
- Always-on context integration: context injected before each turn, the turn synced back afterward

**MCP path** — for coding agents (Claude Code, Codex, Crush, OpenCode; Hermes can use both):
- Model Context Protocol via stdio (HTTP transport also available: `colony mcp run --transport http`)
- Configured via harness config files
- On-demand tool access

Both paths read/write the same cognitive stores — facts stored by one harness are immediately visible to the others.

Anything that is neither a Hermes host nor an MCP client can talk to the REST API directly (see [API Endpoints Reference](#api-endpoints-reference)).

---

## Agent Harnesses (Plugin)

### Hermes

[Hermes](https://github.com/NousResearch/hermes-agent) is a memory-augmented agent framework that supports **both plugin and MCP integration**.

Colony ships three Hermes plugins, all in this repo:

| Repo path | Installs to | Role |
|-----------|-------------|------|
| `plugins/hermes-plugin/` | `~/.hermes/plugins/colony/` | General adapter: native Colony tools, slash commands (`/colony status`, ...), lifecycle hooks (contact resolution, time injection, turn journaling), WebSocket event subscriber, autonomy bridge |
| `plugins/colony-memory/` | `~/.hermes/plugins/colony-memory/` | Memory provider: injects assembled context before each turn and syncs the turn back for extraction. The single canonical copy of the provider |
| `plugins/hermes-context/` | `~/.hermes/plugins/context_engine/colony/` | Context engine: replaces the built-in compressor with Colony's cognitive summarization |

**Setup (plugin path):**
```bash
colony init --agent-harness hermes      # wizard installs and configures the plugins
# or, from a repo checkout:
plugins/hermes-plugin/install.sh --memory
```

**Setup (MCP path):**
```bash
colony mcp setup --harness hermes
```

The MCP path writes an entry to `~/.hermes/config.yaml`:
```yaml
mcp_servers:
  colony:
    command: colony
    args: ["mcp"]
    env:
      COLONY_API_KEY: "${COLONY_API_KEY}"
      COLONY_URL: "http://127.0.0.1:7777"
      COLONY_MCP_CONTACT_ID: "user"
      COLONY_MCP_SOURCE: "hermes"
```

Host-side operational tooling (a self-validating doctor, a resilient gateway-restart runner, an activity monitor) lives under `plugins/hermes-plugin/ops/` — see its [README](../plugins/hermes-plugin/ops/README.md).

**Note:** Hermes has no skill directory — it uses plugin-based skills only.

---

## Coding Harnesses (MCP)

`colony mcp detect` lists which of these are installed; `colony mcp setup` (no `--harness`) offers the detected ones interactively (defaulting to all), and `colony mcp setup --harness all` configures every detected harness non-interactively.

### Claude Code

Anthropic's official Claude coding agent.

**Setup:**
```bash
colony mcp setup --harness claude-code
```

**Config location:** `~/.claude.json`

```json
{
  "mcpServers": {
    "colony": {
      "command": "colony",
      "args": ["mcp"],
      "env": {
        "COLONY_API_KEY": "${COLONY_API_KEY}",
        "COLONY_URL": "http://127.0.0.1:7777",
        "COLONY_MCP_CONTACT_ID": "user",
        "COLONY_MCP_SOURCE": "claude-code"
      }
    }
  }
}
```

**Note:** Claude Code shares `~/.codex/skills/` with Codex for the `colony-diagnose` skill.

### Codex

OpenAI's terminal coding agent.

**Setup:**
```bash
colony mcp setup --harness codex
```

**Config location:** `~/.codex/config.toml`

```toml
[mcp_servers.colony]
command = "colony"
args = ["mcp"]
env = { COLONY_API_KEY = "${COLONY_API_KEY}", COLONY_URL = "http://127.0.0.1:7777" }
```

### Crush

Charmbracelet's terminal-based coding agent.

**Setup:**
```bash
colony mcp setup --harness crush
```

**Config location:** `~/.crush.json`

```json
{
  "mcp": {
    "colony": {
      "type": "stdio",
      "command": "colony",
      "args": ["mcp"],
      "env": {
        "COLONY_URL": "http://127.0.0.1:7777",
        "COLONY_API_KEY": "your-key",
        "COLONY_MCP_CONTACT_ID": "user",
        "COLONY_MCP_SOURCE": "crush"
      }
    }
  },
  "options": {
    "skills_paths": ["~/.config/crush/skills"]
  }
}
```

Setup also installs the `colony-diagnose` skill to `~/.config/crush/skills/` and adds that directory to `skills_paths`.

**Distributed setup (Colony on a different machine):**
```bash
colony mcp setup --harness crush --sidecar-url http://192.168.1.100:7777 --print-config
```

### OpenCode

Open-source coding agent with MCP support.

**Setup:**
```bash
colony mcp setup --harness opencode
```

**Config location:** `~/.config/opencode/opencode.json`

---

## MCP Tools Reference

Colony exposes 18 MCP tools:

### Memory
| Tool | Description |
|------|-------------|
| `colony_lookup_facts` | Search stored facts by query |
| `colony_remember_fact` | Store a new fact |
| `colony_forget_fact` | Remove a fact |

### Commitments
| Tool | Description |
|------|-------------|
| `colony_check_commitments` | List active commitments |
| `colony_create_commitment` | Create a new commitment |
| `colony_fulfill_commitment` | Mark a commitment as fulfilled |
| `colony_cancel_commitment` | Cancel a commitment |

### Affect
| Tool | Description |
|------|-------------|
| `colony_check_affect` | Get affect state for a contact |
| `colony_record_affect` | Record an affect event |

### Context
| Tool | Description |
|------|-------------|
| `colony_get_context` | Get assembled context for a contact |
| `colony_get_patterns` | Get learned patterns |

### World Model
| Tool | Description |
|------|-------------|
| `colony_search_world` | Search world model entities |

### Tasks & Initiatives
| Tool | Description |
|------|-------------|
| `colony_task_complete` | Mark a task as completed |
| `colony_task_snooze` | Snooze a task for N hours (1–168) |
| `colony_task_dismiss` | Dismiss a task as no longer relevant |
| `colony_initiative_feedback` | Report how an initiative was handled (acknowledged / actioned / dismissed / snoozed) |

### Meta
| Tool | Description |
|------|-------------|
| `colony_health` | Check sidecar health |
| `colony_record_surprise` | Record a surprise event |

The server also exposes MCP **resources** (`colony://status`, `colony://commitments`, `colony://affect/{contact_id}`, `colony://facts/{contact_id}`, `colony://world/entities`, `colony://surprises/unresolved`) and three **prompts** (daily briefing, pre-task, post-task).

---

## API Endpoints Reference

Base URL: `http://127.0.0.1:7777/v1/host/`

Authentication: `Authorization: Bearer {api_key}`

### Key Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/capabilities` | GET | List all capabilities |
| `/mind/facts` | GET, POST | List/store facts |
| `/commitments` | GET, POST | List/create commitments |
| `/context/assemble` | GET | Get full context for a contact |
| `/health` | GET | Sidecar health check |
| `/autonomy/posture` | GET | The resolved autonomy posture of the running process |
| `/self/params` | GET | Adaptive runtime parameters and their journaled values |

### Example Requests

**Store a fact:**
```bash
curl -X POST http://127.0.0.1:7777/v1/host/mind/facts \
  -H "Authorization: Bearer colony" \
  -H "Content-Type: application/json" \
  -d '{"contact_id": "owner", "fact": "Prefers dark mode", "source": "preference"}'
```

**Get context:**
```bash
curl "http://127.0.0.1:7777/v1/host/context/assemble?contact_id=owner" \
  -H "Authorization: Bearer colony"
```

---

## Distributed Setups

When Colony runs on a different machine than your coding harness:

### Colony Server (Machine A)
```bash
# .env
COLONY_SIDECAR_HOST=0.0.0.0  # Bind to all interfaces
COLONY_SIDECAR_PORT=7777
COLONY_API_KEY=your-secure-key
```

### Coding Harness (Machine B)
```bash
# Generate config with remote URL
colony mcp setup --harness crush --sidecar-url http://192.168.1.100:7777 --print-config

# Or set environment variable
export COLONY_SIDECAR_URL=http://192.168.1.100:7777
```

### Security Considerations
- Use HTTPS in production (reverse proxy with TLS)
- Firewall port 7777 to trusted IPs only
- Rotate `COLONY_API_KEY` if it may have leaked

---

## Troubleshooting

### Common Issues

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| Sidecar not running | `colony status` returns error | `colony start -d` |
| 401 Unauthorized | API key mismatch | Check `.env` and harness config match |
| MCP tools not found | Config not loaded | Restart harness, check config syntax |
| Neo4j errors | Database not running | `docker start neo4j` (or your Neo4j service) |
| Connection refused | Firewall/network | Check port 7777 accessible |

### Diagnostic Commands

```bash
# Check sidecar health
colony status

# Test API connectivity
curl -s http://127.0.0.1:7777/v1/host/capabilities -H "Authorization: Bearer colony"

# Check MCP config (Crush)
cat ~/.crush.json | jq '.mcp.colony'

# Detect installed coding harnesses
colony mcp detect

# Run full diagnostics (config + running-server checks)
colony doctor
```

---

## Files Written by Colony

| Harness | Config File | Skill Directory |
|---------|-------------|-----------------|
| Hermes | `~/.hermes/config.yaml` (MCP + plugin) + `~/.hermes/plugins/{colony,colony-memory,context_engine/colony}/` | — |
| Claude Code | `~/.claude.json` (MCP) | `~/.codex/skills/colony-diagnose/` (shared with Codex) |
| Codex | `~/.codex/config.toml` (MCP) | `~/.codex/skills/colony-diagnose/` |
| Crush | `~/.crush.json` (MCP) | `~/.config/crush/skills/colony-diagnose/` |
| OpenCode | `~/.config/opencode/opencode.json` (MCP) | `~/.config/opencode/skills/colony-diagnose/` |

---

## See Also

- [Colony Documentation](https://github.com/Aevonix/ColonyAI)
- [Hermes (NousResearch/hermes-agent)](https://github.com/NousResearch/hermes-agent)
- [MCP Specification](https://modelcontextprotocol.io)
