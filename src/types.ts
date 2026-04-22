/**
 * TypeScript types for the Colony sidecar Host API.
 *
 * AUTO-GENERATED from the Python sidecar's OpenAPI spec.
 * Do NOT edit manually — run `npm run generate-types` to regenerate.
 *
 * The Python Pydantic schemas in `sidecar/colony_sidecar/api/schemas/host.py`
 * are the single source of truth.
 */

import type { components } from "./types-generated.js";

// --- Identity ---------------------------------------------------------------

export type HostIdentity = components["schemas"]["HostIdentity"];
export type HostTurnContext = components["schemas"]["HostTurnContext"];

// --- Messages ---------------------------------------------------------------

export type HostMessageRole = components["schemas"]["HostMessage"]["role"];
export type HostMessage = components["schemas"]["HostMessage"];

// --- Health -----------------------------------------------------------------

export type HostHealthResponse = components["schemas"]["HostHealthResponse"];

// --- Memory -----------------------------------------------------------------

export type MemoryEntry = components["schemas"]["MemoryEntry"];
export type MemoryReadRequest = components["schemas"]["MemoryReadRequest"];
export type MemoryReadResponse = components["schemas"]["MemoryReadResponse"];
export type MemoryWriteRequest = components["schemas"]["MemoryWriteRequest"];
export type MemoryWriteResponse = components["schemas"]["MemoryWriteResponse"];
export type MemorySearchRequest = components["schemas"]["MemorySearchRequest"];
export type MemorySearchResponse = components["schemas"]["MemorySearchResponse"];

export type MemoryFlushRequest = components["schemas"]["MemoryFlushRequest"];
export type MemoryFlushResponse = components["schemas"]["MemoryFlushResponse"];
export type MemoryEmbedRequest = components["schemas"]["MemoryEmbedRequest"];
export type MemoryEmbedResponse = components["schemas"]["MemoryEmbedResponse"];

// --- Context ----------------------------------------------------------------

export type ContextAssembleRequest = components["schemas"]["ContextAssembleRequest"];
export type ContextSection = components["schemas"]["ContextSection"];
export type ContextAssembleResponse = components["schemas"]["ContextAssembleResponse"];

// --- Reasoning --------------------------------------------------------------

export type ReasoningTurnRequest = components["schemas"]["ReasoningTurnRequest"];
export type ReasoningToolCall = components["schemas"]["ReasoningToolCall"];
export type ReasoningTurnResponse = components["schemas"]["ReasoningTurnResponse"];

// --- Signals ----------------------------------------------------------------

export type SignalIngestRequest = components["schemas"]["SignalIngestRequest"];
export type SignalIngestResponse = components["schemas"]["SignalIngestResponse"];

// --- Turns ------------------------------------------------------------------

export type TurnSyncRequest = components["schemas"]["TurnSyncRequest"];
export type TurnSyncResponse = components["schemas"]["TurnSyncResponse"];

// --- Safety -----------------------------------------------------------------

export type SafetyCheckRequest = components["schemas"]["SafetyCheckRequest"];
export type SafetyCheckResponse = components["schemas"]["SafetyCheckResponse"];

// --- Events -----------------------------------------------------------------

export type HostEventType =
  | "proactive_message"
  | "briefing"
  | "anomaly"
  | "goal_update"
  | "memory_consolidated"
  | "world_model_changed"
  | "skill_draft_approved"
  | "turn_synced"
  | "replay_complete"
  | "commitment.created"
  | "commitment.fulfilled"
  | "commitment.overdue"
  | "commitment.cancelled"
  | "cognition.requested"
  | "affect.event_created"
  | "affect.negative_spike"
  | "affect.sustained_decline"
  | "mind.fact_created"
  | "log";

export interface HostEvent {
  type: HostEventType;
  occurred_at: string;
  payload: Record<string, unknown>;
  seq?: number;
}

export interface CommitmentResponse {
  id: string;
  person_id: string;
  description: string;
  made_at: string;
  due_at?: string;
  fulfilled_at?: string;
  status: string;
  source_type: string;
  source_context?: string;
  priority: number;
  metadata?: Record<string, unknown>;
}

export interface CommitmentListResponse {
  commitments: CommitmentResponse[];
  total: number;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// Theory of Mind — Affect
// ---------------------------------------------------------------------------

export interface AffectEventResponse {
  id: string;
  contact_id: string;
  valence: number;
  arousal: number;
  source: string;
  trigger: string | null;
  timestamp: string;
  session_id: string | null;
}

export interface AffectStateResponse {
  contact_id: string;
  current_valence: number;
  current_arousal: number;
  trend: string;
  last_event_id: string | null;
  last_updated: string | null;
  event_count: number;
}

export interface AffectEventListResponse {
  events: AffectEventResponse[];
  total: number;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// Theory of Mind — Shared Facts
// ---------------------------------------------------------------------------

export interface SharedFactResponse {
  id: string;
  contact_id: string;
  fact: string;
  source: string;
  confidence: number;
  created_at: string;
  expires_at: string | null;
  metadata: Record<string, unknown> | null;
}

export interface SharedFactListResponse {
  facts: SharedFactResponse[];
  total: number;
  limit: number;
  offset: number;
}

// Pattern Extraction
export interface PatternResponse {
  id: string;
  pattern_type: string;
  description: string;
  pattern_key: string;
  frequency: number;
  last_seen: string;
  first_seen: string;
  confidence: number;
  metadata?: Record<string, unknown>;
  source: string;
  active: boolean;
}

export interface PatternListResponse {
  patterns: PatternResponse[];
  total: number;
  limit: number;
  offset: number;
}

export interface PatternExtractResponse {
  new: number;
  updated: number;
  total: number;
  reason?: string;
}

// Surprise Engine
export interface SurpriseResponse {
  id: string;
  observation: string;
  expected?: string;
  surprise_score: number;
  pattern_id?: string;
  context?: Record<string, unknown>;
  timestamp: string;
  resolved: boolean;
  resolution?: string;
}

export interface SurpriseListResponse {
  surprises: SurpriseResponse[];
  total: number;
  limit: number;
  offset: number;
}
