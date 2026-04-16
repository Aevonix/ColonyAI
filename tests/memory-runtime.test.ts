import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  __memoryCapability,
  __capabilityProbe,
  ColonyApiError,
} from "../src/plugin.js";
import type { ColonyPluginContext } from "../src/plugin.js";
import type {
  HostHealthResponse,
  MemoryReadResponse,
  MemorySearchResponse,
} from "../src/types.js";

/**
 * Tests for the ``runtime`` (MemoryPluginRuntime) and the
 * ``MemorySearchManager`` returned by ``memoryCapability``.
 */

function makeCtx(healthCaps: string[] = []) {
  const memorySearch = vi.fn<
    [unknown],
    Promise<MemorySearchResponse>
  >().mockResolvedValue({
    entries: [],
  });
  const memoryRead = vi.fn<
    [unknown],
    Promise<MemoryReadResponse>
  >().mockResolvedValue({
    entries: [],
  });
  const memoryFlush = vi.fn().mockResolvedValue({
    accepted: true,
    job_id: null,
  });
  const health = vi.fn<
    [],
    Promise<HostHealthResponse>
  >().mockResolvedValue({
    status: "ok",
    api_version: "1",
    capabilities: healthCaps,
  });

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
      memorySearch,
      memoryRead,
      memoryFlush,
      health,
    } as unknown as ColonyPluginContext["client"],
    identity: () => ({ host_id: "host-test", plugin_version: "0.0.1" }),
  };

  return { ctx, memorySearch, memoryRead, memoryFlush, health };
}

describe("resolveMemoryBackendConfig", () => {
  it("returns {backend: 'builtin'}", () => {
    const { ctx } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const config = capability.runtime!.resolveMemoryBackendConfig({
      cfg: {} as unknown,
      agentId: "agent-1",
    });
    expect(config).toEqual({ backend: "builtin" });
  });
});

describe("getMemorySearchManager", () => {
  it("returns a non-null manager", async () => {
    const { ctx } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });
    expect(manager).not.toBeNull();
  });
});

describe("MemorySearchManager.search", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("translates query + limit to snake_case request", async () => {
    const { ctx, memorySearch } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    memorySearch.mockResolvedValueOnce({ entries: [] });
    await manager!.search("hello world", { maxResults: 5 });

    expect(memorySearch).toHaveBeenCalledTimes(1);
    const body = memorySearch.mock.calls[0]![0] as Record<string, unknown>;
    expect(body).toMatchObject({
      query: "hello world",
      limit: 5,
    });
  });

  it("maps MemoryEntry[] to MemorySearchResult[] with path='memory://<id>'", async () => {
    const { ctx, memorySearch } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    memorySearch.mockResolvedValueOnce({
      entries: [
        {
          id: "mem-abc",
          content: "Some remembered content that is quite long for testing",
          score: 0.95,
        },
        {
          id: "mem-def",
          content: "Another memory",
          score: null,
        },
      ],
    });

    const results = await manager!.search("test query");
    expect(results).toHaveLength(2);
    expect(results[0]).toMatchObject({
      path: "memory://mem-abc",
      score: 0.95,
      source: "memory",
      citation: "mem:mem-abc",
    });
    expect(results[0]!.snippet).toBe(
      "Some remembered content that is quite long for testing",
    );
    expect(results[1]).toMatchObject({
      path: "memory://mem-def",
      score: 0,
      source: "memory",
      citation: "mem:mem-def",
    });
  });

  it("returns [] on 501 phase1_wiring_required", async () => {
    const { ctx, memorySearch } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    memorySearch.mockRejectedValueOnce(
      new ColonyApiError(501, "phase1_wiring_required", "not wired"),
    );

    const results = await manager!.search("test");
    expect(results).toEqual([]);
  });

  it("returns [] on network error", async () => {
    const { ctx, memorySearch } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    memorySearch.mockRejectedValueOnce(new Error("ECONNREFUSED"));

    const results = await manager!.search("test");
    expect(results).toEqual([]);
  });

  it("rethrows 4xx bad_request", async () => {
    const { ctx, memorySearch } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    const err = new ColonyApiError(400, "bad_request", "missing field");
    memorySearch.mockRejectedValueOnce(err);

    await expect(manager!.search("test")).rejects.toBe(err);
  });
});

describe("MemorySearchManager.readFile", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('parses "memory://<id>" prefix and calls memoryRead', async () => {
    const { ctx, memoryRead } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    memoryRead.mockResolvedValueOnce({
      entries: [{ id: "mem-xyz", content: "recalled content" }],
    });

    const result = await manager!.readFile({ relPath: "memory://mem-xyz" });
    expect(result).toEqual({ text: "recalled content", path: "memory://mem-xyz" });

    const body = memoryRead.mock.calls[0]![0] as Record<string, unknown>;
    expect(body).toMatchObject({ memory_id: "mem-xyz" });
  });

  it("returns {text: '', path} on 501/5xx", async () => {
    const { ctx, memoryRead } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    memoryRead.mockRejectedValueOnce(
      new ColonyApiError(501, "phase1_wiring_required", "not wired"),
    );

    const result = await manager!.readFile({ relPath: "memory://mem-xyz" });
    expect(result).toEqual({ text: "", path: "memory://mem-xyz" });
  });
});

describe("MemorySearchManager.status", () => {
  it("is synchronous", () => {
    const { ctx } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);

    // getMemorySearchManager is async, but we can verify status() itself
    // is sync by checking the return type is not a promise.
    const managerPromise = capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    managerPromise.then(({ manager }) => {
      const status = manager!.status();
      // If status() returned a promise, this would be a Promise object
      // rather than a plain object with 'backend'.
      expect(status.backend).toBe("builtin");
      expect(status.provider).toBe("colony");
    });

    // Also do the same synchronously by awaiting first
    return managerPromise.then(({ manager }) => {
      const status = manager!.status();
      // Verify it's NOT a promise (i.e., it's synchronous)
      expect(status).not.toBeInstanceOf(Promise);
      expect(status.backend).toBe("builtin");
      expect(status.provider).toBe("colony");
      expect(status.sources).toEqual(["memory"]);
      expect(status.cache).toEqual({ enabled: false });
      expect(status.fts).toEqual({ enabled: false, available: false });
      expect(status.vector).toEqual({ enabled: false, available: false });
    });
  });
});

describe("MemorySearchManager.sync", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("calls memoryFlush and passes reason", async () => {
    const { ctx, memoryFlush } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    const progress = vi.fn();
    await manager!.sync!({ reason: "user-requested", progress });

    expect(memoryFlush).toHaveBeenCalledTimes(1);
    const body = memoryFlush.mock.calls[0]![0] as Record<string, unknown>;
    expect(body).toMatchObject({ reason: "user-requested" });
    expect(progress).toHaveBeenCalledWith({
      completed: 1,
      total: 1,
      label: "colony.memory.flush",
    });
  });

  it("resolves cleanly on 501", async () => {
    const { ctx, memoryFlush } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    memoryFlush.mockRejectedValueOnce(
      new ColonyApiError(501, "phase1_wiring_required", "not wired"),
    );

    // Should not throw
    await expect(manager!.sync!({ reason: "test" })).resolves.toBeUndefined();
  });
});

describe("MemorySearchManager.probeEmbeddingAvailability", () => {
  it('returns {ok:true} when caps has "embed"', async () => {
    const { ctx } = makeCtx(["embed", "memory"]);
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    const result = await manager!.probeEmbeddingAvailability();
    expect(result).toEqual({ ok: true });
  });

  it("returns {ok:false} when probe failed", async () => {
    const { ctx } = makeCtx([]);
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    const result = await manager!.probeEmbeddingAvailability();
    expect(result.ok).toBe(false);
    expect(result.error).toBeDefined();
  });
});

describe("MemorySearchManager.probeVectorAvailability", () => {
  it("always returns false", async () => {
    const { ctx } = makeCtx(["embed"]);
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const { manager } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });

    const result = await manager!.probeVectorAvailability();
    expect(result).toBe(false);
  });
});

describe("closeAllMemorySearchManagers", () => {
  it("clears cache", async () => {
    const { ctx } = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);

    // Create a manager
    const { manager: m1 } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });
    expect(m1).not.toBeNull();

    // Close all managers
    await capability.runtime!.closeAllMemorySearchManagers!();

    // A new getMemorySearchManager call should create a fresh instance
    const { manager: m2 } = await capability.runtime!.getMemorySearchManager({
      cfg: {} as unknown,
      agentId: "agent-1",
    });
    expect(m2).not.toBeNull();
    // They should be different instances since the cache was cleared
    expect(m2).not.toBe(m1);
  });
});
