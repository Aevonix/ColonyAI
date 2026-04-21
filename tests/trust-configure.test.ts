import { describe, expect, it, vi } from "vitest";

import { withHostLLMEnvOverrides } from "../src/config.js";
import { __buildContext } from "../src/plugin.js";
import { buildVerifyAuthorityTool } from "../src/tool-registrar.js";

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

describe("withHostLLMEnvOverrides", () => {
  it("returns the config unchanged when hostLLM is absent", () => {
    const cfg = {
      sidecarUrl: "http://stub",
      apiKey: "x",
      hostId: "h",
      ownReasoningLoop: false,
      ownMemoryCapability: false,
      ownContextEngine: false,
      forwardProactiveDeliveries: true,
      failSafetyClosed: true,
      requestTimeoutMs: 10_000,
    };
    const out = withHostLLMEnvOverrides(cfg as never, {});
    expect(out.hostLLM).toBeUndefined();
  });

  it("fills apiKey and baseUrl from env when not set", () => {
    const cfg = {
      sidecarUrl: "http://stub",
      apiKey: "x",
      hostId: "h",
      ownReasoningLoop: false,
      ownMemoryCapability: false,
      ownContextEngine: false,
      forwardProactiveDeliveries: true,
      failSafetyClosed: true,
      requestTimeoutMs: 10_000,
      hostLLM: { provider: "openai" },
    };
    const out = withHostLLMEnvOverrides(cfg as never, {
      COLONY_HOST_LLM_API_KEY: "sk-xxx",
      COLONY_HOST_LLM_BASE_URL: "https://api.openai.com/v1",
    } as never);
    expect(out.hostLLM?.apiKey).toBe("sk-xxx");
    expect(out.hostLLM?.baseUrl).toBe("https://api.openai.com/v1");
  });

  it("does NOT overwrite an explicit apiKey with env", () => {
    const cfg = {
      sidecarUrl: "http://stub",
      apiKey: "x",
      hostId: "h",
      ownReasoningLoop: false,
      ownMemoryCapability: false,
      ownContextEngine: false,
      forwardProactiveDeliveries: true,
      failSafetyClosed: true,
      requestTimeoutMs: 10_000,
      hostLLM: { provider: "openai", apiKey: "explicit" },
    };
    const out = withHostLLMEnvOverrides(cfg as never, {
      COLONY_HOST_LLM_API_KEY: "sk-from-env",
    } as never);
    expect(out.hostLLM?.apiKey).toBe("explicit");
  });
});

describe("buildContext.verifyChain", () => {
  it("caches chain fields on the snapshot when valid", async () => {
    const ctx = __buildContext(makeApi() as never);
    vi.spyOn(ctx.client, "chainVerify").mockResolvedValue({
      valid: true,
      colony_id: "col-42",
      signed_attestation: "ab".repeat(32),
      signer_public_key: "cd".repeat(32),
      attested_at: "2026-04-21T00:00:00Z",
    } as never);

    const snap = await ctx.verifyChain("hello");
    expect(snap.chain_valid).toBe(true);
    expect(snap.signed_attestation).toBe("ab".repeat(32));
    expect(snap.signer_public_key).toBe("cd".repeat(32));
    expect(snap.attested_at).toBe("2026-04-21T00:00:00Z");
  });

  it("swallows chainVerify errors and returns existing snapshot", async () => {
    const ctx = __buildContext(makeApi() as never);
    vi.spyOn(ctx.client, "chainVerify").mockRejectedValue(new Error("fail"));
    const snap = await ctx.verifyChain();
    expect(snap.chain_valid).toBeUndefined();
    expect(snap.signed_attestation).toBeUndefined();
  });
});

describe("buildVerifyAuthorityTool", () => {
  it("forwards to client.chainVerify and returns the full response body", async () => {
    const ctx = __buildContext(makeApi() as never);
    vi.spyOn(ctx.client, "chainVerify").mockResolvedValue({
      valid: true,
      colony_id: "col-42",
      signed_attestation: "aa".repeat(32),
      signer_public_key: "bb".repeat(32),
      attested_at: "2026-04-21T00:00:00Z",
    } as never);

    const tool = buildVerifyAuthorityTool(ctx);
    expect(tool.name).toBe("colony_verify_authority");
    const res = await tool.execute("tc-1", { data: "I speak for Colony" });
    const parsed = JSON.parse(res.content[0]!.text);
    expect(parsed.valid).toBe(true);
    expect(parsed.signed_attestation).toBe("aa".repeat(32));
    expect(ctx.client.chainVerify).toHaveBeenCalledWith(
      "I speak for Colony",
      expect.objectContaining({ host_id: "host-test" }),
    );
  });

  it("returns valid=false on exception", async () => {
    const ctx = __buildContext(makeApi() as never);
    vi.spyOn(ctx.client, "chainVerify").mockRejectedValue(
      new Error("sidecar down"),
    );
    const tool = buildVerifyAuthorityTool(ctx);
    const res = await tool.execute("tc-1", { data: "x" });
    const parsed = JSON.parse(res.content[0]!.text);
    expect(parsed.valid).toBe(false);
    expect(String(parsed.error)).toContain("sidecar down");
  });
});
