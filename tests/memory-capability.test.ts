import { describe, expect, it, vi } from "vitest";

import { __memoryCapability, __capabilityProbe } from "../src/plugin.js";
import type { ColonyPluginContext } from "../src/plugin.js";
import type { HostHealthResponse } from "../src/types.js";

/**
 * Tests for the ``promptBuilder`` returned by ``memoryCapability``.
 * The prompt builder is a synchronous function that tells the model
 * how Colony memory is injected and whether to include citations.
 */

function makeCtx(): ColonyPluginContext {
  return {
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
      health: vi.fn<[], Promise<HostHealthResponse>>().mockResolvedValue({
        status: "ok",
        api_version: "1",
        capabilities: [],
      }),
    } as unknown as ColonyPluginContext["client"],
    identity: () => ({ host_id: "host-test", plugin_version: "0.0.1" }),
  };
}

describe("promptBuilder", () => {
  it("returns non-empty section when availableTools is empty", () => {
    const ctx = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const sections = capability.promptBuilder!({
      availableTools: new Set<string>(),
    });
    expect(sections.length).toBeGreaterThan(0);
    expect(sections.some((s) => s.length > 0)).toBe(true);
  });

  it("omits citation hint when citationsMode === 'off'", () => {
    const ctx = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);
    const sections = capability.promptBuilder!({
      availableTools: new Set<string>(),
      citationsMode: "off",
    });
    const joined = sections.join("\n");
    expect(joined).not.toMatch(/citation/i);
  });

  it("includes citation hint when citationsMode !== 'off'", () => {
    const ctx = makeCtx();
    const caps = __capabilityProbe(ctx);
    const capability = __memoryCapability(ctx, caps);

    for (const mode of ["auto", "on", undefined]) {
      const sections = capability.promptBuilder!({
        availableTools: new Set<string>(),
        citationsMode: mode,
      });
      const joined = sections.join("\n");
      expect(joined).toMatch(/citation/i);
    }
  });
});
