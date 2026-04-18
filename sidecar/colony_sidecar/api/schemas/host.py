"""Host API schemas — Pydantic models for the /v1/host surface.

Mirrors the TypeScript types in colony-core's src/types.ts.
The sidecar is the source of truth for these schemas.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

MAX_NAME_LEN = 256


# --- Identity ---------------------------------------------------------------

class HostIdentity(BaseModel):
    host_id: str
    host_version: Optional[str] = None
    plugin_version: Optional[str] = None
    instance_id: Optional[str] = None


class HostTurnContext(BaseModel):
    session_id: str
    contact_id: str
    channel_id: Optional[str] = None
    turn_id: Optional[str] = None
    locale: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# --- Messages ---------------------------------------------------------------

class HostMessage(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# --- Health -----------------------------------------------------------------

class HostHealthResponse(BaseModel):
    status: Literal["ok", "degraded", "starting", "stopping"]
    api_version: str = "1.0.0"
    capabilities: List[str] = []
    notes: Optional[Dict[str, str]] = None


# --- Memory -----------------------------------------------------------------

class MemoryEntry(BaseModel):
    id: str
    content: str
    type: Optional[str] = None
    strength: Optional[float] = None
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    created_at: Optional[str] = None
    score: Optional[float] = None


class MemoryReadRequest(BaseModel):
    identity: HostIdentity
    memory_id: Optional[str] = None
    person_id: Optional[str] = None
    limit: Optional[int] = None


class MemoryReadResponse(BaseModel):
    entries: List[MemoryEntry] = []


class MemoryWriteRequest(BaseModel):
    identity: HostIdentity
    context: Optional[HostTurnContext] = None
    content: str
    type: Optional[str] = None
    person_id: Optional[str] = None
    entities: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    strength: Optional[float] = None


class MemoryWriteResponse(BaseModel):
    id: str
    accepted: bool


class MemorySearchRequest(BaseModel):
    identity: HostIdentity
    query: str
    limit: Optional[int] = None
    min_score: Optional[float] = None
    person_id: Optional[str] = None
    types: Optional[List[str]] = None
    tags: Optional[List[str]] = None


class MemorySearchResponse(BaseModel):
    entries: List[MemoryEntry] = []


# --- Context ----------------------------------------------------------------

class ContextAssembleRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    incoming_message: HostMessage
    available_tools: Optional[List[str]] = None
    citations_mode: Optional[Literal["off", "inline", "appendix"]] = None


class ContextSection(BaseModel):
    id: str
    title: Optional[str] = None
    body: str
    priority: Optional[int] = None
    citations: Optional[List[Dict[str, Any]]] = None


class ContextAssembleResponse(BaseModel):
    sections: List[ContextSection] = []
    notices: Optional[List[str]] = None


# --- Reasoning --------------------------------------------------------------

class ReasoningTurnRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    messages: List[HostMessage]
    available_tools: List[str] = Field(default_factory=list)
    model_override: Optional[str] = Field(None, max_length=MAX_NAME_LEN)


class ReasoningToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ReasoningTurnResponse(BaseModel):
    status: Literal["completed", "needs_tool", "error"]
    message: Optional[HostMessage] = None
    tool_calls: List[ReasoningToolCall] = Field(default_factory=list)
    usage: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


# --- Signals ----------------------------------------------------------------

class SignalIngestRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    incoming_message: Optional[HostMessage] = None
    outgoing_message: Optional[HostMessage] = None
    tool_calls: List[ReasoningToolCall] = Field(default_factory=list)
    correction: Optional[str] = None


class SignalIngestResponse(BaseModel):
    accepted: bool
    signals_recorded: int


# --- Turns ------------------------------------------------------------------

class TurnSyncRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    topics: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    pending_tasks: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    summary: Optional[str] = None


class TurnSyncResponse(BaseModel):
    accepted: bool
    continuity_updated: bool
    skipped_reason: Optional[str] = None
    errors: Optional[List[str]] = None


# --- Safety -----------------------------------------------------------------

class SafetyCheckRequest(BaseModel):
    identity: HostIdentity
    context: HostTurnContext
    response_text: str
    incoming_message_text: Optional[str] = None
    target_gateway: Optional[str] = None
    trust_tier: Optional[str] = None
    mentioned_entities: Optional[List[str]] = None


class SafetyCheckResponse(BaseModel):
    decision: Literal["pass", "block", "pending"]
    blocked: bool
    blocking_layer: Optional[int] = None
    reason: Optional[str] = None
    flagged_excerpt: Optional[str] = None
    layer_results: Optional[Dict[str, Any]] = None


# --- Events -----------------------------------------------------------------

class HostEvent(BaseModel):
    type: str
    occurred_at: str
    payload: Dict[str, Any] = Field(default_factory=dict)
