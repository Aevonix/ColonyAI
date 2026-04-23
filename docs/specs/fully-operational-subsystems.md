# Colony Feature Spec: Fully Operational Subsystems

**Version:** 1.0  
**Status:** Planned  
**Scope:** Make all stub-cleaned subsystems fully operational

---

## Overview

The stub cleanup made 5 subsystems fail honestly instead of silently. This spec covers what each one needs to become fully operational. Ordered by impact and effort.

---

## 1. Web Search Orchestrator

**Current state:** WebGatherer returns empty results. No web search capability exists.  
**Goal:** Live web search integrated into the research pipeline.

### Architecture

```
ResearchPipeline → GathererRouter → WebGatherer → SearchProvider → Results → EvidenceItems
```

### New files

```
research/search/
├── __init__.py
├── base.py              # SearchProvider ABC
├── tavily.py            # Tavily API provider
├── serpapi.py           # SerpAPI provider  
├── brave.py             # Brave Search API provider
├── orchestrator.py      # Routes queries, handles rate limits, caches
└── cache.py             # Result cache to avoid duplicate API calls
```

### SearchProvider base class

```python
class SearchResult:
    title: str
    url: str
    snippet: str
    content: Optional[str]  # Full page content if available
    source: str
    rank: int
    retrieved_at: datetime

class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Execute a search query and return results."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def rate_limit_per_minute(self) -> int:
        ...
```

### TavilyProvider

```python
class TavilyProvider(SearchProvider):
    """Tavily search API — designed for AI agents, returns clean content."""

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._base_url = "https://api.tavily.com/search"

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(self._base_url, json={
                "api_key": self._api_key,
                "query": query,
                "max_results": max_results,
                "include_raw_content": True,
                "search_depth": "advanced",
            })
            data = resp.json()
            return [
                SearchResult(
                    title=r["title"],
                    url=r["url"],
                    snippet=r["content"][:300],
                    content=r.get("raw_content"),
                    source="tavily",
                    rank=i,
                    retrieved_at=datetime.now(timezone.utc),
                )
                for i, r in enumerate(data.get("results", []))
            ]
```

### SearchOrchestrator

```python
class SearchOrchestrator:
    """Routes search queries to providers, handles rate limits and caching."""

    def __init__(self, providers: list[SearchProvider], cache_ttl: int = 3600):
        self._providers = {p.name: p for p in providers}
        self._cache = SearchCache(ttl_seconds=cache_ttl)
        self._rate_tracker = {p.name: [] for p in providers}

    async def search(self, query: str, max_results: int = 5, provider: str = "") -> list[SearchResult]:
        # Check cache first
        cached = self._cache.get(query)
        if cached:
            return cached[:max_results]

        # Select provider
        prov = self._select_provider(provider)
        if not prov:
            return []

        # Rate limit check
        if not self._check_rate_limit(prov.name):
            # Fall back to another provider
            prov = self._fallback_provider(prov.name)
            if not prov:
                return []

        # Execute search
        results = await prov.search(query, max_results)
        self._cache.put(query, results)
        return results

    def _select_provider(self, preferred: str) -> SearchProvider | None:
        if preferred and preferred in self._providers:
            return self._providers[preferred]
        # Default: first available
        return next(iter(self._providers.values()), None)
```

### Integration

- Wire `SearchOrchestrator` into `WebGatherer` via the existing `_orchestrator` field
- `WebGatherer.query()` calls `self._orchestrator.search(query)` instead of returning empty
- New env vars: `COLONY_SEARCH_PROVIDER` (tavily/serpapi/brave), `TAVILY_API_KEY`, `SERPAPI_KEY`, `BRAVE_API_KEY`
- Search is optional — if no provider is configured, WebGatherer still returns empty (graceful degradation)
- Add to `colony init` as an optional step: "Configure web search? [y/N]"

### Configuration

```bash
COLONY_SEARCH_PROVIDER=tavily
TAVILY_API_KEY=tvly-xxxxx
COLONY_SEARCH_CACHE_TTL=3600
COLONY_SEARCH_MAX_RESULTS=5
```

### Dependencies

- `httpx` (already a dependency)
- No new required packages — search providers use their REST APIs directly

---

## 2. Native Tool Handlers

**Current state:** ToolExecutor returns error for tools it doesn't recognize. Most tools are handled by the host harness.  
**Goal:** Colony handles common tools natively so the LLM can execute them without host roundtrips.

### Which tools to implement

Based on the existing tool definitions in `colony_sidecar/tools/`:

| Tool | Priority | Description |
|---|---|---|
| `recall` | P0 | Search Colony memory (already wired via VectorStore) |
| `store_memory` | P0 | Store to Colony memory (already wired) |
| `web_search` | P1 | Uses the new SearchOrchestrator above |
| `calculate` | P1 | Safe math evaluation |
| `read_file` | P2 | Read files from a sandboxed directory |
| `write_file` | P2 | Write files to a sandboxed directory |
| `list_directory` | P2 | List files in a sandboxed directory |
| `http_request` | P2 | Make HTTP requests (with allowlist) |

### Architecture

```python
class NativeToolHandler(ABC):
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def schema(self) -> dict:
        """JSON Schema for the tool's parameters."""
        ...

    @abstractmethod
    async def execute(self, args: dict) -> dict: ...
```

### CalculateTool

```python
class CalculateTool(NativeToolHandler):
    """Safe math evaluation — no eval(), no exec()."""

    ALLOWED_NAMES = {
        "abs": abs, "round": round, "min": min, "max": max,
        "sum": sum, "len": len, "int": int, "float": float,
        "pow": pow, "divmod": divmod,
    }

    def name(self) -> str:
        return "calculate"

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Math expression to evaluate"},
            },
            "required": ["expression"],
        }

    async def execute(self, args: dict) -> dict:
        expr = args.get("expression", "")
        # Parse AST — only allow numeric operations
        tree = ast.parse(expr, mode="eval")
        result = self._eval_node(tree.body)
        return {"result": result, "expression": expr}

    def _eval_node(self, node) -> float:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.ALLOWED_NAMES:
                return self.ALLOWED_NAMES[node.id]
            raise ValueError(f"Name not allowed: {node.id}")
        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            ops = {
                ast.Add: operator.add, ast.Sub: operator.sub,
                ast.Mult: operator.mul, ast.Div: operator.truediv,
                ast.Mod: operator.mod, ast.Pow: operator.pow,
            }
            return ops[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
        if isinstance(node, ast.Call):
            func = self._eval_node(node.func)
            args = [self._eval_node(a) for a in node.args]
            return func(*args)
        raise ValueError(f"Operation not allowed: {type(node).__name__}")
```

### WebSearchTool

```python
class WebSearchTool(NativeToolHandler):
    """Web search via Colony's SearchOrchestrator."""

    def __init__(self, orchestrator: SearchOrchestrator):
        self._orchestrator = orchestrator

    def name(self) -> str:
        return "web_search"

    async def execute(self, args: dict) -> dict:
        query = args.get("query", "")
        max_results = args.get("max_results", 5)
        results = await self._orchestrator.search(query, max_results)
        return {
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ],
            "count": len(results),
        }
```

### FileTools (sandboxed)

```python
class ReadFileTool(NativeToolHandler):
    """Read files from a sandboxed directory."""

    def __init__(self, sandbox_dir: str):
        self._sandbox = Path(sandbox_dir).resolve()

    def name(self) -> str:
        return "read_file"

    async def execute(self, args: dict) -> dict:
        path = self._resolve_safe(args.get("path", ""))
        if not path.exists():
            return {"error": True, "message": f"File not found: {args['path']}"}
        content = path.read_text(errors="replace")
        return {"content": content, "path": str(path.relative_to(self._sandbox))}

    def _resolve_safe(self, path: str) -> Path:
        """Resolve path within sandbox — reject path traversal."""
        resolved = (self._sandbox / path).resolve()
        if not str(resolved).startswith(str(self._sandbox)):
            raise ValueError("Path traversal detected")
        return resolved
```

### Integration

- Register native handlers in `ToolExecutor.__init__()` during server startup
- ToolExecutor checks native handlers first, then host harness tools
- New env var: `COLONY_SANDBOX_DIR` (default: `{state_dir}/sandbox/`)
- `colony init` creates the sandbox directory

---

## 3. Autonomy Scheduler

**Current state:** Autonomy loop logs "scheduler not yet implemented" and skips cron tasks.  
**Goal:** Periodic task scheduling within the autonomy loop.

### Architecture

```
AutonomyLoop → Scheduler → TaskSchedule → registered callbacks
```

### New files

```
autonomy/
├── scheduler.py         # Cron-like periodic task scheduler
└── schedule_store.py    # SQLite-backed schedule persistence
```

### TaskSchedule

```python
class TaskSchedule:
    id: str
    name: str
    interval_seconds: int
    callback_name: str        # Name of the registered callback
    last_run: Optional[datetime]
    next_run: datetime
    enabled: bool
    metadata: dict
```

### Scheduler

```python
class AutonomyScheduler:
    """Lightweight periodic task scheduler for the autonomy loop.

    Not a full cron daemon — just enough for Colony's periodic needs:
    - Memory consolidation
    - Briefing generation
    - Signal ingestion
    - CPI tracking
    - Health self-checks
    """

    def __init__(self, db_path: str):
        self._store = ScheduleStore(db_path)
        self._callbacks: Dict[str, Callable] = {}

    def register(self, name: str, callback: Callable, interval_seconds: int, metadata: dict = None):
        """Register a periodic task."""
        schedule = TaskSchedule(
            id=str(uuid.uuid4()),
            name=name,
            interval_seconds=interval_seconds,
            callback_name=name,
            next_run=datetime.now(timezone.utc),
            enabled=True,
            metadata=metadata or {},
        )
        self._store.upsert(schedule)
        self._callbacks[name] = callback

    async def tick(self) -> list[dict]:
        """Check and execute all due tasks. Called by the autonomy loop."""
        due = self._store.get_due()
        results = []
        for task in due:
            callback = self._callbacks.get(task.callback_name)
            if callback:
                try:
                    result = await callback() if asyncio.iscoroutinefunction(callback) else callback()
                    results.append({"task": task.name, "status": "ok", "result": result})
                except Exception as e:
                    results.append({"task": task.name, "status": "error", "error": str(e)})
                self._store.update_last_run(task.id)
        return results

    def list_schedules(self) -> list[TaskSchedule]:
        return self._store.list_all()

    def enable(self, task_id: str): ...
    def disable(self, task_id: str): ...
```

### Default schedules

Registered during server startup:

| Task | Interval | Purpose |
|---|---|---|
| `memory_consolidate` | 3600s (1hr) | Deduplicate and merge near-duplicate memories |
| `briefing_generate` | 1800s (30min) | Generate proactive briefings for active contacts |
| `signal_ingest` | 600s (10min) | Process queued behavioral signals |
| `cpi_track` | 86400s (24hr) | Calculate Cognitive Performance Index |
| `health_check` | 300s (5min) | Run subsystem health check |
| `world_model_prune` | 86400s (24hr) | Remove stale world model entities |

### Integration

- `AutonomyLoop.wake()` calls `self._scheduler.tick()` as part of each cycle
- New API endpoints:
  - `GET /v1/host/autonomy/schedule` — list all scheduled tasks
  - `POST /v1/host/autonomy/schedule/{id}/enable` — enable a task
  - `POST /v1/host/autonomy/schedule/{id}/disable` — disable a task
- Schedules persist in SQLite, survive restarts
- `colony doctor --full` checks that the scheduler is running

---

## 4. ColonyGraph Wiring for All Environments

**Current state:** Memory write returns 501 when ColonyGraph isn't wired. Works on Spark 2 because Neo4j + embeddings are both running.  
**Goal:** ColonyGraph wires reliably in all environments with proper error handling.

### What needs to happen

The wiring is fragile — it depends on Neo4j being connected AND embeddings being initialized, in the right order. If either fails silently, memory writes silently fail.

### Fix: Explicit wiring verification

```python
async def _ensure_colony_graph_wired(self):
    """Verify ColonyGraph is fully operational. Returns True if wired."""
    if not self._graph:
        return False
    
    # Check Neo4j connectivity
    try:
        await self._graph.client.verify_connectivity()
    except Exception:
        return False
    
    # Check embedding pipeline
    if not self._embed_fn:
        return False
    
    # Check vector store
    if not self._vector_store:
        return False
    
    return True
```

### Fix: Wiring order guarantee in server.py

```python
# 1. Neo4j must be connected first
if not neo4j_connected:
    logger.error("Neo4j not connected — memory subsystem unavailable")
    return

# 2. Embedding pipeline must be initialized
if not embed_pipeline:
    logger.error("Embedding pipeline not initialized — memory subsystem unavailable")
    return

# 3. Then wire ColonyGraph
colony_graph = ColonyGraph(neo4j_client)
colony_graph.set_embed_fn(embed_pipeline.embed)
colony_graph.set_vector_store(vector_store)
await vector_store.connect(dimensions=embed_dims)
await vector_store.ensure_collections(dimensions=embed_dims)

# 4. Verify
if await colony_graph.verify():
    logger.info("ColonyGraph fully operational")
else:
    logger.error("ColonyGraph verification failed — memory subsystem degraded")
```

### Fix: Colony init validation

`colony init` should verify the wiring end-to-end:

```python
# After init completes:
print("  Verifying memory subsystem...")
r = requests.post(f"http://localhost:{port}/v1/host/memory/write", ...)
if r.status_code == 200:
    print("  ✅ Memory subsystem operational")
elif r.status_code == 501:
    print("  ⚠️  Memory subsystem not wired — check Neo4j and embedding config")
```

### New API endpoint

```
GET /v1/host/memory/status
→ { wired: bool, neo4j_connected: bool, embeddings_ready: bool, vector_store_ready: bool }
```

This gives `colony doctor` a clear diagnostic for why memory isn't working.

---

## 5. Additional World Model Backends

**Current state:** Falls back to sqlite. Only sqlite backend works.  
**Goal:** Support Postgres as an alternative for larger deployments.

### Architecture

```python
class WorldModelBackend(ABC):
    @abstractmethod
    async def find_entities(self, query: str, limit: int) -> list[Entity]: ...

    @abstractmethod
    async def upsert_entity(self, entity: Entity) -> str: ...

    @abstractmethod
    async def delete_entity(self, entity_id: str) -> bool: ...

    @abstractmethod
    async def connect(self) -> None: ...
```

### PostgresBackend

```python
class PostgresBackend(WorldModelBackend):
    def __init__(self, connection_string: str):
        self._conn_string = connection_string
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        self._pool = await asyncpg.create_pool(self._conn_string, min_size=2, max_size=10)
        await self._create_tables()

    async def _create_tables(self):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS world_entities (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    attributes JSONB DEFAULT '{}',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_entities_type ON world_entities(type);
                CREATE INDEX IF NOT EXISTS idx_entities_name ON world_entities USING gin(to_tsvector('english', name));
            """)

    async def find_entities(self, query: str, limit: int) -> list[Entity]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM world_entities WHERE to_tsvector('english', name) @@ to_tsquery($1) LIMIT $2",
                query, limit,
            )
            return [self._row_to_entity(r) for r in rows]

    async def upsert_entity(self, entity: Entity) -> str:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO world_entities (id, name, type, attributes, updated_at)
                   VALUES ($1, $2, $3, $4, NOW())
                   ON CONFLICT (id) DO UPDATE SET name=$2, type=$3, attributes=$4, updated_at=NOW()""",
                entity.id, entity.name, entity.entity_type, json.dumps(entity.attributes),
            )
            return entity.id
```

### Configuration

```bash
WORLD_MODEL_BACKEND=postgres       # or sqlite (default)
WORLD_MODEL_PG_CONNECTION=postgresql://user:pass@host:5432/colony
```

### Dependencies

```toml
[project.optional-dependencies]
postgres = ["asyncpg>=0.29"]
```

---

## Implementation Order

| # | Feature | Effort | Depends on |
|---|---|---|---|
| 1 | Web Search Orchestrator | 1 day | Nothing |
| 2 | Native Tool Handlers | 2 days | Web Search Orchestrator (for web_search tool) |
| 3 | Autonomy Scheduler | 1 day | Nothing |
| 4 | ColonyGraph Wiring Fix | 3 hours | Nothing |
| 5 | Postgres Backend | 1 day | Nothing |

**Recommended order:** 4 → 3 → 1 → 2 → 5

Items 4 and 3 are quick wins. Item 1 unlocks item 2. Item 5 is independent and can ship whenever.

---

## API Endpoints Summary

### New

```
GET  /v1/host/memory/status                        — Memory subsystem diagnostic
GET  /v1/host/autonomy/schedule                    — List scheduled tasks
POST /v1/host/autonomy/schedule/{id}/enable        — Enable a task
POST /v1/host/autonomy/schedule/{id}/disable       — Disable a task
```

### Updated

```
POST /v1/host/reasoning/turn                       — Now has native calculate + web_search tools
POST /v1/host/research/start                       — Now has live web search via orchestrator
GET  /v1/host/autonomy/cycle                       — Now executes scheduled tasks
```

---

## Configuration Summary

```bash
# Web search (optional)
COLONY_SEARCH_PROVIDER=tavily          # tavily | serpapi | brave
TAVILY_API_KEY=tvly-xxxxx
SERPAPI_KEY=xxxxx
BRAVE_API_KEY=xxxxx
COLONY_SEARCH_CACHE_TTL=3600
COLONY_SEARCH_MAX_RESULTS=5

# File sandbox (optional)
COLONY_SANDBOX_DIR=/path/to/sandbox    # default: {state_dir}/sandbox/

# World model backend
WORLD_MODEL_BACKEND=sqlite             # sqlite | postgres
WORLD_MODEL_PG_CONNECTION=postgresql://user:pass@host:5432/colony
```
