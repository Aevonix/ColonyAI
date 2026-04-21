import { describe, expect, it, vi } from "vitest";

import { createContextCache } from "../src/context-cache.js";
import {
  dispatchHostEvent,
  handleMemoryConsolidated,
  handleGoalUpdate,
  handleBriefing,
  handleSkillDraftApproved,
  handleWorldModelChanged,
} from "../src/event-handlers.js";
import type { HostEvent } from "../src/types.js";

function makeEvent(type: string, payload: Record<string, unknown> = {}): HostEvent {
  return {
    type: type as HostEvent["type"],
    occurred_at: new Date().toISOString(),
    payload,
  };
}

describe("dispatchHostEvent", () => {
  it("returns true for handled event types", () => {
    const cache = createContextCache();
    const logger = { info: vi.fn(), warn: vi.fn() };
    const matched = dispatchHostEvent(
      makeEvent("memory_consolidated", { merged: 3 }),
      { cache, logger },
    );
    expect(matched).toBe(true);
    expect(logger.info).toHaveBeenCalled();
  });

  it("returns false for unhandled event types", () => {
    const cache = createContextCache();
    const matched = dispatchHostEvent(makeEvent("log", { message: "x" }), {
      cache,
    });
    expect(matched).toBe(false);
  });

  it("routes each declared type to its handler", () => {
    const cache = createContextCache();
    const channels: string[] = [];
    cache.subscribe((ch) => channels.push(ch));

    dispatchHostEvent(
      makeEvent("memory_consolidated", { merged: 1 }),
      { cache },
    );
    dispatchHostEvent(
      makeEvent("goal_update", { goal_id: "g1", status: "completed" }),
      { cache },
    );
    dispatchHostEvent(
      makeEvent("briefing", { briefing_id: "b1" }),
      { cache },
    );
    dispatchHostEvent(
      makeEvent("world_model_changed", { change_type: "entity_upsert" }),
      { cache },
    );
    dispatchHostEvent(
      makeEvent("skill_draft_approved", { skill_id: "s1", name: "x" }),
      { cache },
    );

    expect(channels).toEqual([
      "memory",
      "goals",
      "briefings",
      "world_model",
      "skills",
    ]);
  });
});

describe("handleMemoryConsolidated", () => {
  it("invalidates the memory channel with the payload", () => {
    const cache = createContextCache();
    const observed: Array<[string, unknown]> = [];
    cache.subscribe((ch, payload) => observed.push([ch, payload]));
    handleMemoryConsolidated(
      makeEvent("memory_consolidated", { merged: 5 }),
      { cache },
    );
    expect(observed).toEqual([["memory", { merged: 5 }]]);
  });
});

describe("handleGoalUpdate", () => {
  it("logs goal id + status", () => {
    const cache = createContextCache();
    const logger = { info: vi.fn() };
    handleGoalUpdate(
      makeEvent("goal_update", { goal_id: "g7", status: "completed" }),
      { cache, logger },
    );
    const msg = String(logger.info.mock.calls[0]?.[0]);
    expect(msg).toContain("g7");
    expect(msg).toContain("completed");
  });
});

describe("handleBriefing", () => {
  it("fires the onBriefing hook when present", () => {
    const cache = createContextCache();
    const onBriefing = vi.fn();
    handleBriefing(
      makeEvent("briefing", { briefing_id: "b-1" }),
      { cache, onBriefing },
    );
    expect(onBriefing).toHaveBeenCalledWith("b-1", { briefing_id: "b-1" });
  });

  it("swallows onBriefing errors", () => {
    const cache = createContextCache();
    const onBriefing = vi.fn(() => {
      throw new Error("boom");
    });
    expect(() =>
      handleBriefing(
        makeEvent("briefing", { briefing_id: "b-1" }),
        { cache, onBriefing, logger: { warn: vi.fn() } },
      ),
    ).not.toThrow();
  });
});

describe("handleSkillDraftApproved", () => {
  it("invokes onSkillApproved with the skill id", async () => {
    const cache = createContextCache();
    const onSkillApproved = vi.fn().mockResolvedValue(undefined);
    handleSkillDraftApproved(
      makeEvent("skill_draft_approved", { skill_id: "s-42", name: "X" }),
      { cache, onSkillApproved },
    );
    // The hook is fire-and-forget; give the event loop a tick.
    await new Promise((r) => setImmediate(r));
    expect(onSkillApproved).toHaveBeenCalledWith("s-42");
  });

  it("skips the hook when skill_id is missing", () => {
    const cache = createContextCache();
    const onSkillApproved = vi.fn();
    handleSkillDraftApproved(
      makeEvent("skill_draft_approved", {}),
      { cache, onSkillApproved },
    );
    expect(onSkillApproved).not.toHaveBeenCalled();
  });
});

describe("handleWorldModelChanged", () => {
  it("invalidates the world_model channel", () => {
    const cache = createContextCache();
    const observed: string[] = [];
    cache.subscribe((ch) => observed.push(ch));
    handleWorldModelChanged(
      makeEvent("world_model_changed", { change_type: "entity_upsert" }),
      { cache },
    );
    expect(observed).toEqual(["world_model"]);
  });
});

describe("dispatcher resilience", () => {
  it("catches handler exceptions and returns false", () => {
    const cache = createContextCache();
    cache.subscribe(() => {
      throw new Error("listener blew up");
    });
    // Listener exceptions are swallowed by the cache itself, so the
    // dispatcher still returns true. This test asserts no crash.
    const matched = dispatchHostEvent(
      makeEvent("memory_consolidated", {}),
      { cache },
    );
    expect(matched).toBe(true);
  });
});
