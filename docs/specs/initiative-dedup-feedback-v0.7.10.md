# Initiative Deduplication & LLM Feedback Loop (v0.7.10)

## Problem

Initiatives are generated every autonomy tick (5 min in proactive mode) without deduplication. The same stale task creates duplicate initiatives, flooding the LLM with noise.

**Current flow:**
```
Autonomy Loop (every 5 min)
    ↓
InitiativeEngine generates from pending tasks
    ↓
Initiative enqueued → LLM sees it
    ↓
LLM has NO way to:
  - Mark task as done/stale
  - Snooze the task
  - Prevent future initiatives
    ↓
Same initiative repeats every 5 min forever
```

## Solution

Three-part fix:

1. **Deduplication at source** - Track last generation time per entity
2. **LLM tools for task management** - Let LLM act on initiatives
3. **Feedback loop** - Colony learns from LLM responses

---

## Part 1: Deduplication at Source

### 1.1 Initiative History Tracking

Add a lightweight tracking table for initiative generation:

```python
# In initiative_engine.py

class InitiativeEngine:
    def __init__(self, ...):
        # Track last generation time per entity
        # Format: {entity_id: {"last_generated_at": datetime, "initiative_type": str}}
        self._generation_history: Dict[str, Dict[str, Any]] = {}
        
    def _should_generate_for_entity(
        self, 
        entity_id: str, 
        initiative_type: str,
        cooldown_hours: float = 24.0
    ) -> bool:
        """Check if enough time has passed since last initiative for this entity."""
        if not entity_id:
            return True  # No entity ID = always generate
            
        key = f"{entity_id}:{initiative_type}"
        history = self._generation_history.get(key)
        
        if not history:
            return True
            
        last = history.get("last_generated_at")
        if not last:
            return True
            
        elapsed = datetime.now(timezone.utc) - last
        return elapsed >= timedelta(hours=cooldown_hours)
    
    def _record_generation(self, entity_id: str, initiative_type: str):
        """Record that an initiative was generated for this entity."""
        if not entity_id:
            return
        key = f"{entity_id}:{initiative_type}"
        self._generation_history[key] = {
            "last_generated_at": datetime.now(timezone.utc),
            "initiative_type": initiative_type,
        }
```

### 1.2 Apply Dedup in Generation

```python
async def generate(self, min_priority: float = 0.5) -> List[Initiative]:
    """Generate initiatives with deduplication."""
    self.clear_context()
    await self._feed_pending_tasks(engine)
    await self._feed_neglected_contacts(engine)
    await self._feed_commitment_reminders(engine)
    
    raw_initiatives = await self._raw_generate(min_priority)
    
    # Filter by cooldown
    deduped = []
    for init in raw_initiatives:
        entity_id = getattr(init, "entity_id", None)
        init_type = getattr(init, "type", "unknown")
        
        if self._should_generate_for_entity(entity_id, init_type):
            self._record_generation(entity_id, init_type)
            deduped.append(init)
    
    return deduped
```

### 1.3 Configurable Cooldown

```bash
# In ~/.colony/.env
COLONY_INITIATIVE_COOLDOWN_HOURS=24  # Don't repeat same entity within 24h
COLONY_INITIATIVE_COOLDOWN_TASKS=12  # Specific to tasks
COLONY_INITIATIVE_COOLDOWN_CONTACTS=72  # Contacts need longer cooldown
```

---

## Part 2: LLM Tools for Task Management

### 2.1 New Colony Tools

Add tools the LLM can call when it sees an initiative:

```python
# In tools/definitions.py

@tool
def colony_task_complete(task_id: str) -> dict:
    """Mark a task as completed.
    
    Use when the LLM determines a task mentioned in an initiative is done.
    
    Args:
        task_id: The task identifier from the initiative context
        
    Returns:
        {"success": bool, "message": str}
    """
    # Implementation in goals/store.py
    pass

@tool  
def colony_task_snooze(task_id: str, hours: int, reason: str = "") -> dict:
    """Snooze a task - don't generate initiatives for it for N hours.
    
    Use when a task is valid but not actionable right now.
    
    Args:
        task_id: The task identifier
        hours: Hours to snooze (1-168, max 1 week)
        reason: Optional reason for snooze
        
    Returns:
        {"success": bool, "snoozed_until": datetime}
    """
    pass

@tool
def colony_task_dismiss(task_id: str, reason: str = "stale") -> dict:
    """Dismiss/delete a task as no longer relevant.
    
    Use when the LLM determines a task is stale, abandoned, or no longer needed.
    
    Args:
        task_id: The task identifier
        reason: "stale" | "completed" | "abandoned" | "not_applicable"
        
    Returns:
        {"success": bool, "message": str}
    """
    pass

@tool
def colony_initiative_feedback(initiative_id: str, action: str, details: dict = None) -> dict:
    """Provide feedback on an initiative.
    
    Use to tell Colony how the LLM handled an initiative.
    
    Args:
        initiative_id: The initiative ID from the system message
        action: "acknowledged" | "actioned" | "dismissed" | "snoozed"
        details: Optional additional context
        
    Returns:
        {"success": bool}
    """
    pass
```

### 2.2 Task Model Updates

```python
# In goals/store.py

@dataclass
class Task:
    id: str
    description: str
    status: str = "pending"  # pending, completed, dismissed, snoozed
    priority: float = 0.5
    created_at: datetime = None
    updated_at: datetime = None
    
    # New fields for initiative management
    last_initiative_at: Optional[datetime] = None
    snoozed_until: Optional[datetime] = None
    snooze_count: int = 0
    dismissal_reason: Optional[str] = None
    completed_at: Optional[datetime] = None
```

### 2.3 Store Methods

```python
class GoalStore:
    # ... existing methods ...
    
    def complete_task(self, task_id: str) -> bool:
        """Mark task as completed."""
        task = self._tasks.get(task_id)
        if task:
            task.status = "completed"
            task.completed_at = datetime.now(timezone.utc)
            task.updated_at = datetime.now(timezone.utc)
            return True
        return False
    
    def snooze_task(self, task_id: str, hours: int, reason: str = "") -> bool:
        """Snooze a task for N hours."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        
        # Cap at 1 week
        hours = min(hours, 168)
        
        task.snoozed_until = datetime.now(timezone.utc) + timedelta(hours=hours)
        task.snooze_count += 1
        task.updated_at = datetime.now(timezone.utc)
        return True
    
    def dismiss_task(self, task_id: str, reason: str = "stale") -> bool:
        """Dismiss a task as no longer relevant."""
        task = self._tasks.get(task_id)
        if task:
            task.status = "dismissed"
            task.dismissal_reason = reason
            task.updated_at = datetime.now(timezone.utc)
            return True
        return False
    
    def get_active_tasks(self) -> List[Task]:
        """Get tasks that should generate initiatives."""
        now = datetime.now(timezone.utc)
        active = []
        
        for task in self._tasks.values():
            # Skip non-pending
            if task.status != "pending":
                continue
            
            # Skip snoozed
            if task.snoozed_until and task.snoozed_until > now:
                continue
            
            active.append(task)
        
        return active
```

---

## Part 3: Feedback Loop

### 3.1 Initiative Response Tracking

When LLM calls tools, Colony records the response:

```python
# In initiatives/store.py

class InitiativeStore:
    def record_response(
        self,
        initiative_id: str,
        action: str,  # acknowledged, actioned, dismissed, snoozed
        details: Optional[dict] = None
    ):
        """Record how an initiative was handled."""
        self.log_history(
            initiative_id,
            action=f"llm_{action}",
            agent_id="openclaw",
            details=details or {}
        )
```

### 3.2 Initiative Context Enhancement

Include task_id in initiative context so LLM knows what to act on:

```python
# In autonomy/loop.py - _phase_execute

payload = {
    "id": getattr(initiative, "id", str(uuid.uuid4())),
    "type": type_value,
    "priority": getattr(initiative, "priority", 0.5),
    "title": getattr(initiative, "description", "").split(".")[0][:80],
    "description": getattr(initiative, "description", ""),
    "rationale": getattr(initiative, "rationale", ""),
    "suggested_action": getattr(initiative, "action_hint", "notify_user"),
    "entity_id": getattr(initiative, "entity_id", None),  # <-- NEW
    "entity_type": "task",  # <-- NEW: task, contact, commitment
    "context": {
        "task_id": getattr(initiative, "entity_id", None),  # <-- NEW
        "pending_tasks": ctx.pending_tasks,
        "neglected_contacts": ctx.neglected_contacts,
    },
    "generated_at": datetime.now(timezone.utc).isoformat(),
}
```

### 3.3 LLM Prompt Enhancement

Plugin includes tool hints when delivering initiative:

```typescript
// In plugin.ts - formatInitiativeText

function formatInitiativeText(init: Record<string, unknown>): string {
  const lines = [
    `[colony_initiative]`,
    `ID: ${init.id ?? "unknown"}`,
    `Type: ${init.type ?? "unknown"}`,
    `Priority: ${init.priority ?? 0}`,
    `Title: ${init.title ?? "(no title)"}`,
    `Description: ${init.description ?? "(no description)"}`,
    `Rationale: ${init.rationale ?? "(no rationale)"}`,
    `Suggested action: ${init.suggested_action ?? "notify_user"}`,
  ];
  
  // NEW: Add action hint for LLM
  const taskId = init.entity_id ?? init.context?.task_id;
  if (taskId && init.entity_type === "task") {
    lines.push(``);
    lines.push(`This is a pending task. You can:`);
    lines.push(`- Mark complete: colony_task_complete("${taskId}")`);
    lines.push(`- Snooze: colony_task_snooze("${taskId}", hours, reason)`);
    lines.push(`- Dismiss: colony_task_dismiss("${taskId}", reason)`);
  }
  
  // ... rest of existing context formatting ...
  
  return lines.join("\n");
}
```

---

## Part 4: API Endpoints

### 4.1 Task Management API

```python
# In api/routers/host.py

@router.post("/v1/host/tasks/{task_id}/complete")
async def complete_task(task_id: str, api_key: str = Depends(verify_api_key)):
    """Mark a task as completed."""
    goals = get_goals_store()
    success = goals.complete_task(task_id)
    return {"success": success, "task_id": task_id}

@router.post("/v1/host/tasks/{task_id}/snooze")
async def snooze_task(
    task_id: str, 
    hours: int = Body(..., ge=1, le=168),
    reason: str = Body(""),
    api_key: str = Depends(verify_api_key)
):
    """Snooze a task for N hours."""
    goals = get_goals_store()
    success = goals.snooze_task(task_id, hours, reason)
    return {
        "success": success, 
        "task_id": task_id,
        "snoozed_until": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    }

@router.post("/v1/host/tasks/{task_id}/dismiss")
async def dismiss_task(
    task_id: str,
    reason: str = Body("stale"),
    api_key: str = Depends(verify_api_key)
):
    """Dismiss a task as no longer relevant."""
    goals = get_goals_store()
    success = goals.dismiss_task(task_id, reason)
    return {"success": success, "task_id": task_id, "reason": reason}

@router.post("/v1/host/initiatives/{initiative_id}/respond")
async def respond_to_initiative(
    initiative_id: str,
    action: str = Body(...),  # acknowledged, actioned, dismissed, snoozed
    details: Optional[dict] = Body(None),
    api_key: str = Depends(verify_api_key)
):
    """Record LLM response to an initiative."""
    store = get_initiative_store()
    store.record_response(initiative_id, action, details)
    return {"success": True, "initiative_id": initiative_id}
```

---

## Part 5: Configuration

### 5.1 Environment Variables

```bash
# Initiative deduplication
COLONY_INITIATIVE_COOLDOWN_HOURS=24      # Default cooldown
COLONY_INITIATIVE_COOLDOWN_TASKS=12      # Task-specific cooldown
COLONY_INITIATIVE_COOLDOWN_CONTACTS=72   # Contact-specific cooldown
COLONY_INITIATIVE_MAX_SNOOZE_HOURS=168   # Max snooze duration (1 week)

# Feedback
COLONY_INITIATIVE_FEEDBACK_ENABLED=true   # Enable LLM feedback loop
```

### 5.2 Config Schema

```python
# In autonomy/config.py

@dataclass
class InitiativeConfig:
    cooldown_hours: float = 24.0
    cooldown_tasks: float = 12.0
    cooldown_contacts: float = 72.0
    max_snooze_hours: int = 168
    feedback_enabled: bool = True
```

---

## Part 6: Testing Checklist

### Unit Tests

- [ ] `_should_generate_for_entity()` respects cooldown
- [ ] `_record_generation()` updates history
- [ ] `complete_task()` marks task as completed
- [ ] `snooze_task()` sets snoozed_until
- [ ] `dismiss_task()` marks task as dismissed
- [ ] `get_active_tasks()` filters out non-pending/snoozed

### Integration Tests

- [ ] Initiative generated only once per cooldown period
- [ ] LLM can complete task via tool
- [ ] LLM can snooze task via tool
- [ ] LLM can dismiss task via tool
- [ ] Feedback recorded in initiative history
- [ ] Snoozed tasks don't generate initiatives

### End-to-End Test

1. Create a pending task
2. Wait for first initiative
3. LLM calls `colony_task_snooze(task_id, 24)`
4. Verify no initiatives for next 24h
5. After 24h, verify new initiative generated
6. LLM calls `colony_task_complete(task_id)`
7. Verify no more initiatives ever

---

## Files Changed

| File | Change |
|------|--------|
| `sidecar/colony_sidecar/intelligence/components/initiative_engine.py` | Add dedup logic |
| `sidecar/colony_sidecar/goals/store.py` | Add task management methods |
| `sidecar/colony_sidecar/goals/schema.py` | Add snoozed_until, etc fields |
| `sidecar/colony_sidecar/tools/definitions.py` | Add task tools |
| `sidecar/colony_sidecar/api/routers/host.py` | Add task/initiative endpoints |
| `sidecar/colony_sidecar/autonomy/config.py` | Add InitiativeConfig |
| `sidecar/colony_sidecar/autonomy/loop.py` | Include entity_id in payload |
| `src/plugin.ts` | Add action hints to initiative text |

---

## Version

- **Target:** v0.7.10
- **Dependencies:** v0.7.9 (initiative delivery fix)

---

## Migration

Existing tasks will get default values:
- `status: "pending"` (existing)
- `last_initiative_at: null` (new - will generate on next tick)
- `snoozed_until: null` (new)
- `snooze_count: 0` (new)

No data migration needed - new fields are optional with defaults.

---

## Future Enhancements

1. **Smart cooldown** - Adjust cooldown based on priority (high priority = shorter cooldown)
2. **Snooze fatigue** - After N snoozes, suggest dismissal
3. **Initiative batching** - Group multiple initiatives into digest
4. **User preferences** - Let user set preferred notification frequency
5. **Learning from feedback** - Adjust generation patterns based on LLM responses
