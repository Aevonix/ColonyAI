import { beforeEach, describe, expect, it, vi } from "vitest";

import { __capabilityProbe, __safetyHook } from "../src/plugin.js";
import type { ColonyPluginContext } from "../src/plugin.js";
import { ColonyApiError } from "../src/sidecar-client.js";
import { SessionTextCache } from "../src/hooks/session-text-cache.js";
import type {
  HostHealthResponse,
  SafetyCheckRequest,
  SafetyCheckResponse,
} from "../src/types.js";

/**
 * Build a ``ColonyPluginContext`` + logger pair for safety-hook tests.
 * ``failSafetyClosed`` defaults to ``true`` (the shipped default).
 */
function makeCtx(opts?: {
  caps?: string[];
  probeFails?: boolean;
  failSafetyClosed?: boolean;
  safetyResult?: SafetyCheckResponse | Error;
}) {
  const caps = opts?.caps ?? ["response_gate"];
  const safetyCheck = vi.fn<
    [SafetyCheckRequest],
    Promise<SafetyCheckResponse>
  >();
  if (opts?.safetyResult instanceof Error) {
    safetyCheck.mockRejectedValue(opts.safetyResult);
  } else {
    safetyCheck.mockResolvedValue(
      opts?.safetyResult ?? {
        decision: "pass",
        blocked: false,
      },
    );
  }

  const health = vi.fn<[], Promise<HostHealthResponse>>();
  if (opts?.probeFails) {
    health.mockRejectedValue(new Error("probe-network"));
  } else {
    health.mockResolvedValue({
      status: "ok",
      api_version: "1",
      capabilities: caps,
    });
  }

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
      safetyCheck,
      health,
    } as unknown as ColonyPluginContext["client"],
    identity: () => ({ host_id: "host-test", plugin_version: "0.0.1" }),
  };

  const warn = vi.fn();
  const info = vi.fn();
  const logger = { warn, info, error: () => {}, debug: () => {} };

  return { ctx, safetyCheck, health, warn, info, logger };
}

type SendingEvent = Parameters<ReturnType<typeof __safetyHook>>[0];
type SendingCtx = Parameters<ReturnType<typeof __safetyHook>>[1];

function makeEvent(overrides?: Partial<SendingEvent>): SendingEvent {
  return {
    to: "user-123",
    content: "the outbound chunk",
    metadata: undefined,
    ...(overrides ?? {}),
  } as SendingEvent;
}

function makeHookCtx(overrides?: Partial<SendingCtx>): SendingCtx {
  return {
    channelId: "telegram",
    accountId: "acct-1",
    conversationId: "conv-42",
    ...(overrides ?? {}),
  } as SendingCtx;
}

describe("safetyHook", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("passes through when the sidecar decision is 'pass'", async () => {
    const { ctx, safetyCheck, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: { decision: "pass", blocked: false },
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toBeUndefined();
    expect(safetyCheck).toHaveBeenCalledTimes(1);
    const body = safetyCheck.mock.calls[0][0];
    expect(body.response_text).toBe("the outbound chunk");
  });

  it("cancels when the sidecar returns blocked=true / decision='block' and logs the reason WITHOUT leaking it via the return", async () => {
    const { ctx, safetyCheck, warn, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: {
        decision: "block",
        blocked: true,
        reason: "pii leak",
      },
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toEqual({ cancel: true });
    // The SDK strips any extra fields, so the return shape MUST be just
    // ``{ cancel: true }`` — no ``reason`` leaking through.
    expect(Object.keys(result as object).sort()).toEqual(["cancel"]);
    expect(safetyCheck).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalled();
    expect(warn.mock.calls[0][0]).toMatch(/pii leak/);
  });

  it("passes through without calling safetyCheck when the probe succeeded and didn't report 'safety'", async () => {
    const { ctx, safetyCheck, logger } = makeCtx({ caps: ["memory"] });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toBeUndefined();
    expect(safetyCheck).not.toHaveBeenCalled();
  });

  it("still calls safetyCheck when the probe failed (unknown state → try anyway)", async () => {
    const { ctx, safetyCheck, logger } = makeCtx({
      probeFails: true,
      safetyResult: { decision: "pass", blocked: false },
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    await hook(makeEvent(), makeHookCtx());

    expect(safetyCheck).toHaveBeenCalledTimes(1);
  });

  it("fails closed on a 501 when failSafetyClosed=true", async () => {
    const { ctx, warn, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: new ColonyApiError(
        501,
        "phase1_wiring_required",
        "not wired",
      ),
      failSafetyClosed: true,
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toEqual({ cancel: true });
    expect(warn).toHaveBeenCalled();
  });

  it("fails OPEN on a 501 when failSafetyClosed=false", async () => {
    const { ctx, warn, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: new ColonyApiError(
        501,
        "phase1_wiring_required",
        "not wired",
      ),
      failSafetyClosed: false,
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toBeUndefined();
    expect(warn).toHaveBeenCalled();
  });

  it("fails closed on a generic 5xx when failSafetyClosed=true", async () => {
    const { ctx, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: new ColonyApiError(503, "unavailable", "pool exhausted"),
      failSafetyClosed: true,
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toEqual({ cancel: true });
  });

  it("fails closed on a network error when failSafetyClosed=true", async () => {
    const { ctx, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: new Error("ECONNREFUSED"),
      failSafetyClosed: true,
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toEqual({ cancel: true });
  });

  it("fails closed on a 4xx contract error (and does NOT propagate the error out of the hook)", async () => {
    const { ctx, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: new ColonyApiError(400, "bad_request", "missing field"),
      failSafetyClosed: true,
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    // The critical assertion is that the call resolves at all — hook
    // handlers must NEVER throw, per the SDK contract. Previous
    // ``withDegradation`` behavior would have let the 4xx propagate.
    const result = await hook(makeEvent(), makeHookCtx());
    expect(result).toEqual({ cancel: true });
  });

  it("builds a SafetyCheckRequest with the expected identity mapping", async () => {
    const { ctx, safetyCheck, logger } = makeCtx({ caps: ["response_gate"] });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    await hook(
      makeEvent({ to: "user-999", content: "hello" }),
      makeHookCtx({
        channelId: "telegram",
        accountId: "acct-1",
        conversationId: "conv-42",
      }),
    );

    const body = safetyCheck.mock.calls[0][0];
    expect(body.response_text).toBe("hello");
    expect(body.target_gateway).toBe("telegram");
    expect(body.context.channel_id).toBe("telegram");
    // session_id is derived from channelId:conversationId when a
    // conversationId is available.
    expect(body.context.session_id).toBe("telegram:conv-42");
    // contact_id falls through accountId → conversationId → to → unknown.
    expect(body.context.contact_id).toBe("acct-1");

    // Now drop accountId — contact_id should fall through to conversationId.
    safetyCheck.mockClear();
    await hook(
      makeEvent({ to: "user-999", content: "hello" }),
      makeHookCtx({
        channelId: "telegram",
        accountId: undefined,
        conversationId: "conv-42",
      }),
    );
    expect(safetyCheck.mock.calls[0][0].context.contact_id).toBe("conv-42");

    // Drop conversationId too — falls through to event.to.
    safetyCheck.mockClear();
    await hook(
      makeEvent({ to: "user-999", content: "hello" }),
      makeHookCtx({
        channelId: "telegram",
        accountId: undefined,
        conversationId: undefined,
      }),
    );
    expect(safetyCheck.mock.calls[0][0].context.contact_id).toBe("user-999");
    // And session_id falls through to channelId:to when no conversationId.
    expect(safetyCheck.mock.calls[0][0].context.session_id).toBe(
      "telegram:user-999",
    );
  });

  it("treats a 'pending' decision as a block and logs the reason", async () => {
    const { ctx, warn, logger } = makeCtx({
      caps: ["response_gate"],
      safetyResult: {
        decision: "pending",
        blocked: false,
        reason: "awaiting human review",
      },
    });
    const hook = __safetyHook(ctx, __capabilityProbe(ctx), new SessionTextCache(), logger);

    const result = await hook(makeEvent(), makeHookCtx());

    expect(result).toEqual({ cancel: true });
    expect(warn).toHaveBeenCalled();
    expect(warn.mock.calls[0][0]).toMatch(/pending/);
  });
});
