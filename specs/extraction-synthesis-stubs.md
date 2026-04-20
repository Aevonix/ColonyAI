# Colony Feature Spec: Extraction Formats + Code Synthesis + Stub Cleanup

**Version:** 1.0  
**Status:** Planned  
**Scope:** One dev push

---

## Part 1: Stub Cleanup

Fix all stubs that return fake data instead of degrading gracefully. These are quick fixes.

### 1.1 WebGatherer stub → empty results

**File:** `research/gatherer.py:125-131`

**Before:**
```python
content=f"[web stub] No live web search available. Query: {query}",
citation="stub://web",
```

**After:**
```python
logger.debug("WebGatherer: no orchestrator configured for query '%s' — skipping", query)
return []  # No results — web search not configured
```

The research pipeline already handles empty gatherer results. The stub was making it look like a real source existed.

### 1.2 Reasoning executor stub → error response

**File:** `reasoning/executor.py:111`

**Before:**
```python
logger.debug("ToolExecutor: no handler for '%s' — returning stub", name)
return json.dumps({"result": f"Tool '{name}' not available"})
```

**After:**
```python
logger.debug("ToolExecutor: no handler for '%s' — returning error", name)
return json.dumps({
    "error": True,
    "message": f"Tool '{name}' is not available. Try a different approach.",
    "available_tools": list(self._handlers.keys()),
})
```

The `error: True` field lets the reasoning loop detect failure programmatically. The `available_tools` list tells the LLM what it can actually use.

### 1.3 Autonomy cron stub → honest no-op

**File:** `autonomy/loop.py:206`

**Before:**
```python
# Phase 4: cron (stub — no scheduler yet)
```

**After:**
```python
logger.debug("Scheduled tasks skipped — scheduler not yet implemented")
```

No behavior change. Just honest logging instead of a misleading comment.

### 1.4 Memory write stub → 501 response

**File:** `api/routers/host.py:449`

**Before:**
```python
return MemoryWriteResponse(id="stub", accepted=False)
```

**After:**
```python
raise HTTPException(status_code=501, detail=_NOT_WIRED)
```

Consistent with every other unwired endpoint. A 200 response with `accepted: false` is misleading — the caller has to know to check that field. A 501 is unambiguous.

### 1.5 World model NotImplementedError → graceful skip

**File:** `world_model/store.py:52`

**Before:**
```python
raise NotImplementedError(f"Extraction format '{fmt}' not supported")
```

**After:**
```python
logger.warning("Extraction format '%s' not supported — skipping", fmt)
return []
```

Matches Colony's degradation pattern everywhere else.

### 1.6 Code synthesis NotImplementedError → graceful skip

**File:** `skills/learning/pattern_extractor.py:145`

**Before:**
```python
raise NotImplementedError("Code synthesis patterns not yet supported")
```

**After:**
```python
logger.debug("Pattern type '%s' not yet supported — skipping", pattern.type)
continue
```

When code synthesis ships, this `continue` gets replaced with the actual implementation.

---

## Part 2: Format Extraction

Add support for extracting structured entities from multiple document formats (PDF, HTML, JSON, CSV) in the world model extraction pipeline.

### Architecture

```
FormatDetector → FormatExtractor → LLM Entity Extractor → EntityDeduplicator → WorldModelStore
```

### New files

```
world_model/extraction/
├── __init__.py
├── base.py              # FormatExtractor ABC
├── detector.py          # MIME type + extension detection
├── formats/
│   ├── __init__.py
│   ├── text.py          # Plain text (existing)
│   ├── pdf.py           # PDF via PyMuPDF
│   ├── html.py          # HTML via BeautifulSoup
│   ├── json.py          # JSON structured data
│   └── csv.py           # CSV via pandas
└── pipeline.py          # Orchestrates format detection + extraction
```

### FormatExtractor base class

```python
class FormatExtractor(ABC):
    @abstractmethod
    def supported_formats(self) -> list[str]:
        """Return list of MIME types this extractor handles."""
        ...

    @abstractmethod
    async def extract_text(self, content: bytes, metadata: dict) -> str:
        """Extract raw text from content. Returns empty string if extraction fails."""
        ...

    @abstractmethod
    async def extract_entities(self, content: bytes, metadata: dict) -> list[Entity]:
        """Extract structured entities directly from content (no LLM)."""
        ...
```

### FormatDetector

```python
class FormatDetector:
    def detect(self, content: bytes, filename: str = "", mime_type: str = "") -> str:
        """Detect content format. Priority: explicit mime_type > filename extension > magic bytes."""
        if mime_type:
            return mime_type
        if filename:
            ext = Path(filename).suffix.lower()
            return EXTENSION_MAP.get(ext, "application/octet-stream")
        return magic.from_buffer(content[:1024], mime=True)
```

### ExtractionPipeline

```python
class ExtractionPipeline:
    def __init__(self, extractors: list[FormatExtractor], llm_extractor: LLMEntityExtractor):
        self._extractors = {fmt: ext for ext in extractors for fmt in ext.supported_formats()}
        self._llm = llm_extractor

    async def extract(self, content: bytes, metadata: dict) -> list[Entity]:
        fmt = FormatDetector().detect(content, metadata.get("filename", ""))
        extractor = self._extractors.get(fmt)

        if extractor is None:
            logger.warning("No extractor for format '%s' — skipping", fmt)
            return []

        # Try structured extraction first (fast, no LLM)
        entities = await extractor.extract_entities(content, metadata)

        # Fall back to text extraction + LLM
        if not entities:
            text = await extractor.extract_text(content, metadata)
            if text:
                entities = await self._llm.extract(text, metadata)

        return entities
```

### Dependencies

- `PyMuPDF` (fitz) — PDF text extraction
- `beautifulsoup4` — HTML parsing
- `pandas` — CSV parsing (already a dependency)

All optional: guarded behind try/import, skipped if not installed.

### Integration

- Wired in `server.py` alongside existing world model components
- Exposed via new endpoint: `POST /v1/host/world/extract`
- Format extraction also used internally by the research pipeline for document analysis

---

## Part 3: Code Synthesis

Auto-generate reusable skills from recurring LLM tool usage patterns.

### Architecture

```
ToolCallObserver → PatternBuffer → CodeSynthesizer → SkillSandbox → HumanReview → SkillRegistry
```

### New files

```
skills/learning/
├── observer.py          # Observes tool calls and records patterns
├── pattern_buffer.py    # Accumulates examples until threshold is met
├── synthesizer.py       # LLM-powered code synthesis from examples
├── sandbox.py           # Restricted execution environment for generated code
└── review.py            # Human review queue for auto-generated skills
```

### ToolCallObserver

Hooks into the reasoning loop to observe every tool call the LLM makes:

```python
class ToolCallObserver:
    def __init__(self, buffer: PatternBuffer):
        self._buffer = buffer

    async def observe(self, tool_name: str, args: dict, result: str, success: bool):
        """Record a tool call observation."""
        await self._buffer.record(
            tool_name=tool_name,
            args=args,
            result=result,
            success=success,
            timestamp=datetime.now(timezone.utc),
        )
```

### PatternBuffer

Accumulates tool call observations and identifies recurring patterns:

```python
class PatternBuffer:
    def __init__(self, db_path: str, threshold: int = 3):
        self._db = sqlite3.connect(db_path)
        self._threshold = threshold

    async def record(self, tool_name: str, args: dict, result: str, success: bool, timestamp: datetime):
        """Store an observation."""
        ...

    async def find_patterns(self) -> list[ObservedPattern]:
        """Find tool calls that have been made N+ times with similar structure."""
        # Group by tool_name + arg structure similarity
        # Return patterns that meet the threshold
        ...

    async def get_examples(self, pattern_id: str) -> list[dict]:
        """Get all examples for a pattern."""
        ...
```

### CodeSynthesizer

Uses the LLM to generalize observed examples into a reusable function:

```python
class CodeSynthesizer:
    def __init__(self, llm_client, sandbox: SkillSandbox):
        self._llm = llm_client
        self._sandbox = sandbox

    async def synthesize(self, pattern: ObservedPattern) -> SynthesizedSkill | None:
        """Generate a skill from a recurring pattern."""
        examples = await self._buffer.get_examples(pattern.id)
        if len(examples) < self._threshold:
            return None

        # Ask LLM to generalize
        prompt = SYNTHESIS_PROMPT.format(
            pattern_name=pattern.inferred_name,
            examples=self._format_examples(examples),
        )
        code = await self._llm.generate(prompt)

        # Test in sandbox
        test_result = await self._sandbox.test(code, pattern.test_inputs)
        if not test_result.passed:
            logger.warning("Synthesized code failed sandbox test: %s", test_result.error)
            return None

        return SynthesizedSkill(
            id=f"synth-{pattern.id}",
            name=pattern.inferred_name,
            description=pattern.inferred_description,
            code=code,
            parameters=test_result.parameters,
            test_results=test_result,
        )
```

### SkillSandbox

Restricted execution environment for generated code:

```python
class SkillSandbox:
    """Execute generated code in a restricted environment."""

    ALLOWED_MODULES = {"math", "json", "re", "datetime", "collections", "itertools", "string"}
    MAX_EXECUTION_TIME = 5  # seconds
    MAX_MEMORY_MB = 50

    async def test(self, code: str, test_inputs: list[dict]) -> SandboxResult:
        """Run code against test inputs. Returns pass/fail + extracted parameters."""
        try:
            # Parse the code AST first — reject dangerous operations
            tree = ast.parse(code)
            self._validate_ast(tree)

            # Execute in restricted namespace
            namespace = {"__builtins__": self._safe_builtins()}
            exec(compile(tree, "<synth>", "exec"), namespace)

            # Run test inputs
            func = namespace.get("run") or namespace.get("execute")
            if not func:
                return SandboxResult(passed=False, error="No 'run' or 'execute' function found")

            results = []
            for inp in test_inputs:
                result = func(**inp)
                results.append(result)

            return SandboxResult(
                passed=True,
                parameters=self._extract_parameters(func),
                results=results,
            )
        except Exception as e:
            return SandboxResult(passed=False, error=str(e))

    def _validate_ast(self, tree: ast.AST):
        """Reject dangerous AST nodes: imports, exec, eval, open, etc."""
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if node.module not in self.ALLOWED_MODULES:
                    raise ValueError(f"Import not allowed: {node.module}")
            if isinstance(node, (ast.Exec, ast.Eval)):
                raise ValueError("exec/eval not allowed")
            # ... more checks
```

### HumanReview

Auto-generated skills go into a review queue before going live:

```python
class ReviewQueue:
    """Pending skills that need human approval before activation."""

    async def submit(self, skill: SynthesizedSkill):
        """Add a skill to the review queue."""
        ...

    async def approve(self, skill_id: str) -> bool:
        """Approve a skill for registration."""
        ...

    async def reject(self, skill_id: str, reason: str):
        """Reject a synthesized skill."""
        ...

    async def list_pending(self) -> list[SynthesizedSkill]:
        """List skills awaiting review."""
        ...
```

### API Endpoints

```
GET  /v1/host/skills/synthesis/patterns      — List detected patterns
POST /v1/host/skills/synthesis/synthesize     — Trigger synthesis for a pattern
GET  /v1/host/skills/synthesis/review         — List pending review items
POST /v1/host/skills/synthesis/review/{id}/approve  — Approve a synthesized skill
POST /v1/host/skills/synthesis/review/{id}/reject   — Reject a synthesized skill
```

### Synthesis Prompt

```
You are a code synthesis engine. Given {N} examples of a recurring tool usage pattern,
write a single reusable Python function that generalizes the pattern.

Pattern: {pattern_name}
Examples:
{examples}

Requirements:
- Function must be named `run` or `execute`
- All parameters must have type hints
- Include a docstring
- No file I/O, network access, or subprocess calls
- Only use these modules: math, json, re, datetime, collections, itertools, string
- Return a dict with at least a "result" key
- Handle edge cases gracefully
```

### Integration

- ToolCallObserver hooks into `reasoning/executor.py` — observes every tool call
- PatternBuffer uses SQLite (alongside existing goals/contacts DBs)
- CodeSynthesizer runs during autonomy cycles (Phase 4 scheduler, or triggered manually)
- Approved skills register via the existing SkillRegistry
- Sandbox uses AST validation + restricted namespace (no subprocess isolation needed for Phase 1)

### Security Model

1. **AST validation** — generated code is parsed and validated before execution
2. **Restricted imports** — only whitelisted modules allowed
3. **No I/O** — no file, network, or subprocess access
4. **Time limit** — 5 second max execution time
5. **Memory limit** — 50MB max
6. **Human review** — no auto-generated skill goes live without approval
7. **No auto-registration** — synthesis only creates a review item, never activates directly

---

## Implementation Order

1. **Stub cleanup** (1 hour) — all 6 fixes in Part 1
2. **Format extraction** (1 day) — Part 2
3. **Code synthesis** (3-4 days) — Part 3

Parts 1 and 2 can ship together immediately. Part 3 is larger and can ship when ready.

---

## Dependencies to Add

```toml
[project.optional-dependencies]
extraction = [
    "PyMuPDF>=1.24",
    "beautifulsoup4>=4.12",
]
```

Both optional. Format extraction degrades gracefully if not installed.
