import { describe, expect, it, vi } from "vitest";

import {
  buildNativeTools,
  buildMemoryWriteTool,
  fetchActiveSkillTools,
  registerColonyTools,
} from "../src/tool-registrar.js";
import type { ColonyPluginContext } from "../src/plugin.js";

vi.mock("openclaw/plugin-sdk", () => ({
  delegateCompactionToRuntime: vi.fn(),
}));
vi.mock("openclaw/plugin-sdk/agent-harness", () => ({
  normalizeUsage: (u: unknown) => u,
}));

function makeCtx(overrides?: Partial<ColonyPluginContext>) {
  const client = {
    toolsInvoke: vi.fn().mockResolvedValue({ result: "", available: true }),
    executeSkill: vi.fn().mockResolvedValue({ status: "success" }),
    listSkills: vi.fn().mockResolvedValue({ skills: [] }),
  };
  return {
    config: { sidecarUrl: "http://stub" } as never,
    client: client as never,
    identity: () => ({ host_id: "h" }),
    refreshIdentity: async () => ({}),
    cache: { invalidate: vi.fn(), subscribe: vi.fn() },
    logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    ...overrides,
  } as ColonyPluginContext;
}

describe("buildNativeTools", () => {
  it("produces tools for calculate, web_search, read/write/list", () => {
    const tools = buildNativeTools(makeCtx());
    const names = tools.map((t) => t.name);
    expect(names).toEqual([
      "colony_calculate",
      "colony_web_search",
      "colony_read_file",
      "colony_write_file",
      "colony_list_directory",
    ]);
  });

  it("forwards the tool name and args to toolsInvoke", async () => {
    const ctx = makeCtx();
    const tools = buildNativeTools(ctx);
    const calc = tools.find((t) => t.name === "colony_calculate")!;
    (ctx.client.toolsInvoke as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      result: "42",
      available: true,
    });

    const res = await calc.execute("tc-1", { expression: "6*7" });
    expect(res.content[0]!.text).toBe("42");
    expect(ctx.client.toolsInvoke).toHaveBeenCalledWith(
      "calculate",
      { expression: "6*7" },
      expect.objectContaining({ host_id: "h" }),
    );
  });

  it("surfaces unavailable flag as JSON error", async () => {
    const ctx = makeCtx();
    (ctx.client.toolsInvoke as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      result: "",
      available: false,
      error: "tool_executor_not_initialized",
    });
    const calc = buildNativeTools(ctx).find((t) => t.name === "colony_calculate")!;
    const res = await calc.execute("tc-1", { expression: "1+1" });
    const parsed = JSON.parse(res.content[0]!.text);
    expect(parsed.error).toBe("tool_executor_not_initialized");
  });
});

describe("buildMemoryWriteTool", () => {
  it("calls memoryManager.write with the agent-supplied args", async () => {
    const ctx = makeCtx();
    const manager = {
      write: vi.fn().mockResolvedValue({ id: "mem-1", accepted: true }),
    };
    const tool = buildMemoryWriteTool(ctx, manager);
    const res = await tool.execute("tc-1", {
      content: "Alice prefers async comms",
      kind: "preference",
    });
    expect(manager.write).toHaveBeenCalledWith({
      content: "Alice prefers async comms",
      kind: "preference",
      personId: undefined,
      entities: undefined,
      tags: undefined,
    });
    const parsed = JSON.parse(res.content[0]!.text);
    expect(parsed.accepted).toBe(true);
    expect(parsed.id).toBe("mem-1");
  });

  it("returns accepted=false on write exception", async () => {
    const ctx = makeCtx();
    const manager = {
      write: vi.fn().mockRejectedValue(new Error("graph offline")),
    };
    const tool = buildMemoryWriteTool(ctx, manager);
    const res = await tool.execute("tc-1", { content: "x" });
    const parsed = JSON.parse(res.content[0]!.text);
    expect(parsed.accepted).toBe(false);
    expect(String(parsed.error)).toContain("graph offline");
  });
});

describe("fetchActiveSkillTools", () => {
  it("builds a tool per active skill", async () => {
    const ctx = makeCtx();
    (ctx.client.listSkills as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      skills: [
        { id: "skill-1", name: "Summarize", description: "Summarize text" },
        { skill_id: "skill-2", name: "Translate" },
      ],
    });
    const tools = await fetchActiveSkillTools(ctx);
    expect(tools.map((t) => t.name)).toEqual([
      "colony_skill_skill_1",
      "colony_skill_skill_2",
    ]);
  });

  it("executes each skill through the executeSkill endpoint", async () => {
    const ctx = makeCtx();
    (ctx.client.listSkills as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      skills: [{ id: "skill-1", name: "Summarize" }],
    });
    (ctx.client.executeSkill as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      status: "success",
      output: { summary: "short" },
    });
    const tools = await fetchActiveSkillTools(ctx);
    const res = await tools[0]!.execute("tc-1", { text: "long text" });
    expect(ctx.client.executeSkill).toHaveBeenCalledWith(
      "skill-1",
      { text: "long text" },
      expect.objectContaining({ host_id: "h" }),
    );
    const parsed = JSON.parse(res.content[0]!.text);
    expect(parsed.status).toBe("success");
  });

  it("returns [] and logs when listSkills rejects", async () => {
    const ctx = makeCtx();
    (ctx.client.listSkills as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("offline"),
    );
    const tools = await fetchActiveSkillTools(ctx);
    expect(tools).toEqual([]);
    expect(ctx.logger?.warn).toHaveBeenCalled();
  });
});

describe("registerColonyTools", () => {
  it("registers each native tool + memory_write + dynamic skills", async () => {
    const ctx = makeCtx();
    (ctx.client.listSkills as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      skills: [{ id: "skill-1", name: "Noop" }],
    });
    const registered: string[] = [];
    const api = {
      registerTool: vi.fn((_factory, opts: { name: string }) => {
        registered.push(opts.name);
      }),
    } as never;

    const handle = registerColonyTools(ctx, api, {
      write: async () => ({ id: "x", accepted: true }),
    });

    // Let the fire-and-forget refreshSkillTools resolve.
    await new Promise((r) => setImmediate(r));

    expect(registered).toContain("colony_calculate");
    expect(registered).toContain("colony_web_search");
    expect(registered).toContain("colony_memory_write");
    expect(registered).toContain("colony_skill_skill_1");
    expect(handle.skillToolCount()).toBe(1);
  });

  it("refreshSkillTools only registers new skills", async () => {
    const ctx = makeCtx();
    (ctx.client.listSkills as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ skills: [{ id: "a", name: "A" }] })
      .mockResolvedValueOnce({
        skills: [
          { id: "a", name: "A" },
          { id: "b", name: "B" },
        ],
      });
    const registered: string[] = [];
    const api = {
      registerTool: vi.fn((_f, opts: { name: string }) => {
        registered.push(opts.name);
      }),
    } as never;

    const handle = registerColonyTools(ctx, api, null);
    await new Promise((r) => setImmediate(r));
    expect(registered.filter((n) => n.startsWith("colony_skill_"))).toEqual([
      "colony_skill_a",
    ]);

    await handle.refreshSkillTools();
    expect(registered.filter((n) => n.startsWith("colony_skill_"))).toEqual([
      "colony_skill_a",
      "colony_skill_b",
    ]);
    expect(handle.skillToolCount()).toBe(2);
  });
});
