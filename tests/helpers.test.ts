import { describe, expect, it, vi } from "vitest";

import { ColonyApiError } from "../src/sidecar-client.js";
import {
  ColonyEmbedUnavailableError,
  summarizeHostEvent,
  withDegradation,
} from "../src/plugin.js";
import type { HostEvent } from "../src/types.js";

/**
 * Exercises the SDK-agnostic helpers the plugin exports. Adapter
 * *shapes* are still scaffolded and need to be rewritten against the
 * real OpenClaw SDK contracts — see the tracking issue — so we
 * deliberately avoid testing the adapter functions here; those tests
 * belong with the rewrite.
 */

describe("withDegradation", () => {
  it("returns the call result on success", async () => {
    const fallback = vi.fn();
    const result = await withDegradation(
      { name: "x.op" },
      async () => "ok",
      () => fallback() as unknown as string,
    );
    expect(result).toBe("ok");
    expect(fallback).not.toHaveBeenCalled();
  });

  it("returns fallback and warns on phase1_wiring_required", async () => {
    const warn = vi.fn();
    const result = await withDegradation(
      { name: "x.op", logger: { warn } },
      async () => {
        throw new ColonyApiError(501, "phase1_wiring_required", "not wired");
      },
      () => "fallback",
    );
    expect(result).toBe("fallback");
    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0][0]).toMatch(/x\.op/);
    expect(warn.mock.calls[0][0]).toMatch(/phase1_wiring_required/);
  });

  it("returns fallback on 5xx sidecar errors", async () => {
    const warn = vi.fn();
    const result = await withDegradation(
      { name: "x.op", logger: { warn } },
      async () => {
        throw new ColonyApiError(503, "unavailable", "pool exhausted");
      },
      () => "fallback",
    );
    expect(result).toBe("fallback");
    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0][0]).toMatch(/503/);
  });

  it("rethrows 4xx structured errors so callers can branch on the code", async () => {
    const err = new ColonyApiError(400, "bad_request", "missing field");
    await expect(
      withDegradation(
        { name: "x.op" },
        async () => {
          throw err;
        },
        () => "fallback",
      ),
    ).rejects.toBe(err);
  });

  it("falls back on network / timeout / non-ColonyApiError exceptions", async () => {
    const warn = vi.fn();
    const result = await withDegradation(
      { name: "x.op", logger: { warn } },
      async () => {
        throw new Error("ECONNREFUSED");
      },
      () => "fallback",
    );
    expect(result).toBe("fallback");
    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0][0]).toMatch(/transport error/);
  });

  it("works without a logger (degrades silently)", async () => {
    const result = await withDegradation(
      { name: "x.op" },
      async () => {
        throw new Error("boom");
      },
      () => 42,
    );
    expect(result).toBe(42);
  });
});

describe("summarizeHostEvent", () => {
  it("formats turn_synced with counts", () => {
    const event: HostEvent = {
      type: "turn_synced",
      occurred_at: "2026-04-15T00:00:00Z",
      payload: {
        session_id: "sess-1",
        topics: ["a", "b", "c"],
        entities: ["X"],
        tools_used: ["mem", "web"],
      },
    };
    expect(summarizeHostEvent(event)).toBe(
      "turn_synced session=sess-1 topics=3 entities=1 tools=2",
    );
  });

  it("formats memory_consolidated with counters", () => {
    const event: HostEvent = {
      type: "memory_consolidated",
      occurred_at: "2026-04-15T00:00:00Z",
      payload: {
        pairs_examined: 100,
        pairs_merged: 4,
        conflicts_detected: 1,
      },
    };
    expect(summarizeHostEvent(event)).toBe(
      "memory_consolidated examined=100 merged=4 conflicts=1",
    );
  });

  it("formats log events with message string", () => {
    const event: HostEvent = {
      type: "log",
      occurred_at: "2026-04-15T00:00:00Z",
      payload: { message: "subscribed" },
    };
    expect(summarizeHostEvent(event)).toBe("log: subscribed");
  });

  it("returns only the type for unknown events (no payload leak)", () => {
    const event = {
      type: "some_future_event",
      occurred_at: "2026-04-15T00:00:00Z",
      payload: { sensitive: "must not leak" },
    } as unknown as HostEvent;
    expect(summarizeHostEvent(event)).toBe("some_future_event");
  });

  it("handles missing payload gracefully", () => {
    const event = {
      type: "turn_synced",
      occurred_at: "2026-04-15T00:00:00Z",
    } as unknown as HostEvent;
    expect(summarizeHostEvent(event)).toBe(
      "turn_synced session=? topics=0 entities=0 tools=0",
    );
  });
});

describe("ColonyEmbedUnavailableError", () => {
  it("carries the reason in its message", () => {
    const err = new ColonyEmbedUnavailableError("phase1_wiring_required");
    expect(err.message).toContain("phase1_wiring_required");
    expect(err.name).toBe("ColonyEmbedUnavailableError");
    expect(err).toBeInstanceOf(Error);
  });
});
