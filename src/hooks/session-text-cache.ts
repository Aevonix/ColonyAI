/**
 * LRU session text cache bridging `message_received` → `message_sending`
 * and `llm_output` → `reply_dispatch`.
 *
 * Stores the most recent inbound and assistant text per session so that
 * the safety hook and post-turn hook can read the full turn context
 * without needing OpenClaw to carry it through its hook surface.
 *
 * The cache is bounded to ~256 sessions and evicts the least-recently-
 * written entry when full. Sessions that go idle naturally age out as
 * newer sessions push them past the limit.
 */

const DEFAULT_MAX_SIZE = 256;

interface CachedSession {
  /** Timestamp of the last write to this entry (used for LRU eviction). */
  lastTouched: number;
  /** Inbound message text, populated by the `message_received` hook. */
  inboundText: string;
  /** Assistant response texts, populated by the `llm_output` hook. */
  assistantTexts: string[];
}

export class SessionTextCache {
  private readonly entries = new Map<string, CachedSession>();
  private readonly maxSize: number;

  constructor(maxSize = DEFAULT_MAX_SIZE) {
    this.maxSize = maxSize;
  }

  /** Store inbound text for a session (from `message_received`). */
  setInbound(sessionKey: string, text: string): void {
    const existing = this.entries.get(sessionKey);
    if (existing) {
      existing.inboundText = text;
      existing.lastTouched = Date.now();
      return;
    }
    this.evictIfNeeded();
    this.entries.set(sessionKey, {
      lastTouched: Date.now(),
      inboundText: text,
      assistantTexts: [],
    });
  }

  /** Store assistant text for a session (from `llm_output`). */
  setAssistant(sessionKey: string, texts: string[]): void {
    const existing = this.entries.get(sessionKey);
    if (existing) {
      existing.assistantTexts = texts;
      existing.lastTouched = Date.now();
      return;
    }
    this.evictIfNeeded();
    this.entries.set(sessionKey, {
      lastTouched: Date.now(),
      inboundText: "",
      assistantTexts: texts,
    });
  }

  /** Read cached inbound text for a session. Returns "" if not cached. */
  getInbound(sessionKey: string): string {
    return this.entries.get(sessionKey)?.inboundText ?? "";
  }

  /** Read cached assistant texts for a session. Returns [] if not cached. */
  getAssistantTexts(sessionKey: string): string[] {
    return this.entries.get(sessionKey)?.assistantTexts ?? [];
  }

  /** Combined assistant text joined by newlines. Returns "" if not cached. */
  getCombinedAssistant(sessionKey: string): string {
    const texts = this.getAssistantTexts(sessionKey);
    return texts.filter(Boolean).join("\n");
  }

  /** Remove a session from the cache. */
  delete(sessionKey: string): void {
    this.entries.delete(sessionKey);
  }

  /** Current cache size (for diagnostics). */
  get size(): number {
    return this.entries.size;
  }

  private evictIfNeeded(): void {
    if (this.entries.size < this.maxSize) return;
    // Find and evict the least-recently-touched entry.
    let oldestKey: string | null = null;
    let oldestTime = Infinity;
    for (const [key, val] of this.entries) {
      if (val.lastTouched < oldestTime) {
        oldestTime = val.lastTouched;
        oldestKey = key;
      }
    }
    if (oldestKey !== null) {
      this.entries.delete(oldestKey);
    }
  }
}
