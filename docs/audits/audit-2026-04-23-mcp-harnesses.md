# MCP Server & Harness Integration Audit — 2026-04-23

Scope: `sidecar/colony_sidecar/mcp/` (MCP server + harness config writer),
`plugins/hermes-memory/` (Hermes memory provider), the CLI glue in
`sidecar/colony_sidecar/cli.py`, and the related test suites in
`sidecar/tests/test_mcp_server.py` and `sidecar/tests/test_mcp_config.py`.

All file paths are repo-relative. Line numbers reflect the tree on
`claude/audit-mcp-server-GDu5f` at the time of the audit.

---

## Critical — feature visibly broken

### 1. Hermes memory-context block is always empty

`plugins/hermes-memory/provider.py:195`

```python
content = section.get("content", "")
```

The sidecar's `ContextSection` schema (`sidecar/colony_sidecar/api/schemas/host.py:125`)
uses field `body`, not `content`. Every section's content is stripped,
so Hermes injects an empty `<memory-context>` with only
`## {id} [priority {n}]` headers and no body. This silently defeats the
entire Hermes integration — the primary feature (prefetch context)
produces no visible context.

Fix: read `section.get("body", "")`, and use
`section.get("title", section.get("id", "colony-context"))` for the
header.

### 2. `COLONY_MCP_SOURCE` provenance tracking is a no-op

`sidecar/colony_sidecar/mcp/server.py:72-88`

`_post` injects `data["provenance"] = src`, but no sidecar Pydantic
model has a `provenance` field (confirmed by grep across
`api/schemas/host.py` and `api/routers/host.py`). Pydantic's default is
`extra="ignore"`, so the value is dropped. The feature advertised by
`colony mcp setup` (per-harness attribution) never lands in the store.

Fix: either add `provenance` to the relevant create-request schemas, or
map onto an existing field (`source_type` for commitments, `source` for
facts/affect, or `metadata["provenance"]` as a generic fallback).

### 3. `_post` unconditionally strips `source`

`sidecar/colony_sidecar/mcp/server.py:79`

```python
data.pop("source", None)
```

- `SharedFactCreateRequest.source`
  (`Literal["told_by_contact", "told_to_contact", "shared_context", "inferred"]`)
  and `AffectEventCreateRequest.source`
  (`Literal["explicit", "inferred", "signal"]`) are legitimate enums. If
  a future tool sets them, this line silently erases them.
- The two tests in `test_mcp_server.py` that target this helper
  (`test_post_injects_source` at line 193, and
  `test_post_doesnt_override_existing_source` at line 218) assert the
  *opposite* behavior — they expect the captured payload to still
  contain `source`. Both tests fail against the current implementation.

Fix: remove the unconditional pop, or clearly separate "MCP provenance"
from the sidecar `source` enum and update the tests.

### 4. `colony_record_surprise` silently drops `actual`

`sidecar/colony_sidecar/mcp/server.py:313-328` vs schema at
`sidecar/colony_sidecar/api/schemas/host.py:1034`

Schema fields: `observation`, `expected`, `surprise_score`,
`pattern_id`, `context`, `auto_score`. The MCP tool sends
`actual=...` as a top-level field; Pydantic ignores it. Agents guided
by the docstring lose half of what they intended to record.

Fix: fold `actual` into `observation`
(e.g. `f"expected {expected}, got {actual}"`) or remove the parameter.

### 5. Several tool params are silently ignored by the sidecar

- `colony_lookup_facts(category=...)` →
  `list_shared_facts` accepts only `contact_id`, `source`,
  `min_confidence` (`routers/host.py:3510`). `category` is dropped.
- `colony_get_patterns(category=...)` →
  `list_patterns` accepts `pattern_type`, `min_frequency`, `source`,
  `active_only`. `category` is dropped; should almost certainly be
  `pattern_type`.
- `colony_remember_fact(category=...)` →
  `SharedFactCreateRequest` has no `category`
  (`api/schemas/host.py:947`).

Agents get no feedback; filters simply don't apply. Fix by renaming
params to match the schema, or by dropping them from the tool
signatures.

### 6. `colony://surprises/unresolved` returns *all* surprises

`sidecar/colony_sidecar/mcp/server.py:357-360`

```python
return await _get("/v1/host/surprises", params={"status": "unresolved"})
```

The route (`routers/host.py:3731`) declares only `min_score`,
`resolved`, `limit`, `offset`. `status` is ignored by FastAPI, so the
resource returns every surprise, resolved or not.

Fix: call `/v1/host/surprises/unresolved` (the dedicated endpoint at
`routers/host.py:3755`) or use `params={"resolved": False}`.

### 7. `colony_check_commitments` forces a contact_id

`sidecar/colony_sidecar/mcp/server.py:155-170`

`_require_contact(person_id)` errors out if no contact is set, yet the
`if cid: params["person_id"] = cid` branch clearly anticipates optional
scoping. With the default MCP install (no
`COLONY_MCP_CONTACT_ID` set), agents can't list pending commitments
globally — they only receive `contact_id_required`.

Fix: use `_contact_id(person_id)` and add the param only when present.

### 8. `colony_create_commitment` default priority is on the wrong scale

`sidecar/colony_sidecar/mcp/server.py:231`

Tool default: `priority: int = 2`. Schema default
(`api/schemas/host.py:850`): `priority: int = Field(default=50, ge=0, le=100)`.
A priority of `2` is nearly the lowest bucket, not "medium-low" as the
scale-of-5 intuition suggests. The current default is likely to create
commitments that never surface.

Fix: match the schema's scale and default (50), or document the 0-100
scale in the docstring.

---

## High — support regressions / test breakage

### 9. Test suite is out of sync with the Hermes addition

`sidecar/tests/test_mcp_config.py`

- Line 36: `assert len(HARNESS_DEFS) == 4` — actual is 5
  (`claude-code`, `codex`, `crush`, `opencode`, `hermes`).
- Line 47: `valid = {"json", "toml"}` — `hermes` uses `yaml`, so
  `test_config_formats_are_valid` fails.
- Line 61: `assert len(result) == 4` — same root cause as above.

CI on this branch will be red the moment these run.

### 10. CLI `--harness` choices exclude `opencode` and `hermes`

`sidecar/colony_sidecar/cli.py:82, 87`

```python
mcp_setup.add_argument("--harness",
    choices=["claude-code", "codex", "crush", "all"], ...)
mcp_remove.add_argument("--harness",
    choices=["claude-code", "codex", "crush", "all"], ...)
```

`colony mcp setup --harness opencode` or `--harness hermes` fails
argparse validation, even though both are in `HARNESS_DEFS` and
detection works for them. Same incomplete list at
`cli.py:1120` (setup-wizard auto-prompt).

### 11. Hermes plugin class does not subclass any ABC

`plugins/hermes-memory/provider.py:21`

The module docstring and `SKILL.md` say "Implements Hermes's
MemoryProvider ABC," but `class ColonyMemoryProvider:` has no base
class and imports no ABC. If Hermes's plugin loader does
`isinstance(provider, MemoryProvider)` or inspects `__mro__`, the
plugin will be rejected. Duck typing only works if the host accepts it;
the commit log
("Fix Hermes plugin: use MemoryProvider instead of ContextEngine")
suggests the interface matters.

Fix: import `MemoryProvider` from the Hermes SDK and subclass it, or
document that Hermes expects duck typing only.

### 12. Synchronous `httpx` calls from the Hermes provider

`plugins/hermes-memory/provider.py:54, 86, 131`

`is_available`, `prefetch`, and `sync_turn` all use blocking
`httpx.get` / `httpx.post`. If Hermes runs these on its asyncio loop
they'll freeze it for the call's duration (up to 10s). Either use
`httpx.AsyncClient` with `async def` methods or wrap with
`asyncio.to_thread`.

### 13. `${COLONY_API_KEY}` env interpolation is not portable

`sidecar/colony_sidecar/mcp/config.py:87, 196`

The generated configs emit literal `"${COLONY_API_KEY}"` strings.
Claude Code expands `${VAR}` in MCP env blocks, but not every harness
does — OpenCode and Crush in particular have historically passed the
raw string through. Users will see auth failures with no diagnostic.

Fix: at `mcp setup` time, resolve the current `COLONY_API_KEY` and
write the concrete value (or omit the key if empty), and document
which harnesses perform expansion.

---

## Medium — latent issues, confusing code

### 14. Duplicate class definitions in schemas

`sidecar/colony_sidecar/api/schemas/host.py`

`ContextAssembleRequest` is defined at **line 114 and again at line 316**;
`ContextSection` at 122 and 324. The second overrides the first
silently. A future edit to only one set will diverge without warning.

Fix: delete one copy.

### 15. `_add_to_toml_config` never updates stale configs

`sidecar/colony_sidecar/mcp/config.py:152-155`

```python
if "[mcp_servers.colony]" in content:
    return None  # Already present
```

If the user upgrades (port change, new env var), the old block is left
in place and the "already configured" message lies. The JSON path
compares values properly; the TOML path should too.

### 16. Hermes provider has a no-op property indirection

`plugins/hermes-memory/provider.py:182-188`

```python
@property
def _contact_id(self): return self.__contact_id
@_contact_id.setter
def _contact_id(self, value): self.__contact_id = value
```

This works (data-descriptor on the class, name-mangled instance attr),
but adds nothing except confusion; a plain attribute is identical.
Likely a refactor leftover — delete.

### 17. Prefetch cache has a race and does duplicate work

`plugins/hermes-memory/provider.py:111-119`

`queue_prefetch` clears `_cached_context` and fires a daemon thread.
If `prefetch` is called on the main thread before the background thread
writes, it sees empty and issues its own HTTP round-trip (duplicate
call). No lock, no `Event` for "ready."

Fix: use `threading.Event` or a `Future` / `Lock`.

### 18. `queue_prefetch` doesn't wait for a prior thread

`plugins/hermes-memory/provider.py:118`

Back-to-back calls spawn overlapping threads; the last writer wins and
earlier results leak. Join the previous thread or cancel it.

### 19. `__init__.py` silently hides missing MCP SDK

`sidecar/colony_sidecar/mcp/__init__.py:3-7`

```python
try:
    from colony_sidecar.mcp.server import create_server, ...
except ImportError:
    create_server = run_stdio = run_http = None
```

`ImportError` will mask any import-time failure inside `server.py`
(not just a missing `mcp` package), and callers of `create_server()`
then get `TypeError: 'NoneType' object is not callable` with no hint.

Fix: check for the `mcp` module specifically
(`importlib.util.find_spec("mcp")`), or let the error surface.

### 20. `colony_get_context` and `colony_search_world` hardcode `session_id="mcp"`

`sidecar/colony_sidecar/mcp/server.py:148, 355`

Every MCP-initiated call uses the same session id. The sidecar session
store will mix all MCP invocations into one logical session, ruining
session-scoped analytics.

Fix: generate a per-process UUID at server start, or read
`COLONY_MCP_SESSION_ID`.

### 21. `mcp[cli]>=1.0` has no upper bound

`sidecar/pyproject.toml:25`

`FastMCP(..., instructions=...)` accepts these today but the
constructor signature has changed across `mcp` SDK versions. A future
2.x could break the call shape or `transport` enums.

Fix: add an upper bound or pin to the tested minor
(e.g. `mcp[cli]>=1.0,<2.0`).

### 22. Port read at config-write time, not runtime

`sidecar/colony_sidecar/mcp/config.py:88, 143, 176`

`COLONY_SIDECAR_PORT` is baked into the harness config when
`colony mcp setup` runs. If the user later changes the port, every
harness keeps calling the stale URL until `setup` is re-run.

Fix: prefer emitting `${COLONY_URL}` or `${COLONY_SIDECAR_PORT}` with a
note about expansion support; same caveat as bug 13.

### 23. `_read_json` eats `JSONDecodeError` and returns `{}`

`sidecar/colony_sidecar/mcp/config.py:98-104`

If the user's `~/.claude.json` has a syntax error, Colony silently
treats it as empty and overwrites it on save — destroying their other
MCP servers.

Fix: raise, or write a timestamped `.bak` before overwriting.

---

## Low — polish

### 24. `_delete` returns `-1` for unhelpful error

`sidecar/colony_sidecar/mcp/server.py:104-110`; `colony_forget_fact`
reports `Status -1` without context. Preserve the exception string.

### 25. `_post` / `_get` mutate the caller's dict

`sidecar/colony_sidecar/mcp/server.py:76-79`. Harmless today (callers
don't reuse the dict), but surprising. Copy before mutating.

### 26. `mcp setup --harness all` inconsistency

Interactive mode iterates *detected* harnesses; explicit `all` goes
through all defs. Minor mismatch with the remove path.

### 27. `install.sh` doesn't `chmod +x` itself

`plugins/hermes-memory/install.sh`. Works with `bash install.sh`, fails
if users `./install.sh` after a fresh clone unless the git filemode is
preserved. Add a note or set the bit in the repo.

### 28. `r.text[:200]` truncation hides debugging info

`sidecar/colony_sidecar/mcp/server.py:65, 84`. A 422 from Pydantic is
usually under 2kB; 200 chars often truncates mid-field. 1000 chars is
still safe.

---

## Suggested quick wins

In priority order, the shortest path to restoring advertised behavior:

1. Hermes `_format_sections`: `content` → `body`
   (one-line fix that unblocks the whole integration).
2. Remove the broken `provenance` injection — or add it to the schemas.
3. Drop / rename `category` params in `colony_lookup_facts`,
   `colony_get_patterns`, `colony_remember_fact`.
4. Fix `colony://surprises/unresolved` to hit the dedicated endpoint.
5. Make `colony_check_commitments` work without a contact_id.
6. Update `test_mcp_config.py` assertions to reflect 5 harnesses and
   the `yaml` format.
7. Add `opencode`, `hermes` to the `--harness` choices in
   `cli.py:82, 87`.

---

## Method

- Read `sidecar/colony_sidecar/mcp/server.py` (424 lines),
  `sidecar/colony_sidecar/mcp/config.py` (308 lines),
  `sidecar/colony_sidecar/mcp/__init__.py`,
  `sidecar/colony_sidecar/mcp/__main__.py`,
  `plugins/hermes-memory/provider.py`, `SKILL.md`, `install.sh`,
  `sidecar/tests/test_mcp_server.py`,
  `sidecar/tests/test_mcp_config.py`, and the relevant portions of
  `sidecar/colony_sidecar/cli.py`.
- Cross-referenced every outgoing request in the MCP server against
  the corresponding FastAPI route in
  `sidecar/colony_sidecar/api/routers/host.py` and the Pydantic model
  in `sidecar/colony_sidecar/api/schemas/host.py`.
- Grepped the whole sidecar for `provenance` to confirm no schema or
  route accepts that field.
- No code was modified as part of this audit.
