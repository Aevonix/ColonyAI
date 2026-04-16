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
function makeCtx(caps: string[], opts?: { failSafetyClosed?: boolean }) {
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
  const health = vi.fn<[], Promise<HostHealthResponse>>().mockResolvedValue({
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
      ownMemoryCapability: false,
      forwardProactiveDeliveries: true,
      failSafetyClosed: opts?.failSafetyClosed ?? true,
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

/**
 * Build a ``PluginHookReplyDispatchEvent``-shaped mock. Matches the real
 * SDK shape at ``openclaw/dist/plugin-sdk/src/plugins/hook-types.d.ts``
 * lines 110-124.
 */
type ReplyEventMock = Parameters<
  ReturnType<typeof __postTurnHook>
>[0];

type ReplyCtxMock = Parameters<ReturnType<typeof __postTurnHook>>[1];

function makeEvent(overrides?: Partial<ReplyEventMock>): ReplyEventMock {
  return {
    ctx: {
      BodyForAgent: "hi there",
      Body: "hi",
      SessionKey: "sess-1",
      SenderId: "person-42",
      Provider: "telegram",
    },
    runId: "run-1",
    sessionKey: "sess-1",
    inboundAudio: false,
    shouldRouteToOriginating: false,
    shouldSendToolSummaries: false,
    sendPolicy: "allow",
    ...(overrides ?? {}),
  } as ReplyEventMock;
}

/**
 * Observer-only: we never touch any of the dispatcher plumbing, so a
 * stub with ``as unknown as ReplyCtxMock`` is enough for the tests that
 * assert call shape.
 */
const ctxStub = {
  cfg: undefined,
  dispatcher: undefined,
  recordProcessed: vi.fn(),
  markIdle: vi.fn(),
} as unknown as ReplyCtxMock;

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

  it("fires both signals/ingest and turns/sync when the sidecar advertises both", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["signals", "turn_sync"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(makeEvent(), ctxStub);

    expect(signalsIngest).toHaveBeenCalledTimes(1);
    const signalBody = signalsIngest.mock.calls[0][0] as SignalIngestRequest;
    // Derived from event.sessionKey (or msgCtx.SessionKey / runId fallback).
    expect(signalBody.context.session_id).toBe("sess-1");
    // Derived from msgCtx.SenderId (first in the fallback chain).
    expect(signalBody.context.contact_id).toBe("person-42");
    // Channel falls through msgCtx.Provider first.
    expect(signalBody.context.channel_id).toBe("telegram");
    expect(signalBody.context.turn_id).toBe("run-1");
    // Phase 6 scope: we send the best-available inbound text.
    expect(signalBody.incoming_message).toEqual({
      role: "user",
      content: "hi there",
    });

    expect(turnsSync).toHaveBeenCalledTimes(1);
    const syncBody = turnsSync.mock.calls[0][0] as TurnSyncRequest;
    // Phase 6 gap: extractors not yet implemented, so empty arrays.
    expect(syncBody.topics).toEqual([]);
    expect(syncBody.entities).toEqual([]);
    expect(syncBody.pending_tasks).toEqual([]);
    expect(syncBody.tools_used).toEqual([]);
    expect(syncBody.summary).toBeUndefined();
  });

  it("skips turns/sync when the sidecar doesn't advertise that capability", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["signals"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(makeEvent(), ctxStub);

    expect(signalsIngest).toHaveBeenCalledTimes(1);
    expect(turnsSync).not.toHaveBeenCalled();
  });

  it("skips signals/ingest when the sidecar doesn't advertise that capability", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["turn_sync"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(makeEvent(), ctxStub);

    expect(signalsIngest).not.toHaveBeenCalled();
    expect(turnsSync).toHaveBeenCalledTimes(1);
  });

  it("skips both when the probe succeeded and neither capability is advertised", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["memory"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(makeEvent(), ctxStub);

    expect(signalsIngest).not.toHaveBeenCalled();
    expect(turnsSync).not.toHaveBeenCalled();
  });

  it("fires both endpoints best-effort when the probe has failed", async () => {
    const { ctx, signalsIngest, turnsSync, health } = makeCtx([]);
    (health as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("network"),
    );
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(makeEvent(), ctxStub);

    expect(signalsIngest).toHaveBeenCalledTimes(1);
    expect(turnsSync).toHaveBeenCalledTimes(1);
  });

  it("short-circuits when sendPolicy === 'deny'", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["signals", "turn_sync"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(makeEvent({ sendPolicy: "deny" }), ctxStub);

    expect(signalsIngest).not.toHaveBeenCalled();
    expect(turnsSync).not.toHaveBeenCalled();
  });

  it("short-circuits when isTailDispatch === true", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["signals", "turn_sync"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    await hook(makeEvent({ isTailDispatch: true }), ctxStub);

    expect(signalsIngest).not.toHaveBeenCalled();
    expect(turnsSync).not.toHaveBeenCalled();
  });

  it("swallows a single rejection, logs it, and still awaits the sibling", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["signals", "turn_sync"]);
    (signalsIngest as unknown as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("signals offline"),
    );
    const warn = vi.fn();
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx), {
      warn,
      info: () => {},
      error: () => {},
      debug: () => {},
    } as unknown as Parameters<typeof __postTurnHook>[2]);

    // Must resolve cleanly — observer-only hook, never throws.
    await expect(hook(makeEvent(), ctxStub)).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalled();
    expect(warn.mock.calls[0][0]).toMatch(/signals offline/);
    expect(turnsSync).toHaveBeenCalledTimes(1);
  });

  it("swallows both rejections and logs each one", async () => {
    const { ctx, signalsIngest, turnsSync } = makeCtx(["signals", "turn_sync"]);
    (signalsIngest as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("signals offline"),
    );
    (turnsSync as unknown as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("turns offline"),
    );
    const warn = vi.fn();
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx), {
      warn,
      info: () => {},
      error: () => {},
      debug: () => {},
    } as unknown as Parameters<typeof __postTurnHook>[2]);

    await expect(hook(makeEvent(), ctxStub)).resolves.toBeUndefined();
    expect(warn.mock.calls.length).toBe(2);
    expect(warn.mock.calls.some((c) => /signals offline/.test(c[0]))).toBe(
      true,
    );
    expect(warn.mock.calls.some((c) => /turns offline/.test(c[0]))).toBe(true);
  });

  it("falls back through BodyForAgent → Body → RawBody → '' for incoming text", async () => {
    const { ctx, signalsIngest } = makeCtx(["signals"]);
    const hook = __postTurnHook(ctx, __capabilityProbe(ctx));

    // (1) BodyForAgent present
    await hook(
      makeEvent({ ctx: { BodyForAgent: "bfa" } }),
      ctxStub,
    );
    expect(
      (signalsIngest.mock.calls[0][0] as SignalIngestRequest).incoming_message,
    ).toEqual({ role: "user", content: "bfa" });

    // (2) Body only
    signalsIngest.mockClear();
    await hook(
      makeEvent({ ctx: { Body: "body" } }),
      ctxStub,
    );
    expect(
      (signalsIngest.mock.calls[0][0] as SignalIngestRequest).incoming_message,
    ).toEqual({ role: "user", content: "body" });

    // (3) RawBody only
    signalsIngest.mockClear();
    await hook(
      makeEvent({ ctx: { RawBody: "raw" } }),
      ctxStub,
    );
    expect(
      (signalsIngest.mock.calls[0][0] as SignalIngestRequest).incoming_message,
    ).toEqual({ role: "user", content: "raw" });

    // (4) None — incoming_message omitted entirely (empty string is falsy)
    signalsIngest.mockClear();
    await hook(makeEvent({ ctx: {} }), ctxStub);
    expect(
      (signalsIngest.mock.calls[0][0] as SignalIngestRequest).incoming_message,
    ).toBeUndefined();
  });
});
