/**
 * Tool registrar — exposes Colony's sidecar-resident tools to OpenClaw.
 *
 * Registers:
 *  - Native tools (calculate, web_search, read_file, write_file,
 *    list_directory) — POST to /v1/host/reasoning/tools/invoke.
 *  - Dynamic skills (each ACTIVE SkillManifest) — POST to
 *    /v1/host/skills/{id}/execute.
 *  - colony_memory_write — direct handle into
 *    ColonyMemorySearchManager.write so agent turns can persist
 *    learning.
 *
 * The factory passed to ``api.registerTool`` runs at tool-discovery
 * time, not at plugin registration. That keeps credentials / identity
 * fresh on every turn.
 */

import { Type, type Static, type TSchema } from "@sinclair/typebox";

import type { ColonyPluginContext } from "./plugin.js";
import type { OpenClawPluginApi } from "./plugin.js";

type JsonObject = Record<string, unknown>;

type AnyTool = {
  name: string;
  label: string;
  description: string;
  parameters: TSchema;
  execute: (
    toolCallId: string,
    params: JsonObject,
  ) => Promise<{ content: Array<{ type: "text"; text: string }>; details: unknown }>;
};

function textResult(text: string, details: unknown = {}) {
  return {
    content: [{ type: "text" as const, text }],
    details,
  };
}

// ---------------------------------------------------------------------------
// Native tool parameter schemas
// ---------------------------------------------------------------------------

const CalculateSchema = Type.Object({
  expression: Type.String({ description: "Arithmetic expression to evaluate." }),
});

const WebSearchSchema = Type.Object({
  query: Type.String({ description: "Search query string." }),
  max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
});

const ReadFileSchema = Type.Object({
  path: Type.String({ description: "Sandbox-relative file path." }),
});

const WriteFileSchema = Type.Object({
  path: Type.String({ description: "Sandbox-relative file path." }),
  content: Type.String({ description: "Contents to write." }),
});

const ListDirectorySchema = Type.Object({
  path: Type.Optional(Type.String({ description: "Sandbox-relative directory (defaults to root)." })),
});

const MemoryWriteSchema = Type.Object({
  content: Type.String({ description: "The memory text to persist." }),
  kind: Type.Optional(Type.String({ description: "Memory category (e.g. 'preference', 'fact')." })),
  person_id: Type.Optional(Type.String()),
  entities: Type.Optional(Type.Array(Type.String())),
  tags: Type.Optional(Type.Array(Type.String())),
});

const VerifyAuthoritySchema = Type.Object({
  data: Type.String({
    description:
      "Arbitrary payload to bind to the attestation (e.g. a claim or statement).",
  }),
});

// ---------------------------------------------------------------------------
// Native tool factories
// ---------------------------------------------------------------------------

function nativeTool<TParams extends TSchema>(
  name: string,
  label: string,
  description: string,
  parameters: TParams,
  ctx: ColonyPluginContext,
): AnyTool {
  return {
    name: `colony_${name}`,
    label,
    description,
    parameters,
    async execute(_toolCallId, params) {
      try {
        const res = await ctx.client.toolsInvoke(
          name,
          params as JsonObject,
          ctx.identity(),
        );
        if (!res.available) {
          return textResult(
            JSON.stringify({ error: res.error ?? `tool '${name}' unavailable` }),
          );
        }
        if (res.error) {
          return textResult(JSON.stringify({ error: res.error }));
        }
        return textResult(res.result ?? "", { source: "colony.native", name });
      } catch (err) {
        return textResult(
          JSON.stringify({ error: String(err) }),
          { source: "colony.native", name },
        );
      }
    },
  };
}

export function buildNativeTools(ctx: ColonyPluginContext): AnyTool[] {
  return [
    nativeTool("calculate", "Colony: calculate",
      "Evaluate an arithmetic expression server-side via Colony.",
      CalculateSchema, ctx),
    nativeTool("web_search", "Colony: web search",
      "Search the web via Colony's configured search provider (DuckDuckGo fallback).",
      WebSearchSchema, ctx),
    nativeTool("read_file", "Colony: read sandbox file",
      "Read a file from Colony's sandbox directory.",
      ReadFileSchema, ctx),
    nativeTool("write_file", "Colony: write sandbox file",
      "Write a file to Colony's sandbox directory.",
      WriteFileSchema, ctx),
    nativeTool("list_directory", "Colony: list sandbox dir",
      "List entries in Colony's sandbox directory.",
      ListDirectorySchema, ctx),
  ];
}

// ---------------------------------------------------------------------------
// Memory-write tool (wraps the ColonyMemorySearchManager directly)
// ---------------------------------------------------------------------------

export function buildMemoryWriteTool(
  ctx: ColonyPluginContext,
  memoryManager: {
    write(params: {
      content: string;
      kind?: string;
      personId?: string;
      entities?: string[];
      tags?: string[];
    }): Promise<{ id?: string; accepted: boolean }>;
  },
): AnyTool {
  return {
    name: "colony_memory_write",
    label: "Colony: remember",
    description:
      "Persist a new memory to Colony's graph so it's available on future turns.",
    parameters: MemoryWriteSchema,
    async execute(_toolCallId, params) {
      const p = params as Static<typeof MemoryWriteSchema>;
      try {
        const res = await memoryManager.write({
          content: p.content,
          kind: p.kind,
          personId: p.person_id,
          entities: p.entities,
          tags: p.tags,
        });
        return textResult(
          JSON.stringify({ accepted: res.accepted, id: res.id }),
          res,
        );
      } catch (err) {
        return textResult(
          JSON.stringify({ accepted: false, error: String(err) }),
        );
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Authority-assertion tool — agent can request a signed attestation
// ---------------------------------------------------------------------------

export function buildVerifyAuthorityTool(ctx: ColonyPluginContext): AnyTool {
  return {
    name: "colony_verify_authority",
    label: "Colony: verify authority",
    description:
      "Request a signed attestation from Colony's sidecar binding the given data to the colony's public key. Returns {valid, colony_id, signed_attestation, signer_public_key}.",
    parameters: VerifyAuthoritySchema,
    async execute(_toolCallId, params) {
      const p = params as Static<typeof VerifyAuthoritySchema>;
      try {
        const res = await ctx.client.chainVerify(p.data, ctx.identity());
        return textResult(JSON.stringify(res), res);
      } catch (err) {
        return textResult(
          JSON.stringify({ valid: false, error: String(err) }),
        );
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Skill-as-tool factory
// ---------------------------------------------------------------------------

type ActiveSkill = {
  id?: string;
  skill_id?: string;
  name?: string;
  description?: string | null;
  input_schema?: TSchema | null;
};

function buildSkillTool(
  ctx: ColonyPluginContext,
  skill: ActiveSkill,
): AnyTool | null {
  const skillId = skill.id ?? skill.skill_id;
  if (!skillId) return null;
  const parameters = (skill.input_schema as TSchema | undefined) ?? Type.Object({});
  return {
    name: `colony_skill_${skillId.replace(/[^a-zA-Z0-9_]/g, "_")}`,
    label: `Colony skill: ${skill.name ?? skillId}`,
    description:
      skill.description ?? `Invoke Colony skill '${skill.name ?? skillId}'.`,
    parameters,
    async execute(_toolCallId, params) {
      try {
        const res = await ctx.client.executeSkill(
          skillId,
          params as JsonObject,
          ctx.identity(),
        );
        return textResult(JSON.stringify(res), res);
      } catch (err) {
        return textResult(
          JSON.stringify({ status: "failed", error: String(err) }),
        );
      }
    },
  };
}

export async function fetchActiveSkillTools(
  ctx: ColonyPluginContext,
): Promise<AnyTool[]> {
  try {
    const res = (await ctx.client.listSkills()) as {
      skills?: ActiveSkill[];
    };
    const out: AnyTool[] = [];
    for (const s of res.skills ?? []) {
      const tool = buildSkillTool(ctx, s);
      if (tool) out.push(tool);
    }
    return out;
  } catch (err) {
    ctx.logger?.warn?.(
      `[colony.tools] listSkills failed — no dynamic skills will be exposed (${String(err)})`,
    );
    return [];
  }
}

// ---------------------------------------------------------------------------
// Registration entry points
// ---------------------------------------------------------------------------

export interface ToolRegistrarHandle {
  /** Re-read active skills and re-register. Called by the WS event
   *  dispatcher when a ``skill_draft_approved`` arrives. */
  refreshSkillTools(): Promise<void>;
  /** Current count of registered skill tools — for logging. */
  skillToolCount(): number;
}

export function registerColonyTools(
  ctx: ColonyPluginContext,
  api: OpenClawPluginApi,
  memoryManager: Parameters<typeof buildMemoryWriteTool>[1] | null,
): ToolRegistrarHandle {
  const knownSkillToolNames = new Set<string>();

  // Older hosts (and unit-test mocks) may not expose registerTool. Fail
  // gracefully: skip registration instead of crashing the entire plugin.
  const register = (
    factory: () => unknown,
    opts: { name: string },
  ): void => {
    const fn = (api as { registerTool?: unknown }).registerTool;
    if (typeof fn !== "function") {
      ctx.logger?.debug?.(
        `[colony.tools] host does not expose registerTool; skipping ${opts.name}`,
      );
      return;
    }
    (fn as (f: () => unknown, o: { name: string }) => void).call(
      api,
      factory,
      opts,
    );
  };

  const registerStatic = () => {
    for (const tool of buildNativeTools(ctx)) {
      register(() => tool as never, { name: tool.name });
    }
    if (memoryManager) {
      const memTool = buildMemoryWriteTool(ctx, memoryManager);
      register(() => memTool as never, { name: memTool.name });
    }
    const authTool = buildVerifyAuthorityTool(ctx);
    register(() => authTool as never, { name: authTool.name });
  };

  const refreshSkillTools = async () => {
    const tools = await fetchActiveSkillTools(ctx);
    for (const tool of tools) {
      if (knownSkillToolNames.has(tool.name)) continue;
      knownSkillToolNames.add(tool.name);
      register(() => tool as never, { name: tool.name });
    }
  };

  registerStatic();
  // Fire-and-forget: skills may not be listed yet when the plugin
  // registers. Initial fetch runs in the background; subsequent
  // refreshes are event-driven.
  refreshSkillTools().catch(() => {
    /* already logged inside fetchActiveSkillTools */
  });

  return {
    refreshSkillTools,
    skillToolCount: () => knownSkillToolNames.size,
  };
}
