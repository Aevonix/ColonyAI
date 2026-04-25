# Colony Harness Integration Guide

Colony integrates with multiple agent harnesses to provide shared cognitive context across your development tools.

## Architecture Overview

Colony supports two integration paths:

```
┌─────────────────────────────────────────────────────────────────┐
│                      Colony Sidecar (:7777)                      │
│                                                                  │
│  ┌──────────────────┐              ┌──────────────────┐         │
│  │   Plugin API     │              │    MCP Server    │         │
│  │  (HTTP REST)     │              │    (stdio/SSE)   │         │
│  └────────┬─────────┘              └────────┬─────────┘         │
│           │                                  │                   │
└───────────┼──────────────────────────────────┼───────────────────┘
            │                                  │
            ▼                                  ▼
    ┌───────────────┐                  ┌───────────────┐
    │  Agent Tools  │                  │  Coding Tools │
    │  (OpenClaw,   │                  │  (Crush,      │
    │   Hermes)     │                  │   Codex, etc) │
    └───────────────┘                  └───────────────┘
```

**Plugin Path** — For orchestrator agents:
- OpenClaw: Plugin only
- Hermes: Plugin + MCP (both paths available)

Features:
- Direct HTTP API access
- Configured via plugin slots
- Always-on context integration

**MCP Path** — For coding agents:
- Crush, Codex, Claude Code, OpenCode: MCP only
- Hermes: MCP + Plugin (both paths available)

Features:
- Model Context Protocol via stdio/SSE
- Configured via harness config files
- On-demand tool access

Both paths read/write to the same cognitive stores — facts stored by one harness are immediately visible to others.

---

## Agent Harnesses (Plugin)

### OpenClaw

OpenClaw is an orchestrator agent that uses Colony as its context engine.

**Setup:**
```bash
colony init --agent-harness openclaw
```

**What gets configured:**
1. Colony plugin installed via `openclaw plugins install @aevonix/colonyai`
2. Plugin config written to `~/.openclaw/openclaw.json`:
   - `plugins.slots.contextEngine: colony`
   - `plugins.entries.colony.config.sidecarUrl`
   - `plugins.entries.colony.config.apiKey`
3. `COLONY.md` written to `~/.openclaw/workspace/` (always-loaded context)
4. `colony-diagnose` skill installed to `~/.openclaw/workspace/skills/`

**Requirements:**
- Node.js v22+
- OpenClaw gateway running (`openclaw gateway start`)

### Hermes

Hermes is a memory-augmented agent framework that supports **both MCP and plugin integration**.

**Setup (Plugin path):**
```bash
colony init --agent-harness hermes
```

**Setup (MCP path):**
```bash
colony mcp setup --harness hermes
```

**What gets configured:**

*Plugin path:*
1. Colony configured as MemoryProvider plugin in `~/.hermes/config.yaml`
2. Context injection before each turn

*MCP path:*
1. MCP server entry in `~/.hermes/config.yaml`:
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

**Note:** Hermes has no skill directory — uses plugin-based skills only.

---

## Coding Harnesses (MCP)

### Crush

Charmbracelet's terminal-based coding agent with MCP support.

**Setup:**
```bash
colony mcp setup --harness crush
```

**What gets configured:**
1. MCP server entry in `~/.crush.json` or `~/.config/crush/crush.json`:
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
2. `colony-diagnose` skill installed to `~/.config/crush/skills/`
3. `skills_paths` updated to include skill directory

**Distributed setup (Colony on different machine):**
```bash
colony mcp setup --harness crush --sidecar-url http://192.168.1.100:7777 --print-config
```

### Codex

OpenAI's terminal coding agent.

**Setup:**
```bash
colony mcp setup --harness codex
```

**Config location:** `~/.codex/config.toml`

**Config format:** TOML
```toml
[mcp_servers.colony]
command = "colony"
args = ["mcp"]
env = { COLONY_API_KEY = "${COLONY_API_KEY}", COLONY_URL = "http://127.0.0.1:7777" }
```

### Claude Code

Anthropic's official Claude coding agent.

**Setup:**
```bash
colony mcp setup --harness claude-code
```

**Config location:** `~/.claude.json`

**Config format:** JSON
```json
{
  "mcpServers": {
    "colony": {
      "command": "colony",
      "args": ["mcp"],
      "env": {
        "COLONY_API_KEY": "${COLONY_API_KEY}",
        "COLONY_URL": "http://127.0.0.1:7777"
      }
    }
  }
}
```

**Note:** Claude Code shares `~/.codex/skills/` with Codex for skills.

### OpenCode

Open-source coding agent with MCP support.

**Setup:**
```bash
colony mcp setup --harness opencode
```

**Config location:** `~/.config/opencode/opencode.json`

---

## MCP Tools Reference

Colony exposes 14 MCP tools for cognitive operations:

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
| `colony_fulfill_commitment` | Mark commitment as fulfilled |
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

### Meta
| Tool | Description |
|------|-------------|
| `colony_health` | Check sidecar health |
| `colony_record_surprise` | Record a surprise event |

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
| `/context/assemble` | GET | Get full context for contact |
| `/health` | GET | Sidecar health check |

### Example Requests

**Store a fact:**
```bash
curl -X POST http://127.0.0.1:7777/v1/host/mind/facts \
  -H "Authorization: Bearer colony" \
  -H "Content-Type: application/json" \
  -d '{"contact_id": "marc", "fact": "Prefers dark mode", "source": "preference"}'
```

**Get context:**
```bash
curl "http://127.0.0.1:7777/v1/host/context/assemble?contact_id=marc" \
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
- Restrict API keys to specific operations
- Firewall port 7777 to trusted IPs only

---

## Troubleshooting

### Common Issues

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| Sidecar not running | `colony status` returns error | `colony start -d` |
| 401 Unauthorized | API key mismatch | Check `.env` and harness config match |
| MCP tools not found | Config not loaded | Restart harness, check config syntax |
| Plugin not loading | Gateway not restarted | `openclaw gateway restart` |
| Neo4j errors | Database not running | `docker start neo4j` |
| Connection refused | Firewall/network | Check port 7777 accessible |

### Diagnostic Commands

```bash
# Check sidecar health
colony status

# Test API connectivity
curl -s http://127.0.0.1:7777/v1/host/capabilities -H "Authorization: Bearer colony"

# Check MCP config (Crush)
cat ~/.crush.json | jq '.mcp.colony'

# Check plugin status (OpenClaw)
openclaw plugins list --json | jq '.plugins[] | select(.id=="colony")'

# Run full diagnostics
colony doctor
```

---

## Files Written by Colony

| Harness | Config File | Context File | Skill Directory |
|---------|-------------|--------------|-----------------|
| OpenClaw | `~/.openclaw/openclaw.json` (plugin) | `~/.openclaw/workspace/COLONY.md` | `~/.openclaw/workspace/skills/colony-diagnose/` |
| Hermes | `~/.hermes/config.yaml` (MCP + plugin) | — | — |
| Crush | `~/.crush.json` (MCP) | — | `~/.config/crush/skills/colony-diagnose/` |
| Codex | `~/.codex/config.toml` (MCP) | — | `~/.codex/skills/colony-diagnose/` |
| Claude Code | `~/.claude.json` (MCP) | — | `~/.codex/skills/colony-diagnose/` |
| OpenCode | `~/.config/opencode/opencode.json` (MCP) | — | `~/.config/opencode/skills/colony-diagnose/` |

---

## See Also

- [Colony Documentation](https://github.com/Aevonix/ColonyAI)
- [OpenClaw Documentation](https://docs.openclaw.ai)
- [MCP Specification](https://modelcontextprotocol.io)
