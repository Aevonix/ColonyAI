/**
 * Colony plugin context-cache — tiny pub/sub for cache invalidation.
 *
 * The sidecar's enriched-context endpoint is authoritative: it
 * recomputes sections on every call from Colony's stores (graph,
 * contacts, goals, cognition, etc.). The plugin doesn't cache those
 * payloads. But it does cache a few derived pieces (skill tool
 * registrations, identity snapshot, capability probe) that must be
 * refreshed when the underlying state changes.
 *
 * This module exposes a small channel-based invalidator so the
 * WebSocket event dispatcher can say "memory changed — drop caches
 * keyed on it" without taking a hard dependency on any consumer.
 */

export type CacheChannel =
  | "memory"
  | "goals"
  | "contacts"
  | "world_model"
  | "skills"
  | "briefings"
  | "identity"
  | "cognition"
  | "commitments"
  | "affect"
  | "facts"
  | "patterns"
  | "surprises";

type Listener = (channel: CacheChannel, payload?: unknown) => void;

export interface ContextCache {
  invalidate(channel: CacheChannel, payload?: unknown): void;
  subscribe(listener: Listener): () => void;
}

export function createContextCache(): ContextCache {
  const listeners = new Set<Listener>();
  return {
    invalidate(channel, payload) {
      for (const listener of listeners) {
        try {
          listener(channel, payload);
        } catch {
          /* listener failures are non-fatal */
        }
      }
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
