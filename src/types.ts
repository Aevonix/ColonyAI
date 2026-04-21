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
  | "log";

export interface HostEvent {
  type: HostEventType;
  occurred_at: string;
  payload: Record<string, unknown>;
  seq?: number;
}
