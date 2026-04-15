/**
 * TypeScript mirror of the schemas defined in
 * `colony/api/schemas/host.py`. Keep these in lockstep — the colony-core
 * server is the source of truth.
 */

export interface HostIdentity {
  host_id: string;
  host_version?: string;
  plugin_version?: string;
  instance_id?: string;
}

export interface HostTurnContext {
  session_id: string;
  contact_id: string;
  channel_id?: string;
  turn_id?: string;
  locale?: string;
  metadata?: Record<string, unknown>;
}

export type HostMessageRole = "user" | "assistant" | "system" | "tool";

export interface HostMessage {
  role: HostMessageRole;
  content: string;
  name?: string;
  tool_call_id?: string;
  metadata?: Record<string, unknown>;
}

// --- Health -----------------------------------------------------------------

export interface HostHealthResponse {
  status: "ok" | "degraded" | "starting" | "stopping";
  api_version: string;
  capabilities: string[];
  notes?: Record<string, string>;
}

// --- Memory ----------------------------------------------------------------

export interface MemoryEntry {
  id: string;
  content: string;
  type?: string;
  strength?: number;
  person_id?: string | null;
  entities?: string[];
  tags?: string[];
  created_at?: string | null;
  score?: number | null;
}

export interface MemoryReadRequest {
  identity: HostIdentity;
  memory_id?: string;
  person_id?: string;
  limit?: number;
}

export interface MemoryReadResponse {
  entries: MemoryEntry[];
}

export interface MemoryWriteRequest {
  identity: HostIdentity;
  context?: HostTurnContext;
  content: string;
  type?: string;
  person_id?: string;
  entities?: string[];
  tags?: string[];
  strength?: number;
}

export interface MemoryWriteResponse {
  id: string;
  accepted: boolean;
}

export interface MemorySearchRequest {
  identity: HostIdentity;
  query: string;
  limit?: number;
  min_score?: number;
  person_id?: string;
  types?: string[];
  tags?: string[];
}

export interface MemorySearchResponse {
  entries: MemoryEntry[];
}

export interface MemoryFlushRequest {
  identity: HostIdentity;
  reason?: string;
}

export interface MemoryFlushResponse {
  accepted: boolean;
  job_id?: string | null;
}

export interface MemoryEmbedRequest {
  identity: HostIdentity;
  inputs: string[];
  model?: string;
}

export interface MemoryEmbedResponse {
  model: string;
  vectors: number[][];
}

// --- Context ---------------------------------------------------------------

export interface ContextAssembleRequest {
  identity: HostIdentity;
  context: HostTurnContext;
  incoming_message: HostMessage;
  available_tools?: string[];
  citations_mode?: "off" | "inline" | "appendix";
}

export interface ContextSection {
  id: string;
  title?: string;
  body: string;
  priority?: number;
  citations?: Array<Record<string, unknown>>;
}

export interface ContextAssembleResponse {
  sections: ContextSection[];
  notices?: string[];
}

// --- Reasoning -------------------------------------------------------------

export interface ReasoningTurnRequest {
  identity: HostIdentity;
  context: HostTurnContext;
  messages: HostMessage[];
  available_tools?: string[];
  model_override?: string;
}

export interface ReasoningToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ReasoningTurnResponse {
  status: "completed" | "needs_tool" | "error";
  message?: HostMessage;
  tool_calls: ReasoningToolCall[];
  usage?: Record<string, unknown>;
  error?: string;
}

// --- Signals ---------------------------------------------------------------

export interface SignalIngestRequest {
  identity: HostIdentity;
  context: HostTurnContext;
  incoming_message?: HostMessage;
  outgoing_message?: HostMessage;
  tool_calls?: ReasoningToolCall[];
  correction?: string;
}

export interface SignalIngestResponse {
  accepted: boolean;
  signals_recorded: number;
}

// --- Turns (post-turn cognition sync) -------------------------------------

export interface TurnSyncRequest {
  identity: HostIdentity;
  context: HostTurnContext;
  topics?: string[];
  entities?: string[];
  pending_tasks?: string[];
  tools_used?: string[];
  summary?: string;
}

export interface TurnSyncResponse {
  accepted: boolean;
  continuity_updated: boolean;
  skipped_reason?: string | null;
  errors?: string[];
}

// --- Safety ----------------------------------------------------------------

export interface SafetyCheckRequest {
  identity: HostIdentity;
  context: HostTurnContext;
  response_text: string;
  incoming_message_text?: string;
  target_gateway?: string;
  trust_tier?: string;
  mentioned_entities?: string[];
}

export interface SafetyCheckResponse {
  decision: "pass" | "block" | "pending";
  blocked: boolean;
  blocking_layer?: number | null;
  reason?: string | null;
  flagged_excerpt?: string | null;
  layer_results?: Record<string, unknown>;
}

// --- Events ----------------------------------------------------------------

export type HostEventType =
  | "proactive_message"
  | "briefing"
  | "anomaly"
  | "goal_update"
  | "memory_consolidated"
  | "turn_synced"
  | "log";

export interface HostEvent {
  type: HostEventType;
  occurred_at: string;
  payload: Record<string, unknown>;
}
