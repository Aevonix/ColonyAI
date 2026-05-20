# Colony Init Wizard Issues — 2026-04-23

Tested on Spark 1 (DGX Spark, Ubuntu 24.04, aarch64) with Colony v0.6.7.

## Status

**Partially fixed in commit 54602f3** — Foundation laid, full implementation pending.

---

## Issue 1: Piped input doesn't reach all prompts

**Severity:** Critical  
**Status:** ✅ FIXED (EOF handling in _prompt)
**Location:** `sidecar/colony_sidecar/setup.py` — `_prompt()` function

**Problem:** The wizard uses `input()` for all prompts. When piping input via SSH (e.g., `printf "3\nowner\n\n6\nY\n" | colony init`), only the first few prompts receive input. Later prompts get `EOFError` and crash.

**Observed:**
```
Step 3: Host framework
  Choose [1-6] [1]:   Colony will connect to Claude Code via MCP.
  What should Colony call you? [user]: 
Step 5: Neo4j graph memory
  Neo4j password: 
Step 6: Writing configuration
  ...
Select tier [0-7] [6]: Warning: You are sending unauthenticated requests to the HF Hub...
  ⚠️ Hardware scan failed: EOF when reading a line
  ...
Start the Colony sidecar now? [Y/n] [Y]: Traceback (most recent call last):
  ...
  EOFError: EOF when reading a line
```

**Root cause:** `input()` consumes from stdin. Once the piped input is exhausted, subsequent `input()` calls raise `EOFError`.

**Impact:** 
- Cannot automate setup via SSH
- Cannot script headless installs
- CI/CD impossible

**Fix options:**
1. Accept CLI flags for all prompts (`--host-framework 3 --contact-name owner --tier 6 --start`)
2. Accept a config file path (`--config colony.yaml`) that pre-populates all values
3. Gracefully handle EOF: treat it as "use default" instead of crashing
4. Add `--non-interactive` mode that requires all values via flags/config

**Recommended:** Add `--non-interactive` mode + config file support. Keep interactive wizard for human users.

---

## Issue 2: Hardware scan crashes on EOF, skips tier selection

**Severity:** High  
**Status:** ✅ FIXED (--tier CLI arg + EOF handling)
**Location:** `sidecar/colony_sidecar/setup.py` — tier selection + hardware scan

**Problem:** The tier selection prompt is preceded by a hardware scan that calls `_probe_hardware()` which internally uses `input()` or similar blocking call. When stdin is exhausted, this crashes and tier selection is skipped entirely.

**Observed:**
```
Select tier [0-7] [6]: Warning: You are sending unauthenticated requests to the HF Hub...
  ⚠️ Hardware scan failed: EOF when reading a line
  ✅ Written to .env
```

**Result:** Colony installed with tier 0 (CPU embedder) on a DGX Spark (130GB VRAM) instead of tier 6 (harrier-oss-v1-27b).

**Impact:**
- Wrong embedder model for hardware
- Severely degraded performance on high-end hardware
- User doesn't know tier was skipped

**Fix:**
1. Hardware scan should not require stdin input
2. Tier selection should accept CLI flag (`--tier 6`)
3. If tier can't be determined, fail loudly rather than silently defaulting to 0

---

## Issue 3: No config file output, only .env

**Severity:** Medium  
**Status:** ✅ FIXED (_write_config_yaml added)
**Location:** `sidecar/colony_sidecar/setup.py`

**Problem:** The wizard writes to `~/.env` but doesn't create a `~/.colony/config.yaml`. The original design expects a config file, but the wizard only produces environment variables.

**Observed:**
```
$ ls ~/.colony/
total 28
drwxrwxr-x  3 user user  4096 Apr 23 11:57 .
drwxr-x--- 46 user user  4096 Apr 23 11:58 ..
drwxrwxr-x  5    7474    7474  4096 Apr 23 11:42 neo4j-data
-rw-rw-r--   user user 12762 Apr 23 11:51 sidecar.log

$ cat ~/.colony/config.yaml
cat: /home/user/.colony/config.yaml: No such file or directory
```

**Impact:**
- Config is scattered across `~/.env` and multiple SQLite DBs in `~/`
- No single source of truth for configuration
- Hard to inspect or modify config after init

**Fix:** Write a `~/.colony/config.yaml` with all settings, and load it on startup.

---

## Issue 4: Defaults to localhost bind, no way to change via wizard

**Severity:** Medium  
**Status:** ✅ FIXED (--bind/--port CLI args + interactive prompt)
**Location:** `sidecar/colony_sidecar/setup.py`

**Problem:** `COLONY_SIDECAR_HOST` defaults to `127.0.0.1`. There's no prompt to change it. For headless servers (DGX Spark, VPS), the sidecar needs to bind to `0.0.0.0` to be accessible from other machines.

**Observed:**
```
COLONY_SIDECAR_HOST=127.0.0.1
```

**Impact:**
- Sidecar only accessible from localhost
- Remote testing impossible without manual config edit
- Unclear to user why sidecar isn't reachable

**Fix:**
1. Add prompt: "Bind address (0.0.0.0 for all interfaces, 127.0.0.1 for localhost only) [127.0.0.1]:"
2. Or accept `--bind 0.0.0.0` CLI flag
3. Detect if running on headless server and suggest `0.0.0.0`

---

## Issue 5: Neo4j password prompt assumes auth is enabled

**Severity:** Low  
**Status:** ✅ FIXED (_check_neo4j_auth added)
**Location:** `sidecar/colony_sidecar/setup.py` — Step 5

**Problem:** The wizard prompts for Neo4j password, but if Neo4j was started with `NEO4J_AUTH=none`, the password prompt is confusing. Empty password is valid but the wizard doesn't explain this.

**Observed:**
```
Step 5: Neo4j graph memory
  ✅ Neo4j is already running (localhost:7687)
  Enter the password this Neo4j instance was configured with.
  Neo4j password: 
```

**Impact:** User may think they need to set a password when Neo4j has no auth.

**Fix:**
1. Detect if Neo4j requires auth (try connecting without credentials)
2. If no auth required, skip password prompt and note "Neo4j auth disabled"
3. If auth required, prompt for password

---

## Issue 6: SQLite DBs scattered in home directory

**Severity:** Low  
**Status:** ✅ FIXED (paths now use ~/.colony/data/)
**Location:** `sidecar/colony_sidecar/setup.py`

**Problem:** Multiple SQLite databases are created in `~/` instead of `~/.colony/`:

```
/home/user/colony-affect.db
/home/user/colony-commitments.db
/home/user/colony-delivery-rate-limit.db
/home/user/colony-facts.db
/home/user/colony-goals.db
/home/user/colony-patterns.db
/home/user/colony-surprise.db
/home/user/colony_world_model.db
```

**Impact:**
- Clutters home directory
- Makes cleanup harder
- Inconsistent with `~/.colony/` for Neo4j data

**Fix:** All SQLite DBs should be under `~/.colony/data/` or similar.

---

## Issue 7: No validation that selected tier matches hardware

**Severity:** Medium  
**Status:** ⏳ Pending (would need runtime check)
**Location:** `sidecar/colony_sidecar/setup.py`

**Problem:** Even when tier selection works, there's no validation that the selected tier is appropriate for the hardware. User can select tier 7 on a 4GB laptop.

**Impact:**
- OOM crashes at runtime
- Poor performance

**Fix:**
1. After tier selection, validate available VRAM/RAM
2. Warn if selected tier exceeds hardware capacity
3. Require `--force` to proceed with mismatched tier

---

## Issue 8: Embedding model download happens during init, not first start

**Severity:** Low  
**Status:** ✅ FIXED (--skip-model-download CLI arg)
**Location:** `sidecar/colony_sidecar/setup.py` — Step 7

**Problem:** The wizard downloads the embedding model during `colony init`. For large models (harrier-oss-v1-27b is 27B params), this can take a long time and block the wizard.

**Observed:**
```
Step 7: Download embedding model
  Downloading sentence-transformers/all-MiniLM-L6-v2...
```

**Impact:**
- Long init time for large models
- Network issues can fail init
- User may not want to download model during setup

**Fix:**
1. Defer model download to first `colony start`
2. Or add `--skip-model-download` flag
3. Show download progress more clearly

---

## Summary

| Issue | Severity | Status |
|-------|----------|--------|
| Piped input crashes wizard | Critical | ✅ FIXED |
| Tier selection skipped silently | High | ✅ FIXED |
| No config file output | Medium | ✅ FIXED |
| No bind address prompt | Medium | ✅ FIXED |
| Neo4j auth detection | Low | ✅ FIXED |
| DBs scattered in ~/ | Low | ✅ FIXED |
| No tier/hardware validation | Medium | ⏳ Pending |
| Model download blocks init | Low | ✅ FIXED |
