/**
 * WebSocket event dispatcher for colony host events.
 *
 * The sidecar broadcasts typed events via /v1/host/events; the plugin's
 * lifecycle service (see plugin.ts) subscribes and forwards each event
 * here. Each handler is side-effect only — it invalidates relevant
 * caches and/or logs user-visible nudges so the next turn sees the
 * fresh state.
 *
 * Handlers never throw. Errors are logged and swallowed so a misbehaved
 * payload can't break the event loop.
 */

import type { ContextCache } from "./context-cache.js";
import type { HostEvent } from "./types.js";

type Logger = {
  info?(m: string): void;
  warn?(m: string): void;
  error?(m: string): void;
};

export interface EventDispatchCtx {
  cache: ContextCache;
  logger?: Logger;
  /** Invoked when a new skill_draft_approved event arrives; used by
   *  Phase 3's tool registrar. Provided as a callback so this module
   *  stays decoupled from the tool registration surface. */
  onSkillApproved?: (skillId: string) => Promise<void> | void;
  /** Invoked when a briefing is published; Phase 2 just logs, but
   *  Phase 3+ may surface it as a proactive message. */
  onBriefing?: (briefingId: string, payload: Record<string, unknown>) => void;
}

export function handleMemoryConsolidated(
  event: HostEvent,
  { cache, logger }: EventDispatchCtx,
): void {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  cache.invalidate("memory", p);
  const merged = typeof p.merged === "number" ? p.merged : "?";
  logger?.info?.(`[colony.event] memory_consolidated merged=${merged}`);
}

export function handleGoalUpdate(
  event: HostEvent,
  { cache, logger }: EventDispatchCtx,
): void {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  cache.invalidate("goals", p);
  const id = typeof p.goal_id === "string" ? p.goal_id : "?";
  const status = typeof p.status === "string" ? p.status : "?";
  logger?.info?.(`[colony.event] goal_update id=${id} status=${status}`);
}

export function handleAnomaly(
  event: HostEvent,
  { logger }: EventDispatchCtx,
): void {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  const severity = typeof p.severity === "number" ? p.severity : 0;
  const msg = typeof p.description === "string" ? p.description : "(no detail)";
  if (severity >= 0.7) {
    logger?.warn?.(`[colony.event] anomaly severity=${severity} ${msg}`);
  } else {
    logger?.info?.(`[colony.event] anomaly severity=${severity} ${msg}`);
  }
}

export function handleBriefing(
  event: HostEvent,
  { cache, logger, onBriefing }: EventDispatchCtx,
): void {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  cache.invalidate("briefings", p);
  const id = typeof p.briefing_id === "string" ? p.briefing_id : "?";
  logger?.info?.(`[colony.event] briefing id=${id}`);
  if (onBriefing && typeof p.briefing_id === "string") {
    try {
      onBriefing(p.briefing_id, p);
    } catch (err) {
      logger?.warn?.(`[colony.event] onBriefing hook failed: ${String(err)}`);
    }
  }
}

export function handleWorldModelChanged(
  event: HostEvent,
  { cache, logger }: EventDispatchCtx,
): void {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  cache.invalidate("world_model", p);
  const kind = typeof p.change_type === "string" ? p.change_type : "?";
  logger?.info?.(`[colony.event] world_model_changed ${kind}`);
}

export function handleSkillDraftApproved(
  event: HostEvent,
  { cache, logger, onSkillApproved }: EventDispatchCtx,
): void {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  cache.invalidate("skills", p);
  const id = typeof p.skill_id === "string" ? p.skill_id : "";
  const name = typeof p.name === "string" ? p.name : "(unnamed)";
  logger?.info?.(`[colony.event] skill_draft_approved id=${id} name=${name}`);
  if (id && onSkillApproved) {
    Promise.resolve(onSkillApproved(id)).catch((err: unknown) => {
      logger?.warn?.(
        `[colony.event] onSkillApproved hook failed for ${id}: ${String(err)}`,
      );
    });
  }
}

/**
 * Dispatch a host event to the correct handler. Returns ``true`` if a
 * handler matched, ``false`` when the event type has no dedicated
 * handler (lifecycle service can still log it).
 */
export function dispatchHostEvent(
  event: HostEvent,
  ctx: EventDispatchCtx,
): boolean {
  try {
    switch (event.type) {
      case "memory_consolidated":
        handleMemoryConsolidated(event, ctx);
        return true;
      case "goal_update":
        handleGoalUpdate(event, ctx);
        return true;
      case "anomaly":
        handleAnomaly(event, ctx);
        return true;
      case "briefing":
        handleBriefing(event, ctx);
        return true;
      case "world_model_changed":
        handleWorldModelChanged(event, ctx);
        return true;
      case "skill_draft_approved":
        handleSkillDraftApproved(event, ctx);
        return true;
      case "commitment.created":
      case "commitment.fulfilled":
      case "commitment.overdue":
      case "commitment.cancelled":
        ctx.cache?.invalidate("commitments", event.payload);
        ctx.logger?.info?.(`[colony.event] ${event.type}`);
        return true;
      case "affect.event_created":
      case "affect.negative_spike":
      case "affect.sustained_decline":
        ctx.cache?.invalidate("affect", event.payload);
        ctx.logger?.info?.(`[colony.event] ${event.type}`);
        return true;
      case "mind.fact_created":
        ctx.cache?.invalidate("facts", event.payload);
        ctx.logger?.info?.(`[colony.event] ${event.type}`);
        return true;
      default:
        return false;
    }
  } catch (err) {
    ctx.logger?.warn?.(
      `[colony.event] dispatcher error on ${event.type}: ${String(err)}`,
    );
    return false;
  }
}
