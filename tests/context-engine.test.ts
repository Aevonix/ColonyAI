import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  __capabilityProbe,
  __contextEngineFactory,
  ColonyApiError,
} from "../src/plugin.js";
import type { AgentMessage, ColonyPluginContext } from "../src/plugin.js";
import { createContextCache } from "../src/context-cache.js";
import type {
  ContextAssembleRequest,
  ContextAssembleResponse,
  HostHealthResponse,
} from "../src/types.js";

// A lot of the OpenClaw SDK's ``delegateCompactionToRuntime`` runs real
// runtime code that we don't want executing in unit tests. Mock the
// module at test time so Phase 4's ``compact`` adapter can be exercised
// in isolation without dragging in session-store plumbing.
const delegateCompactionToRuntimeMock = vi.fn();
vi.mock("openclaw/plugin-sdk", () => ({
  delegateCompactionToRuntime: (...args: unknown[]) =>
    delegateCompactionToRuntimeMock(...args),
}));

vi.mock("openclaw/plugin-sdk/agent-harness", () => ({
  normalizeUsage: (u: unknown) => u,
}));

/**
 * Builds a ``ColonyPluginContext`` backed by vitest mocks plus a
 * ``capabilityProbe`` matching the supplied capability set.
 */
function makeCtx(opts?: {
  capabilities?: string[];
  healthFails?: boolean;
  assembleResponse?: ContextAssembleResponse;
  assembleImpl?: (
    body: ContextAssembleRequest,
  ) => Promise<ContextAssembleResponse>;
}) {
  const enrichedContext = vi.fn<
    [ContextAssembleRequest],
    Promise<ContextAssembleResponse>
  >();
  if (opts?.assembleImpl) {
    enrichedContext.mockImplementation(opts.assembleImpl);
  } else {
    enrichedContext.mockResolvedValue(
      opts?.assembleResponse ?? { sections: [], notices: [] },
    );
  }

  const health = vi.fn<[], Promise<HostHealthResponse>>();
  if (opts?.healthFails) {
    health.mockRejectedValue(new Error("ECONNREFUSED"));
  } else {
    health.mockResolvedValue({
      status: "ok",
      api_version: "1",
      capabilities: opts?.capabilities ?? ["context"],
    });
  }

  const logger = {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };

  const ctx: ColonyPluginContext = {
    config: {
      sidecarUrl: "http://stub",
      apiKey: "x",
      hostId: "host-test",
      requestTimeoutMs: 2_000,
      ownReasoningLoop: false,
      ownMemoryCapability: true,
      forwardProactiveDeliveries: true,
    } as unknown as ColonyPluginContext["config"],
    client: {
      enrichedContext,
      health,
    } as unknown as ColonyPluginContext["client"],
    identity: () => ({ host_id: "host-test", plugin_version: "0.0.1" }),
    refreshIdentity: async () => ({}),
    cache: createContextCache(),
    logger,
  };

  const caps = __capabilityProbe(ctx);
  return { ctx, caps, enrichedContext, health, logger };
}

function userMessage(text: string): AgentMessage {
  return { role: "user", content: text } as AgentMessage;
}

function assistantMessage(text: string): AgentMessage {
  return { role: "assistant", content: text } as AgentMessage;
}

const BASE_ASSEMBLE_PARAMS = {
  sessionId: "sess-1",
  sessionKey: "sess-key-1",
};

describe("contextEngineFactory — info", () => {
  it("advertises id, name, and delegates compaction", () => {
    const { ctx, caps } = makeCtx();
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    expect(engine.info.id).toBe("colony");
    expect(engine.info.name.length).toBeGreaterThan(0);
    expect(engine.info.ownsCompaction).toBe(false);
  });
});

describe("contextEngineFactory — ingest", () => {
  it("returns { ingested: true } and performs zero sidecar calls", async () => {
    const { ctx, caps, enrichedContext } = makeCtx();
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    const res = await engine.ingest({
      sessionId: "sess-1",
      message: userMessage("hi"),
    });
    expect(res.ingested).toBe(true);
    expect(enrichedContext).not.toHaveBeenCalled();
  });
});

describe("contextEngineFactory — assemble happy path", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders sections into systemPromptAddition, passes messages through", async () => {
    const { ctx, caps } = makeCtx({
      assembleResponse: {
        sections: [
          {
            id: "s1",
            title: "Recent Context",
            body: "The user cares about Colony.",
            priority: 10,
          },
        ],
        notices: [],
      },
    });
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();

    const messages = [userMessage("tell me about Colony")];
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages,
    });

    expect(res.systemPromptAddition).toContain("Recent Context");
    expect(res.systemPromptAddition).toContain(
      "The user cares about Colony.",
    );
    // Identity pass-through so callers that compare by reference see
    // no change.
    expect(res.messages).toBe(messages);
    expect(res.estimatedTokens).toBeGreaterThan(0);
  });

  it("renders notices under a 'Notices:' prefix", async () => {
    const { ctx, caps } = makeCtx({
      assembleResponse: {
        sections: [],
        notices: ["sidecar warning: stale embeddings"],
      },
    });
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("hello")],
    });
    expect(res.systemPromptAddition).toContain("Notices:");
    expect(res.systemPromptAddition).toContain(
      "stale embeddings",
    );
  });

  it("returns undefined systemPromptAddition when sections and notices are empty", async () => {
    const { ctx, caps } = makeCtx({
      assembleResponse: { sections: [], notices: [] },
    });
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    const messages = [userMessage("hello")];
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages,
    });
    expect(res.systemPromptAddition).toBeUndefined();
    expect(res.messages).toBe(messages);
  });
});

describe("contextEngineFactory — assemble prompt / message walk", () => {
  beforeEach(() => vi.clearAllMocks());

  it("uses params.prompt verbatim when provided and ignores the messages walk", async () => {
    const { ctx, caps, enrichedContext } = makeCtx();
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("OLD user message")],
      prompt: "NEW prompt from caller",
    });
    expect(enrichedContext).toHaveBeenCalledTimes(1);
    const body = enrichedContext.mock.calls[0]![0];
    expect(body.message).toBe("NEW prompt from caller");
  });

  it("walks messages backwards to find the latest user turn", async () => {
    const { ctx, caps, enrichedContext } = makeCtx();
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [
        userMessage("first"),
        assistantMessage("reply"),
        userMessage("second"),
        assistantMessage("another reply"),
      ],
    });
    const body = enrichedContext.mock.calls[0]![0];
    expect(body.message).toBe("second");
  });

  it("passes through without calling the sidecar when no prompt and no user message", async () => {
    const { ctx, caps, enrichedContext } = makeCtx();
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    const messages = [assistantMessage("only assistant")];
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages,
    });
    expect(enrichedContext).not.toHaveBeenCalled();
    expect(res.messages).toBe(messages);
    expect(res.systemPromptAddition).toBeUndefined();
  });
});

describe("contextEngineFactory — assemble degradation", () => {
  beforeEach(() => vi.clearAllMocks());

  it("passes through and warns on ColonyApiError(501, phase1_wiring_required)", async () => {
    const { ctx, caps, logger } = makeCtx({
      assembleImpl: async () => {
        throw new ColonyApiError(
          501,
          "phase1_wiring_required",
          "not wired",
        );
      },
    });
    const engine = __contextEngineFactory(ctx, caps, logger)();
    const messages = [userMessage("hi")];
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages,
    });
    expect(res.messages).toBe(messages);
    // Fallback yields a "degraded" notice rendered under the Notices: block.
    expect(res.systemPromptAddition).toContain("Notices:");
    expect(res.systemPromptAddition).toContain("degraded");
    expect(logger.warn).toHaveBeenCalled();
  });

  it("passes through and warns on ColonyApiError(503)", async () => {
    const { ctx, caps, logger } = makeCtx({
      assembleImpl: async () => {
        throw new ColonyApiError(503, "sidecar_overloaded", "retry");
      },
    });
    const engine = __contextEngineFactory(ctx, caps, logger)();
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("hi")],
    });
    expect(res.systemPromptAddition).toContain("degraded");
    expect(logger.warn).toHaveBeenCalled();
  });

  it("re-throws on ColonyApiError(400) (structured contract error)", async () => {
    const err = new ColonyApiError(
      400,
      "bad_request",
      "invalid contact id",
    );
    const { ctx, caps, logger } = makeCtx({
      assembleImpl: async () => {
        throw err;
      },
    });
    const engine = __contextEngineFactory(ctx, caps, logger)();
    await expect(
      engine.assemble({
        ...BASE_ASSEMBLE_PARAMS,
        messages: [userMessage("hi")],
      }),
    ).rejects.toBe(err);
  });

  it("passes through and warns on network errors", async () => {
    const { ctx, caps, logger } = makeCtx({
      assembleImpl: async () => {
        throw new Error("ETIMEDOUT");
      },
    });
    const engine = __contextEngineFactory(ctx, caps, logger)();
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("hi")],
    });
    expect(res.systemPromptAddition).toContain("degraded");
    expect(logger.warn).toHaveBeenCalled();
  });
});

describe("contextEngineFactory — capability gate", () => {
  beforeEach(() => vi.clearAllMocks());

  it("skips sidecar when probe succeeded and 'context' is not advertised", async () => {
    const { ctx, caps, enrichedContext } = makeCtx({
      capabilities: ["memory"], // no "context"
    });
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    const messages = [userMessage("hello")];
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages,
    });
    expect(enrichedContext).not.toHaveBeenCalled();
    expect(res.messages).toBe(messages);
    expect(res.systemPromptAddition).toBeUndefined();
  });

  it("still calls the sidecar when the probe failed (unknown state)", async () => {
    const { ctx, caps, enrichedContext } = makeCtx({
      healthFails: true,
    });
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("hello")],
    });
    expect(enrichedContext).toHaveBeenCalledTimes(1);
  });
});

describe("contextEngineFactory — assemble wire params", () => {
  beforeEach(() => vi.clearAllMocks());

  it("passes identity, context, message, and features to enrichedContext", async () => {
    const { ctx, caps, enrichedContext } = makeCtx();
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("hi")],
    });
    const body = enrichedContext.mock.calls[0]![0];
    expect(body.identity).toBeDefined();
    expect(body.context).toBeDefined();
    expect(body.message).toBe("hi");
    expect(body.features).toEqual({
      memory: true,
      relationships: true,
      style: true,
      goals: true,
      worldModel: true,
      insights: true,
      identity: true,
      briefings: true,
      contactsList: true,
      cognition: true,
    });
  });
});

describe("contextEngineFactory — compact", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    delegateCompactionToRuntimeMock.mockReset();
  });

  it("delegates to delegateCompactionToRuntime and returns its result", async () => {
    const { ctx, caps } = makeCtx();
    const expected = {
      ok: true,
      compacted: true,
      result: { tokensBefore: 100, tokensAfter: 40 },
    };
    delegateCompactionToRuntimeMock.mockResolvedValueOnce(expected);

    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    const res = await engine.compact({
      sessionId: "sess-1",
      sessionFile: "/tmp/sess",
    });
    expect(delegateCompactionToRuntimeMock).toHaveBeenCalledTimes(1);
    expect(res).toBe(expected);
  });
});

describe("contextEngineFactory — section ordering and token heuristic", () => {
  beforeEach(() => vi.clearAllMocks());

  it("orders sections with higher priority first in systemPromptAddition", async () => {
    const { ctx, caps } = makeCtx({
      assembleResponse: {
        sections: [
          { id: "low", title: "Low", body: "LOW-BODY", priority: 10 },
          { id: "hi", title: "Hi", body: "HI-BODY", priority: 100 },
        ],
        notices: [],
      },
    });
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();
    const res = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("x")],
    });
    const addition = res.systemPromptAddition ?? "";
    expect(addition.indexOf("HI-BODY")).toBeGreaterThanOrEqual(0);
    expect(addition.indexOf("LOW-BODY")).toBeGreaterThan(
      addition.indexOf("HI-BODY"),
    );
  });

  it("estimatedTokens is a non-negative integer that grows with input size", async () => {
    const { ctx, caps } = makeCtx();
    const engine = __contextEngineFactory(ctx, caps, ctx.logger)();

    const small = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("short")],
    });
    const large = await engine.assemble({
      ...BASE_ASSEMBLE_PARAMS,
      messages: [userMessage("x".repeat(4_000))],
    });

    for (const v of [small.estimatedTokens, large.estimatedTokens]) {
      expect(Number.isInteger(v)).toBe(true);
      expect(v).toBeGreaterThanOrEqual(0);
    }
    expect(large.estimatedTokens).toBeGreaterThan(small.estimatedTokens);
  });
});
