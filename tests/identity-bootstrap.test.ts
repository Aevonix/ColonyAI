import { describe, expect, it, vi } from "vitest";

import { __buildContext } from "../src/plugin.js";

vi.mock("openclaw/plugin-sdk", () => ({
  delegateCompactionToRuntime: vi.fn(),
}));
vi.mock("openclaw/plugin-sdk/agent-harness", () => ({
  normalizeUsage: (u: unknown) => u,
}));

function makeApi(pluginConfig?: Record<string, unknown>) {
  return {
    pluginConfig: pluginConfig ?? {
      sidecarUrl: "http://stub",
      apiKey: "x",
      hostId: "host-test",
    },
    logger: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
  };
}

describe("buildContext — identity bootstrap", () => {
  it("returns a minimal HostIdentity before refreshIdentity runs", () => {
    const ctx = __buildContext(makeApi() as never);
    const id = ctx.identity();
    expect(id.host_id).toBe("host-test");
    expect(id.colony_id).toBeUndefined();
    expect(id.node_id).toBeUndefined();
    expect(id.trust_tier).toBeUndefined();
  });

  it("caches identity fields after refreshIdentity resolves", async () => {
    const ctx = __buildContext(makeApi() as never);
    vi.spyOn(ctx.client, "identityStatus").mockResolvedValue({
      colony_id: "col-99",
      node_id: "node-5",
      node_cert_fingerprint: "ab".repeat(16),
      trust_tier: "GENESIS",
    } as never);

    const snap = await ctx.refreshIdentity();
    expect(snap.colony_id).toBe("col-99");
    expect(snap.trust_tier).toBe("GENESIS");

    const id = ctx.identity();
    expect(id.colony_id).toBe("col-99");
    expect(id.node_id).toBe("node-5");
    expect(id.trust_tier).toBe("GENESIS");
    expect(id.node_cert_fingerprint).toBe("ab".repeat(16));
  });

  it("swallows identityStatus errors and returns the existing snapshot", async () => {
    const ctx = __buildContext(makeApi() as never);
    vi.spyOn(ctx.client, "identityStatus").mockRejectedValue(
      new Error("ECONNREFUSED"),
    );
    const snap = await ctx.refreshIdentity();
    expect(snap).toEqual({});
    // identity() still produces a valid minimal HostIdentity.
    expect(ctx.identity().host_id).toBe("host-test");
  });

  it("ignores null / missing fields from identityStatus", async () => {
    const ctx = __buildContext(makeApi() as never);
    vi.spyOn(ctx.client, "identityStatus").mockResolvedValue({
      colony_id: "col-1",
      node_id: null,
      node_cert_fingerprint: null,
      trust_tier: null,
    } as never);
    await ctx.refreshIdentity();
    const id = ctx.identity();
    expect(id.colony_id).toBe("col-1");
    expect(id.node_id).toBeUndefined();
    expect(id.trust_tier).toBeUndefined();
  });
});
