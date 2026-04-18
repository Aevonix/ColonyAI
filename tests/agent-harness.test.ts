import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  __agentHarness,
  __capabilityProbe,
  ColonyApiError,
} from "../src/plugin.js";
import type { ColonyPluginContext } from "../src/plugin.js";
import type {
  AgentHarnessAttemptParams,
  AgentHarnessSupportContext,
} from "openclaw/plugin-sdk/agent-harness";
import type {
  HostHealthResponse,
  ReasoningTurnRequest,
  ReasoningTurnResponse,
} from "../src/types.js";

/**
 * Tests for the Colony ``AgentHarness`` adapter returned by
 * ``agentHarness``.
 *
 * The harness has two surfaces:
 *
 *   1. ``supports(ctx)`` — synchronous gate consulted by OpenClaw's
 *      harness registry on every turn. Must return ``{supported: false}``
 *      with a human-readable reason whenever the sidecar can't
 *      currently handle the turn.
 *   2. ``runAttempt(params)`` — NEVER throws. Every failure mode must
 *      surface as a shaped ``EmbeddedRunAttemptResult`` with
 *      ``promptError`` set so OpenClaw can fall back to PI.
 *
 * The ``supports()`` truth table covers: ``ownReasoningLoop`` gating,
 * ``requestedRuntime`` filtering, the capability probe lifecycle, and
 * ``reasoning`` advertising. ``runAttempt()`` coverage spans the happy
 * path, all four Colony error-class branches (501, 5xx, 4xx, transport),
 * pre-aborted signal handling, request-body translation, and abort
 * propagation via the sidecar-client ``signal`` option.
 */

type ReasoningTurnCall = (
  body: ReasoningTurnRequest,
  opts?: { signal?: AbortSignal },
) => Promise<ReasoningTurnResponse>;

type HealthCall = () => Promise<HostHealthResponse>;

function makeCtx(overrides?: {
  ownReasoningLoop?: boolean;
  healthCapabilities?: string[];
  healthFn?: HealthCall;
  reasoningFn?: ReasoningTurnCall;
  reasoningResponse?: ReasoningTurnResponse;
}) {
  const reasoningTurn = vi.fn<
    [ReasoningTurnRequest, { signal?: AbortSignal } | undefined],
    Promise<ReasoningTurnResponse>
  >();
  if (overrides?.reasoningFn) {
    reasoningTurn.mockImplementation(overrides.reasoningFn);
  } else {
    reasoningTurn.mockResolvedValue(
      overrides?.reasoningResponse ?? {
        status: "completed",
        tool_calls: [],
      },
    );
  }

  const health = vi.fn<[], Promise<HostHealthResponse>>();
  if (overrides?.healthFn) {
    health.mockImplementation(overrides.healthFn);
  } else {
    health.mockResolvedValue({
      status: "ok",
      api_version: "1",
      capabilities: overrides?.healthCapabilities ?? ["reasoning"],
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
      ownReasoningLoop: overrides?.ownReasoningLoop ?? true,
      ownMemoryCapability: false,
      forwardProactiveDeliveries: true,
    } as unknown as ColonyPluginContext["config"],
    client: {
      reasoningTurn,
      health,
    } as unknown as ColonyPluginContext["client"],
    identity: () => ({ host_id: "host-test", plugin_version: "0.0.1" }),
    logger,
  };

  const caps = __capabilityProbe(ctx);
  return { ctx, caps, reasoningTurn, health, logger };
}

/**
 * Build ``AgentHarnessAttemptParams`` with safe defaults. The SDK type
 * has many fields that the harness does not actually read; we cast
 * through ``unknown`` so tests only specify what the adapter observes.
 */
function makeParams(
  overrides?: Partial<AgentHarnessAttemptParams> & {
    [k: string]: unknown;
  },
): AgentHarnessAttemptParams {
  const base = {
    sessionId: "sess-1",
    sessionFile: "/tmp/sess",
    workspaceDir: "/tmp/ws",
    runId: "run-1",
    prompt: "hello",
    provider: "openai",
    modelId: "gpt-4",
    model: { api: "openai", provider: "openai", id: "gpt-4" },
    thinkLevel: "off",
    timeoutMs: 60_000,
    authStorage: {},
    modelRegistry: {},
    ...overrides,
  };
  return base as unknown as AgentHarnessAttemptParams;
}

function makeSupportCtx(
  requestedRuntime: string,
): AgentHarnessSupportContext {
  return {
    provider: "openai",
    modelId: "gpt-4",
    requestedRuntime: requestedRuntime as AgentHarnessSupportContext["requestedRuntime"],
  };
}

// ---------------------------------------------------------------------------
// supports() truth table
// ---------------------------------------------------------------------------

describe("agentHarness.supports", () => {
  beforeEach(() => vi.clearAllMocks());

  it("1. returns {supported:false} when ownReasoningLoop=false", () => {
    const { ctx, caps } = makeCtx({ ownReasoningLoop: false });
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = harness.supports(makeSupportCtx("colony"));
    expect(res.supported).toBe(false);
    if (!res.supported) {
      expect(res.reason).toMatch(/ownReasoningLoop/);
    }
  });

  it("2. returns {supported:false} when requestedRuntime=pi", () => {
    const { ctx, caps } = makeCtx();
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = harness.supports(makeSupportCtx("pi"));
    expect(res.supported).toBe(false);
    if (!res.supported) {
      expect(res.reason).toMatch(/requestedRuntime/);
    }
  });

  it("3. returns {supported:false} when caps not probed yet", () => {
    const { ctx, caps } = makeCtx();
    expect(caps.snapshot().probed).toBe(false);
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = harness.supports(makeSupportCtx("colony"));
    expect(res.supported).toBe(false);
    if (!res.supported) {
      expect(res.reason).toMatch(/not yet probed|probed/i);
    }
  });

  it("4. returns {supported:false} when probed but 'reasoning' is absent", async () => {
    const { ctx, caps } = makeCtx({ healthCapabilities: ["memory"] });
    // Kick & await the probe so snapshot() reports probed:true with a set
    // that does NOT contain "reasoning".
    await caps.kick();
    expect(caps.snapshot().probed).toBe(true);
    expect(caps.snapshot().caps.has("reasoning")).toBe(false);
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = harness.supports(makeSupportCtx("colony"));
    expect(res.supported).toBe(false);
    if (!res.supported) {
      expect(res.reason).toMatch(/does not advertise|reasoning/);
    }
  });

  it("5. returns {supported:true, priority:50} when probed and 'reasoning' present (colony)", async () => {
    const { ctx, caps } = makeCtx({ healthCapabilities: ["reasoning"] });
    await caps.kick();
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = harness.supports(makeSupportCtx("colony"));
    expect(res.supported).toBe(true);
    if (res.supported) {
      expect(res.priority).toBe(50);
    }
  });

  it("6. returns {supported:true} when probed and 'reasoning' present (auto)", async () => {
    const { ctx, caps } = makeCtx({ healthCapabilities: ["reasoning"] });
    await caps.kick();
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = harness.supports(makeSupportCtx("auto"));
    expect(res.supported).toBe(true);
  });

  it("7. returns {supported:false} for an unknown runtime id (codex)", () => {
    const { ctx, caps } = makeCtx();
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = harness.supports(makeSupportCtx("codex"));
    expect(res.supported).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// runAttempt() — happy path + failure modes
// ---------------------------------------------------------------------------

describe("agentHarness.runAttempt", () => {
  beforeEach(() => vi.clearAllMocks());

  it("8. happy path — full result with messages, tool calls, usage", async () => {
    const response: ReasoningTurnResponse = {
      status: "completed",
      message: { role: "user", content: "hello" },
      tool_calls: [
        {
          id: "call-1",
          name: "search",
          arguments: { query: "foo" },
        },
        {
          id: "call-2",
          name: "fetch",
          arguments: { url: "https://example" },
        },
      ],
      usage: { input_tokens: 12, output_tokens: 8 },
    };
    const { ctx, caps } = makeCtx({ reasoningResponse: response });
    const harness = __agentHarness(ctx, caps, ctx.logger);

    const params = makeParams({ prompt: "hello" });
    const res = await harness.runAttempt(params);

    expect(res.sessionIdUsed).toBe(params.sessionId);
    expect(res.messagesSnapshot.length).toBe(2);
    expect(res.assistantTexts).toEqual(["hello"]);
    expect(res.lastAssistant).toBeDefined();

    // lastAssistant.content should include one text part AND both tool calls.
    const content = (res.lastAssistant as { content: Array<{ type: string }> })
      .content;
    const textParts = content.filter((c) => c.type === "text");
    const toolParts = content.filter((c) => c.type === "toolCall");
    expect(textParts.length).toBe(1);
    expect(toolParts.length).toBe(2);

    expect(res.attemptUsage).toBeDefined();
    expect(res.promptError).toBeNull();
    expect(res.promptErrorSource).toBeNull();

    // Safe defaults for the rest of the required booleans.
    expect(res.aborted).toBe(false);
    expect(res.externalAbort).toBe(false);
    expect(res.timedOut).toBe(false);
    expect(res.idleTimedOut).toBe(false);
    expect(res.timedOutDuringCompaction).toBe(false);
    expect(res.cloudCodeAssistFormatError).toBe(false);
    expect(res.didSendViaMessagingTool).toBe(false);

    expect(res.replayMetadata).toEqual({
      hadPotentialSideEffects: false,
      replaySafe: true,
    });
    expect(res.itemLifecycle).toEqual({
      startedCount: 0,
      completedCount: 0,
      activeCount: 0,
    });
  });

  it("9. ColonyApiError(501, phase1_wiring_required) — shaped promptError, no throw", async () => {
    const { ctx, caps, logger } = makeCtx({
      reasoningFn: async () => {
        throw new ColonyApiError(
          501,
          "phase1_wiring_required",
          "not wired",
        );
      },
    });
    const harness = __agentHarness(ctx, caps, logger);
    const res = await harness.runAttempt(makeParams());
    expect(res.promptError).not.toBeNull();
    expect(String(res.promptError)).toMatch(/501|phase1/);
    expect(res.promptErrorSource).toBe("prompt");
    // Safe defaults preserved.
    expect(res.aborted).toBe(false);
    expect(res.timedOut).toBe(false);
    expect(res.sessionIdUsed).toBe("sess-1");
  });

  it("10. ColonyApiError(502) — graceful promptError mentioning 502 / sidecar error", async () => {
    const { ctx, caps, logger } = makeCtx({
      reasoningFn: async () => {
        throw new ColonyApiError(502, "bad_gateway", "upstream down");
      },
    });
    const harness = __agentHarness(ctx, caps, logger);
    const res = await harness.runAttempt(makeParams());
    expect(res.promptError).not.toBeNull();
    expect(String(res.promptError)).toMatch(/502|sidecar error/i);
    expect(res.promptErrorSource).toBe("prompt");
  });

  it("11. ColonyApiError(400) — graceful promptError (NOT rethrown)", async () => {
    const { ctx, caps, logger } = makeCtx({
      reasoningFn: async () => {
        throw new ColonyApiError(400, "bad_request", "invalid input");
      },
    });
    const harness = __agentHarness(ctx, caps, logger);
    // MUST NOT throw — key divergence from context-engine's 4xx rethrow policy.
    const res = await harness.runAttempt(makeParams());
    expect(res.promptError).not.toBeNull();
    expect(res.promptErrorSource).toBe("prompt");
  });

  it("12. transport error — graceful promptError with transport / message", async () => {
    const { ctx, caps, logger } = makeCtx({
      reasoningFn: async () => {
        throw new Error("ECONNREFUSED");
      },
    });
    const harness = __agentHarness(ctx, caps, logger);
    const res = await harness.runAttempt(makeParams());
    expect(res.promptError).not.toBeNull();
    expect(String(res.promptError)).toMatch(/transport|ECONNREFUSED/);
    expect(res.promptErrorSource).toBe("prompt");
  });

  it("13. pre-aborted signal — aborted:true, externalAbort:true, no sidecar call", async () => {
    const { ctx, caps, reasoningTurn } = makeCtx();
    const harness = __agentHarness(ctx, caps, ctx.logger);

    const controller = new AbortController();
    controller.abort();

    const res = await harness.runAttempt(
      makeParams({ abortSignal: controller.signal }),
    );
    expect(res.aborted).toBe(true);
    expect(res.externalAbort).toBe(true);
    expect(reasoningTurn).not.toHaveBeenCalled();
  });

  it("14. request-body translation — context, messages, available_tools, model_override, identity", async () => {
    const { ctx, caps, reasoningTurn } = makeCtx();
    const harness = __agentHarness(ctx, caps, ctx.logger);

    const clientTools = [
      {
        type: "function" as const,
        function: { name: "tool_a", parameters: {} },
      },
      {
        type: "function" as const,
        function: { name: "tool_b", parameters: {} },
      },
    ];

    await harness.runAttempt(
      makeParams({
        sessionId: "sess-xyz",
        senderId: "sender-abc",
        prompt: "hi there",
        clientTools,
        provider: "anthropic",
        modelId: "claude-3-5",
        runId: "run-xyz",
      }),
    );

    expect(reasoningTurn).toHaveBeenCalledTimes(1);
    const call = reasoningTurn.mock.calls[0]!;
    const body = call[0];

    expect(body.context.session_id).toBe("sess-xyz");
    expect(body.context.contact_id).toBe("sender-abc");
    const lastMsg = body.messages[body.messages.length - 1]!;
    expect(lastMsg.content).toBe("hi there");
    expect(body.available_tools).toEqual(["tool_a", "tool_b"]);
    expect(body.model_override).toBe("anthropic/claude-3-5");
    expect(body.identity).toEqual({
      host_id: "host-test",
      plugin_version: "0.0.1",
    });
  });

  it("15. response with no tool calls — stopReason:'stop'", async () => {
    const { ctx, caps } = makeCtx({
      reasoningResponse: {
        status: "completed",
        message: { role: "user", content: "answered" },
        tool_calls: [],
      },
    });
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = await harness.runAttempt(makeParams());
    const assistant = res.lastAssistant as { stopReason: string } | undefined;
    expect(assistant?.stopReason).toBe("stop");
  });

  it("16. response with tool calls — stopReason:'toolUse'", async () => {
    const { ctx, caps } = makeCtx({
      reasoningResponse: {
        status: "completed",
        message: { role: "user", content: "" },
        tool_calls: [
          { id: "t-1", name: "search", arguments: {} },
        ],
      },
    });
    const harness = __agentHarness(ctx, caps, ctx.logger);
    const res = await harness.runAttempt(makeParams());
    const assistant = res.lastAssistant as { stopReason: string } | undefined;
    expect(assistant?.stopReason).toBe("toolUse");
  });
});

// ---------------------------------------------------------------------------
// Registration wiring — drive createColonyPlugin via a stubbed
// definePluginEntry module.
// ---------------------------------------------------------------------------

/**
 * Mock ``openclaw/plugin-sdk/plugin-entry`` so ``createColonyPlugin``
 * can be instantiated inside the test process. The real module pulls in
 * a large chunk of the OpenClaw runtime; we only need the
 * ``definePluginEntry`` helper to expose the plugin's ``register``
 * callback so we can invoke it with a fake ``OpenClawPluginApi`` and
 * observe which ``register*`` methods are called.
 */
vi.mock("openclaw/plugin-sdk/plugin-entry", () => ({
  definePluginEntry: (entry: { register: (api: unknown) => void }) => entry,
}));

type FakeApi = {
  pluginConfig: Record<string, unknown>;
  logger: {
    debug: ReturnType<typeof vi.fn>;
    info: ReturnType<typeof vi.fn>;
    warn: ReturnType<typeof vi.fn>;
    error: ReturnType<typeof vi.fn>;
  };
  registerAgentHarness: ReturnType<typeof vi.fn>;
  registerMemoryCapability: ReturnType<typeof vi.fn>;
  registerMemoryEmbeddingProvider: ReturnType<typeof vi.fn>;
  registerContextEngine: ReturnType<typeof vi.fn>;
  registerService: ReturnType<typeof vi.fn>;
  registerHook: ReturnType<typeof vi.fn>;
  on: ReturnType<typeof vi.fn>;
};

function makeFakeApi(pluginConfig: Record<string, unknown>): FakeApi {
  return {
    pluginConfig,
    logger: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
    registerAgentHarness: vi.fn(),
    registerMemoryCapability: vi.fn(),
    registerMemoryEmbeddingProvider: vi.fn(),
    registerContextEngine: vi.fn(),
    registerService: vi.fn(),
    registerHook: vi.fn(),
    on: vi.fn(),
  };
}

describe("createColonyPlugin — agent harness registration", () => {
  beforeEach(() => vi.clearAllMocks());

  it("17. registers the harness exactly once when ownReasoningLoop=true", async () => {
    const { createColonyPlugin } = await import("../src/plugin.js");
    const entry = (await createColonyPlugin()) as {
      register: (api: unknown) => void;
    };
    const api = makeFakeApi({
      apiKey: "sk-test",
      ownReasoningLoop: true,
    });
    entry.register(api);
    expect(api.registerAgentHarness).toHaveBeenCalledTimes(1);
  });

  it("18. does NOT register the harness when ownReasoningLoop=false", async () => {
    const { createColonyPlugin } = await import("../src/plugin.js");
    const entry = (await createColonyPlugin()) as {
      register: (api: unknown) => void;
    };
    const api = makeFakeApi({
      apiKey: "sk-test",
      ownReasoningLoop: false,
    });
    entry.register(api);
    expect(api.registerAgentHarness).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Sidecar-client signal propagation
// ---------------------------------------------------------------------------

describe("agentHarness — abort signal propagation", () => {
  beforeEach(() => vi.clearAllMocks());

  it("19. passes the abortSignal through to reasoningTurn opts", async () => {
    const { ctx, caps, reasoningTurn } = makeCtx();
    const harness = __agentHarness(ctx, caps, ctx.logger);

    const controller = new AbortController();
    // Fire mid-call: we abort after runAttempt kicks off the sidecar
    // call (the mock resolves immediately so the signal doesn't fire
    // until after the call is already in flight — but the harness is
    // required to have already passed the signal into reasoningTurn's
    // opts argument).
    const p = harness.runAttempt(
      makeParams({ abortSignal: controller.signal }),
    );
    controller.abort();
    await p;

    expect(reasoningTurn).toHaveBeenCalledTimes(1);
    const [, opts] = reasoningTurn.mock.calls[0]!;
    expect(opts).toBeDefined();
    expect(opts?.signal).toBe(controller.signal);
  });
});
