import { beforeEach, describe, expect, it, vi } from "vitest";

import { __capabilityProbe, __postTurnHook } from "../src/plugin.js";
import type { ColonyPluginContext } from "../src/plugin.js";
import type {
  HostHealthResponse,
  SignalIngestRequest,
  TurnSyncRequest,
} from "../src/types.js";

/**
 * Build a lightweight ``ColonyPluginContext`` backed by vitest mocks so we
 * can assert exactly which sidecar calls fire for a given set of
 * capabilities. Matches the shape the real ``buildContext`` produces.
 */
function makeCtx(caps: string[]) {
  const signalsIngest = vi.fn().mockResolvedValue({
    accepted: true,
    signals_recorded: 0,
  });
  const turnsSync = vi.fn().mockResolvedValue({
    accepted: true,
    continuity_updated: true,
    skipped_reason: null,
    errors: [],
  });
  const health = vi.fn<
    [],
    Promise<HostHealthResponse>
  >().mockResolvedValue({
    status: "ok",
    api_version: "1",
    capabilities: caps,
  });

  const ctx: ColonyPluginContext = {
    config: {
      sidecarUrl: "http://stub",
      apiKey: "x",
      hostId: "host-test",
      requestTimeoutMs: 2_000,
      ownReasoningLoop: false,
      forwardProactiveDeliveries: true,
    } as unknown as ColonyPluginContext["config"],
    client: {
      signalsIngest,
      turnsSync,
      health,
    } as unknown as ColonyPluginContext["client"],
    identity: () => ({ host_id: "host-test", plugin_version: "0.0.1" }),
  };

  return { ctx, signalsIngest, turnsSync, health };
}

const event = {
  context: {
    session_id: "sess-1",
    contact_id: "person-42",
  },
  incomingMessage: { role: "user" as const, content: "hi" },
  outgoingMessage: { role: "assistant" as const, content: "hello back" },
  topics: ["greeting"],
  entities: ["Colony"],
  pendingTasks: [],
  toolsUsed: ["memory"],
  summary: "A warm hello",
};

describe("capabilityProbe", () => {
  it("caches the health response across calls", async () => {
    const { ctx, health } = makeCtx(["turn_sync", "events"]);
    const probe = __capabilityProbe(ctx);

    expect(await probe.has("turn_sync")).toBe(true);
    expect(await probe.has("events")).toBe(true);
    expect(await probe.has("reasoning")).toBe(false);
    expect(health).toHaveBeenCalledTimes(1);
  });

  it("re-probes after a failure so one bad turn doesn't silence sync forever", async () => {
    const { ctx, health } = makeCtx(["turn_sync"]);
    const probe = __capabilityProbe(ctx);

    (health as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("network"),
    );

    expect(await probe.has("turn_sync")).toBe(false);
    // Second call re-probes and succeeds.
    expect(await probe.has("turn_sync")).toBe(true);
    expect(health).toHaveBeenCalledTimes(2);
  });
});

describe("postTurnHook", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("always calls signals/ingest and calls turns/sync when capability is present", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["turn_sync"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(event);

    expect(signalsIngest).toHaveBeenCalledTimes(1);
    const signalBody = signalsIngest.mock.calls[0][0] as SignalIngestRequest;
    expect(signalBody.context.session_id).toBe("sess-1");

    expect(turnsSync).toHaveBeenCalledTimes(1);
    const syncBody = turnsSync.mock.calls[0][0] as TurnSyncRequest;
    expect(syncBody.topics).toEqual(["greeting"]);
    expect(syncBody.tools_used).toEqual(["memory"]);
    expect(syncBody.summary).toBe("A warm hello");
  });

  it("skips turns/sync when the sidecar doesn't advertise the capability", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["memory", "events"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(event);

    expect(signalsIngest).toHaveBeenCalledTimes(1);
    expect(turnsSync).not.toHaveBeenCalled();
  });

  it("rethrows the first failure but lets the sibling call finish", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["turn_sync"]);
    (signalsIngest as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("signals offline"),
    );
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await expect(hook(event)).rejects.toThrow(/signals offline/);
    // The other call still ran — we want at-least-once sync on partial
    // failure, not silent loss.
    expect(turnsSync).toHaveBeenCalledTimes(1);
  });
});
