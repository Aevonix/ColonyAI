import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  __memoryEmbeddingProvider,
  ColonyApiError,
  COLONY_EMBED_BATCH_CHUNK,
} from "../src/plugin.js";
import type {
  ColonyPluginContext,
  MemoryEmbeddingProviderCreateOptions,
} from "../src/plugin.js";
import type {
  HostHealthResponse,
  MemoryEmbedResponse,
} from "../src/types.js";

/**
 * Tests for the ``MemoryEmbeddingProviderAdapter`` returned by
 * ``memoryEmbeddingProvider``.
 *
 * The adapter has two distinct failure domains that must stay separate:
 *
 *   1. **Setup failures** (``create()`` time): signalled by returning
 *      ``{ provider: null }``. OpenClaw treats this as "this adapter
 *      has nothing to contribute, try the next one".
 *   2. **Runtime failures** (``embedQuery`` / ``embedBatch``): MUST be
 *      thrown, never swallowed, so OpenClaw's retry / fallback layer
 *      gets to decide. Returning zero-vectors would silently corrupt
 *      downstream vector similarity search — the tests below guard
 *      against that regression.
 */

type EmbedCall = (body: {
  identity: { host_id: string; plugin_version: string };
  inputs: string[];
  model?: string;
}) => Promise<MemoryEmbedResponse>;

type HealthCall = () => Promise<HostHealthResponse>;

function makeCtx(overrides?: {
  healthCapabilities?: string[];
  healthFn?: HealthCall;
  embedFn?: EmbedCall;
  sidecarUrl?: string;
}) {
  const memoryEmbed = vi.fn<[Parameters<EmbedCall>[0]], Promise<MemoryEmbedResponse>>();
  if (overrides?.embedFn) {
    memoryEmbed.mockImplementation(overrides.embedFn);
  } else {
    memoryEmbed.mockResolvedValue({ model: "default", vectors: [[0.1, 0.2]] });
  }

  const health = vi.fn<[], Promise<HostHealthResponse>>();
  if (overrides?.healthFn) {
    health.mockImplementation(overrides.healthFn);
  } else {
    health.mockResolvedValue({
      status: "ok",
      api_version: "1",
      capabilities: overrides?.healthCapabilities ?? ["embed"],
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
      sidecarUrl: overrides?.sidecarUrl ?? "http://stub",
      apiKey: "x",
      hostId: "host-test",
      requestTimeoutMs: 2_000,
      ownReasoningLoop: false,
      ownMemoryCapability: false,
      forwardProactiveDeliveries: true,
    } as unknown as ColonyPluginContext["config"],
    client: {
      memoryEmbed,
      health,
    } as unknown as ColonyPluginContext["client"],
    identity: () => ({ host_id: "host-test", plugin_version: "0.0.1" }),
    logger,
  };

  return { ctx, memoryEmbed, health, logger };
}

/** Minimal ``MemoryEmbeddingProviderCreateOptions`` that satisfies the SDK shape. */
function makeCreateOptions(
  model = "text-embedding",
): MemoryEmbeddingProviderCreateOptions {
  return {
    config: {} as MemoryEmbeddingProviderCreateOptions["config"],
    model,
  };
}

describe("memoryEmbeddingProvider — adapter shape", () => {
  it("exposes the expected top-level fields", () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    expect(adapter.id).toBe("colony-embed");
    expect(adapter.transport).toBe("remote");
    expect(adapter.defaultModel).toBe("default");
    expect(typeof adapter.create).toBe("function");
    expect(typeof adapter.formatSetupError).toBe("function");
    expect(typeof adapter.shouldContinueAutoSelection).toBe("function");
  });
});

describe("memoryEmbeddingProvider.create", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns a non-null provider when sidecar advertises 'embed'", async () => {
    const { ctx } = makeCtx({ healthCapabilities: ["embed"] });
    const adapter = __memoryEmbeddingProvider(ctx);
    const result = await adapter.create(makeCreateOptions());
    expect(result.provider).not.toBeNull();
    expect(result.provider?.id).toBe("colony-embed");
  });

  it("returns { provider: null } when sidecar does not advertise 'embed'", async () => {
    const { ctx, logger } = makeCtx({ healthCapabilities: ["memory"] });
    const adapter = __memoryEmbeddingProvider(ctx);
    const result = await adapter.create(makeCreateOptions());
    expect(result.provider).toBeNull();
    expect(result.runtime).toBeUndefined();
    expect(logger.info).toHaveBeenCalledWith(
      expect.stringContaining("did not advertise 'embed'"),
    );
  });

  it("returns { provider: null } when health probe throws a network error", async () => {
    const { ctx, logger } = makeCtx({
      healthFn: async () => {
        throw new Error("ECONNREFUSED 127.0.0.1:7777");
      },
    });
    const adapter = __memoryEmbeddingProvider(ctx);
    const result = await adapter.create(makeCreateOptions());
    expect(result.provider).toBeNull();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.stringContaining("health probe failed"),
    );
  });

  it("returns { provider: null } on 501 phase1_wiring_required from health", async () => {
    const { ctx, logger } = makeCtx({
      healthFn: async () => {
        throw new ColonyApiError(501, "phase1_wiring_required", "not wired");
      },
    });
    const adapter = __memoryEmbeddingProvider(ctx);
    const result = await adapter.create(makeCreateOptions());
    expect(result.provider).toBeNull();
    expect(logger.info).toHaveBeenCalledWith(
      expect.stringContaining("phase1_wiring_required"),
    );
  });

  it("provider.model reflects options.model", async () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const result = await adapter.create(makeCreateOptions("colony-bge-m3"));
    expect(result.provider?.model).toBe("colony-bge-m3");
  });

  it("passes through runtime.cacheKeyData with provider/sidecarUrl/model", async () => {
    const { ctx } = makeCtx({ sidecarUrl: "http://sidecar.local:7777" });
    const adapter = __memoryEmbeddingProvider(ctx);
    const result = await adapter.create(makeCreateOptions("colony-bge-m3"));
    expect(result.runtime).toBeDefined();
    expect(result.runtime?.id).toBe("colony-embed");
    expect(result.runtime?.cacheKeyData).toEqual({
      provider: "colony",
      sidecarUrl: "http://sidecar.local:7777",
      model: "colony-bge-m3",
    });
  });
});

describe("provider.embedQuery", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns the first vector on the happy path", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    memoryEmbed.mockResolvedValueOnce({
      model: "colony-bge-m3",
      vectors: [[0.11, 0.22, 0.33]],
    });

    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());
    const vec = await provider!.embedQuery("hello world");
    expect(vec).toEqual([0.11, 0.22, 0.33]);
  });

  it("forwards the requested model in the sidecar request", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions("colony-bge-m3"));
    await provider!.embedQuery("hello");
    expect(memoryEmbed).toHaveBeenCalledTimes(1);
    const body = memoryEmbed.mock.calls[0]![0];
    expect(body.model).toBe("colony-bge-m3");
    expect(body.inputs).toEqual(["hello"]);
  });

  it("propagates 503 errors (does NOT swallow)", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());

    const err = new ColonyApiError(503, "sidecar_overloaded", "try again");
    memoryEmbed.mockRejectedValueOnce(err);

    await expect(provider!.embedQuery("hello")).rejects.toBe(err);
  });

  it("propagates 501 errors surfaced after successful create", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());

    const err = new ColonyApiError(501, "embed_not_wired", "not wired");
    memoryEmbed.mockRejectedValueOnce(err);

    await expect(provider!.embedQuery("hello")).rejects.toBe(err);
  });

  it("propagates network errors", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());

    const err = new Error("ETIMEDOUT");
    memoryEmbed.mockRejectedValueOnce(err);

    await expect(provider!.embedQuery("hello")).rejects.toBe(err);
  });
});

describe("provider.embedBatch", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns all vectors on the happy path", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    memoryEmbed.mockResolvedValueOnce({
      model: "colony-bge-m3",
      vectors: [
        [0.1, 0.2],
        [0.3, 0.4],
        [0.5, 0.6],
      ],
    });

    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());
    const vecs = await provider!.embedBatch(["a", "b", "c"]);
    expect(vecs).toEqual([
      [0.1, 0.2],
      [0.3, 0.4],
      [0.5, 0.6],
    ]);
    expect(memoryEmbed).toHaveBeenCalledTimes(1);
  });

  it(`sends a single call for exactly ${COLONY_EMBED_BATCH_CHUNK} inputs`, async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const texts = Array.from({ length: COLONY_EMBED_BATCH_CHUNK }, (_, i) => `t${i}`);
    const vectors = texts.map((_, i) => [i, i + 0.5]);
    memoryEmbed.mockResolvedValueOnce({ model: "default", vectors });

    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());
    const result = await provider!.embedBatch(texts);

    expect(memoryEmbed).toHaveBeenCalledTimes(1);
    expect(result).toHaveLength(COLONY_EMBED_BATCH_CHUNK);
    expect(result).toEqual(vectors);
  });

  it(`chunks inputs > ${COLONY_EMBED_BATCH_CHUNK} into multiple calls`, async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const total = COLONY_EMBED_BATCH_CHUNK + 10;
    const texts = Array.from({ length: total }, (_, i) => `t${i}`);

    // Return one vector per input per chunk.
    memoryEmbed.mockImplementation(async (body) => ({
      model: "default",
      vectors: body.inputs.map((_, i) => [i, i]),
    }));

    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());
    const result = await provider!.embedBatch(texts);

    expect(memoryEmbed).toHaveBeenCalledTimes(2);
    // First call gets CHUNK items; second call gets the tail.
    expect(memoryEmbed.mock.calls[0]![0].inputs).toHaveLength(
      COLONY_EMBED_BATCH_CHUNK,
    );
    expect(memoryEmbed.mock.calls[1]![0].inputs).toHaveLength(
      total - COLONY_EMBED_BATCH_CHUNK,
    );
    expect(result).toHaveLength(total);
  });

  it("returns [] for empty input without calling the sidecar", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());
    const result = await provider!.embedBatch([]);
    expect(result).toEqual([]);
    expect(memoryEmbed).not.toHaveBeenCalled();
  });

  it("propagates 503 errors (does NOT swallow)", async () => {
    const { ctx, memoryEmbed } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const { provider } = await adapter.create(makeCreateOptions());

    const err = new ColonyApiError(503, "sidecar_overloaded", "try again");
    memoryEmbed.mockRejectedValueOnce(err);

    await expect(provider!.embedBatch(["a", "b"])).rejects.toBe(err);
  });
});

describe("adapter.formatSetupError", () => {
  it("includes status + code for ColonyApiError", () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const formatted = adapter.formatSetupError!(
      new ColonyApiError(503, "sidecar_overloaded", "queue full"),
    );
    expect(formatted).toContain("503");
    expect(formatted).toContain("sidecar_overloaded");
    expect(formatted).toContain("queue full");
  });

  it("includes message for a generic Error", () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    const formatted = adapter.formatSetupError!(new Error("ECONNREFUSED"));
    expect(formatted).toContain("ECONNREFUSED");
  });
});

describe("adapter.shouldContinueAutoSelection", () => {
  it("returns true for 501 ColonyApiError", () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    expect(
      adapter.shouldContinueAutoSelection!(
        new ColonyApiError(501, "phase1_wiring_required", "not wired"),
      ),
    ).toBe(true);
  });

  it("returns true for embed_not_wired code", () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    // Note: embed_not_wired typically travels with 400 or 501 in the
    // sidecar; the adapter should flag it regardless of status since the
    // *code* is the authoritative "this endpoint doesn't exist here"
    // signal.
    expect(
      adapter.shouldContinueAutoSelection!(
        new ColonyApiError(400, "embed_not_wired", "endpoint disabled"),
      ),
    ).toBe(true);
  });

  it("returns false for 503 (real failure, not a no-op)", () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    expect(
      adapter.shouldContinueAutoSelection!(
        new ColonyApiError(503, "sidecar_overloaded", "try again"),
      ),
    ).toBe(false);
  });

  it("returns true for network errors", () => {
    const { ctx } = makeCtx();
    const adapter = __memoryEmbeddingProvider(ctx);
    expect(
      adapter.shouldContinueAutoSelection!(new Error("ECONNREFUSED")),
    ).toBe(true);
  });
});
