import { ColonyPluginConfigSchema, type ColonyPluginConfig } from "./config.js";
import { ColonyApiError, ColonySidecarClient } from "./sidecar-client.js";
import type {
  ContextSection,
  HostEvent,
  HostHealthResponse,
  HostMessage,
  HostTurnContext,
} from "./types.js";

/**
 * The real OpenClaw plugin API surface. Imported ``type``-only so the
 * plugin can still be loaded in test contexts without OpenClaw
 * installed at runtime — ``import type`` is erased by the TypeScript
 * compiler.
 *
 * Pulling the real type (instead of a structural stub) means ``tsc``
 * enforces every ``register*`` / ``on`` shape against the SDK. This
 * is what catches the adapter-contract drift tracked in the issue:
 * aevonix/colony-ai#7 Phase 1.
 */
import type {
  OpenClawPluginApi,
  PluginLogger,
} from "openclaw/plugin-sdk/plugin-entry";

/**
 * Context-engine contract surface pulled directly from the OpenClaw SDK.
 * Re-exported through ``openclaw/plugin-sdk`` (the package index); the
 * narrower ``openclaw/plugin-sdk/core`` subpath deliberately does not
 * re-export the ``ContextEngine`` shapes.
 */
import type {
  AssembleResult,
  CompactResult,
  ContextEngine,
  ContextEngineFactory,
  ContextEngineInfo,
  IngestResult,
} from "openclaw/plugin-sdk";
import { delegateCompactionToRuntime } from "openclaw/plugin-sdk";

export type { OpenClawPluginApi };

/**
 * ``AgentMessage`` is defined in ``@mariozechner/pi-agent-core`` but is
 * only pulled into the plugin transitively through ``openclaw``. Rather
 * than declaring the package as a direct dependency just to name the
 * type, we derive it from the ``ContextEngine.assemble`` signature —
 * this guarantees the adapter stays in lockstep with whatever shape the
 * SDK actually accepts.
 */
export type AgentMessage = Parameters<
  ContextEngine["assemble"]
>[0]["messages"][number];

/**
 * ``MemoryEmbeddingProviderAdapter`` and friends aren't exposed through
 * a public subpath of ``openclaw/plugin-sdk/*``, so we derive the shapes
 * from the ``registerMemoryEmbeddingProvider`` signature on
 * ``OpenClawPluginApi``. Using the derived types instead of redeclaring
 * them locally keeps the adapter in lockstep with whatever the SDK
 * ships.
 */
export type MemoryEmbeddingProviderAdapter = Parameters<
  OpenClawPluginApi["registerMemoryEmbeddingProvider"]
>[0];
export type MemoryEmbeddingProviderCreateOptions = Parameters<
  MemoryEmbeddingProviderAdapter["create"]
>[0];
export type MemoryEmbeddingProviderCreateResult = Awaited<
  ReturnType<MemoryEmbeddingProviderAdapter["create"]>
>;
export type MemoryEmbeddingProvider = NonNullable<
  MemoryEmbeddingProviderCreateResult["provider"]
>;

/**
 * `definePluginEntry` is the OpenClaw SDK helper. We re-import lazily so
 * test environments can stub it; production builds resolve it from the
 * peer-dependency `openclaw` package. See
 * `openclaw/plugin-sdk/plugin-entry`'s `DefinePluginEntryOptions` for
 * the authoritative shape — the fields listed here are a subset of
 * those we actually use.
 */
type DefinePluginEntry = (entry: {
  id: string;
  name: string;
  description: string;
  register: (api: OpenClawPluginApi) => void;
}) => unknown;

async function loadDefinePluginEntry(): Promise<DefinePluginEntry> {
  // Dynamic import keeps the plugin loadable in test contexts where
  // OpenClaw is not installed; we replace this with a static import once
  // OpenClaw publishes the SDK as a discoverable npm package.
  const mod = (await import(
    /* @vite-ignore */ "openclaw/plugin-sdk/plugin-entry"
  )) as { definePluginEntry: DefinePluginEntry };
  return mod.definePluginEntry;
}

const PLUGIN_ID = "colony";
const PLUGIN_NAME = "Colony Intelligence";
const PLUGIN_DESCRIPTION =
  "Mount Colony's graph memory, autonomy loop, context assembly, and safety pipeline into OpenClaw via the colony-core /v1/host API.";
const PLUGIN_VERSION = "0.0.1";

// ---------------------------------------------------------------------------
// Shared helpers used across adapters and exported so tests / future
// adapter rewrites can reuse them. Adapter *shapes* are still the
// scaffolded OpenClaw-pseudo-API; correcting them to match the real
// OpenClaw SDK contracts is follow-up work (see the linked tracking
// issue in README.md § Status).
// ---------------------------------------------------------------------------

/**
 * Error thrown when the sidecar explicitly doesn't expose an embedder.
 * Callers must decide whether to fall back to another provider — returning
 * empty vectors would silently corrupt downstream similarity search, so
 * the embedding adapter throws instead of returning a zero result.
 */
export class ColonyEmbedUnavailableError extends Error {
  constructor(reason: string) {
    super(`colony embedder unavailable: ${reason}`);
    this.name = "ColonyEmbedUnavailableError";
  }
}

/**
 * Shared degradation policy for calls the plugin makes into colony-core.
 *
 * Colony-core responds with a structured ``{error: {code, ...}}`` envelope
 * surfaced by ``ColonyApiError``. Adapters that want consistent
 * degradation wrap their call with this helper:
 *
 *  * ``phase1_wiring_required`` / 501: endpoint is not wired on this
 *    sidecar build. Return the caller-supplied fallback and log a
 *    warning.
 *  * 5xx: transient server failure. Return the fallback with a warn-log
 *    so operators see the degradation without the host crashing.
 *  * 4xx: structured contract error. Re-throw so callers can branch on
 *    the machine-readable code.
 *  * Everything else (network / timeout / unknown): treat as transient,
 *    fall back with a warn-log.
 */
export async function withDegradation<T>(
  opts: {
    name: string;
    logger?: { warn(m: string): void };
  },
  call: () => Promise<T>,
  fallback: () => T,
): Promise<T> {
  try {
    return await call();
  } catch (err) {
    if (err instanceof ColonyApiError) {
      if (err.code === "phase1_wiring_required" || err.status === 501) {
        opts.logger?.warn(
          `[colony] ${opts.name}: sidecar returned ${err.code} — returning fallback`,
        );
        return fallback();
      }
      if (err.status >= 500 && err.status < 600) {
        opts.logger?.warn(
          `[colony] ${opts.name}: sidecar ${err.status} ${err.code} — returning fallback`,
        );
        return fallback();
      }
      // 4xx and other structured errors are contract violations — surface them.
      throw err;
    }
    // Non-ColonyApiError (network, timeout, etc.) — treat as transient.
    opts.logger?.warn(
      `[colony] ${opts.name}: transport error — returning fallback (${String(err)})`,
    );
    return fallback();
  }
}

/**
 * Format a ``HostEvent`` into a single diagnostic log line. Used by the
 * events lifecycle service so operators see a stream of the cognition
 * events flowing from colony-core without dumping full payloads. The
 * default case returns only ``event.type`` so unknown events don't leak
 * their payloads.
 */
export function summarizeHostEvent(event: HostEvent): string {
  const p = (event.payload ?? {}) as Record<string, unknown>;
  switch (event.type) {
    case "turn_synced": {
      const session = typeof p.session_id === "string" ? p.session_id : "?";
      const topics = Array.isArray(p.topics) ? p.topics.length : 0;
      const entities = Array.isArray(p.entities) ? p.entities.length : 0;
      const tools = Array.isArray(p.tools_used) ? p.tools_used.length : 0;
      return `turn_synced session=${session} topics=${topics} entities=${entities} tools=${tools}`;
    }
    case "memory_consolidated": {
      const exam = typeof p.pairs_examined === "number" ? p.pairs_examined : "?";
      const merged = typeof p.pairs_merged === "number" ? p.pairs_merged : "?";
      const conflicts =
        typeof p.conflicts_detected === "number" ? p.conflicts_detected : "?";
      return `memory_consolidated examined=${exam} merged=${merged} conflicts=${conflicts}`;
    }
    case "proactive_message": {
      const target = typeof p.target === "string" ? p.target : "?";
      return `proactive_message target=${target}`;
    }
    case "log": {
      const msg = typeof p.message === "string" ? p.message : "(no message)";
      return `log: ${msg}`;
    }
    default:
      return `${event.type}`;
  }
}

export interface ColonyPluginContext {
  config: ColonyPluginConfig;
  client: ColonySidecarClient;
  identity: () => { host_id: string; plugin_version: string };
  /**
   * Plugin-scoped logger. Optional so ``buildContext`` can be stubbed in
   * tests without requiring a logger; adapters that want to emit
   * diagnostics should use the ``logger?.info(...)`` safe-access form.
   */
  logger?: PluginLogger;
}

function buildContext(api: OpenClawPluginApi): ColonyPluginContext {
  const config = ColonyPluginConfigSchema.parse(api.pluginConfig ?? {});
  const client = new ColonySidecarClient(config);
  return {
    config,
    client,
    identity: () => ({ host_id: config.hostId, plugin_version: PLUGIN_VERSION }),
    logger: api.logger,
  };
}

// ---------------------------------------------------------------------------
// Capability bundles — each helper returns the OpenClaw-shaped object for
// one extension slot. They're factored out so the entry point reads as a
// declarative manifest of what Colony provides.
// ---------------------------------------------------------------------------

/**
 * Build the ``MemoryPluginCapability`` object that matches the real
 * OpenClaw SDK contract (``{ promptBuilder?, runtime? }``).
 *
 * We populate ``promptBuilder`` and ``runtime``; ``flushPlanResolver``
 * and ``publicArtifacts`` are intentionally omitted — Colony doesn't
 * need them.
 */
function memoryCapability(
  ctx: ColonyPluginContext,
  caps: ReturnType<typeof capabilityProbe>,
) {
  // -- promptBuilder --------------------------------------------------
  const promptBuilder = (params: {
    availableTools: Set<string>;
    citationsMode?: string;
  }): string[] => {
    const lines: string[] = [
      "Colony graph memory is available. Context is auto-injected via context assembly — no explicit memory tool call is needed.",
    ];
    if (params.citationsMode !== "off") {
      lines.push(
        "When referencing recalled memories, include the memory ID in parentheses as a citation.",
      );
    }
    return lines;
  };

  // -- MemorySearchManager cache (one per agentId) --------------------
  const managers = new Map<string, ColonyMemorySearchManager>();

  // -- runtime --------------------------------------------------------
  const runtime = {
    async getMemorySearchManager(params: {
      cfg: unknown;
      agentId: string;
      purpose?: "default" | "status";
    }): Promise<{ manager: ColonyMemorySearchManager | null; error?: string }> {
      const existing = managers.get(params.agentId);
      if (existing) return { manager: existing };
      const mgr = new ColonyMemorySearchManager(ctx, caps);
      managers.set(params.agentId, mgr);
      return { manager: mgr };
    },

    resolveMemoryBackendConfig(_params: {
      cfg: unknown;
      agentId: string;
    }): { backend: "builtin" } {
      return { backend: "builtin" };
    },

    async closeAllMemorySearchManagers(): Promise<void> {
      managers.clear();
    },
  };

  return { promptBuilder, runtime };
}

/**
 * A ``MemorySearchManager`` backed by the colony-core sidecar.
 *
 * Every method that hits the sidecar is wrapped in ``withDegradation``
 * so transient / phase-1-wiring failures degrade gracefully.
 */
class ColonyMemorySearchManager {
  private readonly statusSnapshot: {
    backend: "builtin";
    provider: string;
    model: undefined;
    workspaceDir: undefined;
    sources: Array<"memory">;
    cache: { enabled: false };
    fts: { enabled: false; available: false };
    vector: { enabled: false; available: false };
  };

  constructor(
    private readonly ctx: ColonyPluginContext,
    private readonly caps: ReturnType<typeof capabilityProbe>,
  ) {
    this.statusSnapshot = {
      backend: "builtin",
      provider: "colony",
      model: undefined,
      workspaceDir: undefined,
      sources: ["memory"],
      cache: { enabled: false },
      fts: { enabled: false, available: false },
      vector: { enabled: false, available: false },
    };
  }

  async search(
    query: string,
    opts?: { maxResults?: number; minScore?: number },
  ): Promise<
    Array<{
      path: string;
      startLine: number;
      endLine: number;
      score: number;
      snippet: string;
      source: "memory";
      citation: string;
    }>
  > {
    return withDegradation(
      { name: "memory.search" },
      async () => {
        const res = await this.ctx.client.memorySearch({
          identity: this.ctx.identity(),
          query,
          limit: opts?.maxResults,
          min_score: opts?.minScore,
        });
        return res.entries.map((entry) => ({
          path: `memory://${entry.id}`,
          startLine: 0,
          endLine: 0,
          score: entry.score ?? 0,
          snippet: entry.content.slice(0, 300),
          source: "memory" as const,
          citation: `mem:${entry.id}`,
        }));
      },
      () => [],
    );
  }

  async readFile(params: {
    relPath: string;
    from?: number;
    lines?: number;
  }): Promise<{ text: string; path: string }> {
    return withDegradation(
      { name: "memory.readFile" },
      async () => {
        let memoryId: string | undefined;
        if (params.relPath.startsWith("memory://")) {
          memoryId = params.relPath.slice("memory://".length);
        }
        const res = await this.ctx.client.memoryRead({
          identity: this.ctx.identity(),
          memory_id: memoryId,
        });
        const entry = res.entries[0];
        return { text: entry?.content ?? "", path: params.relPath };
      },
      () => ({ text: "", path: params.relPath }),
    );
  }

  status() {
    return this.statusSnapshot;
  }

  async sync(params?: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
    progress?: (update: {
      completed: number;
      total: number;
      label?: string;
    }) => void;
  }): Promise<void> {
    await withDegradation(
      { name: "memory.sync" },
      async () => {
        await this.ctx.client.memoryFlush({
          identity: this.ctx.identity(),
          reason: params?.reason,
        });
        params?.progress?.({
          completed: 1,
          total: 1,
          label: "colony.memory.flush",
        });
      },
      () => undefined,
    );
  }

  async probeEmbeddingAvailability(): Promise<{
    ok: boolean;
    error?: string;
  }> {
    const hasEmbed = await this.caps.has("embed");
    if (hasEmbed) return { ok: true };
    return { ok: false, error: "colony sidecar does not advertise embed capability" };
  }

  async probeVectorAvailability(): Promise<boolean> {
    return false;
  }

  async close(): Promise<void> {
    // No-op — the sidecar client is shared across managers.
  }
}

/**
 * Sidecar batch limit for ``/v1/host/memory/embed``. Matches the
 * ``max_length=64`` on ``MemoryEmbedRequest.inputs`` in
 * ``colony/api/schemas/host.py`` — exceeding it triggers a 422 from the
 * sidecar, so we chunk on the plugin side to keep callers ignorant of
 * the transport-level limit.
 */
export const COLONY_EMBED_BATCH_CHUNK = 64;

/**
 * Build the ``MemoryEmbeddingProviderAdapter`` that routes embedding
 * requests to the colony-core sidecar.
 *
 * The adapter intentionally degrades by **returning ``{provider: null}``
 * from ``create()``** when the sidecar is unreachable or doesn't
 * advertise the ``embed`` capability — OpenClaw treats a null provider
 * as "this adapter has nothing to contribute" and moves on to the next
 * registered provider. This is the correct fallback shape; returning
 * zero-vectors from the embed methods would silently corrupt any
 * downstream vector similarity search.
 *
 * Once the adapter is active (non-null provider returned), ``embedQuery``
 * and ``embedBatch`` must **propagate errors as thrown** — OpenClaw
 * retries/fallback logic is the right layer to decide what to do with a
 * transient failure, not us.
 *
 * ``embedBatchInputs`` is deliberately omitted: Colony's sidecar does
 * not support multimodal embeddings.
 */
function memoryEmbeddingProvider(
  ctx: ColonyPluginContext,
): MemoryEmbeddingProviderAdapter {
  return {
    id: "colony-embed",
    // The sidecar resolves the actual model name from its wired embedder
    // config; "default" is just a placeholder that callers can override
    // via ``MemoryEmbeddingProviderCreateOptions.model``.
    defaultModel: "default",
    transport: "remote" as const,
    // ``autoSelectPriority`` intentionally omitted — Colony embedder is
    // only used when the user explicitly configures it as the memory
    // embedding provider, not via OpenClaw's auto-selection fallback.

    create: async (
      options: MemoryEmbeddingProviderCreateOptions,
    ): Promise<MemoryEmbeddingProviderCreateResult> => {
      const model = options.model || "default";

      // Probe sidecar health at create time. If the sidecar is
      // unreachable, doesn't advertise the "embed" capability, or
      // returns 501, return ``{ provider: null }`` so OpenClaw falls
      // through to another registered adapter instead of installing a
      // broken embedder.
      try {
        const health = await ctx.client.health();
        if (!health.capabilities.includes("embed")) {
          ctx.logger?.info(
            "[colony] embed: sidecar did not advertise 'embed' capability — returning null provider",
          );
          return { provider: null };
        }
      } catch (err) {
        if (
          err instanceof ColonyApiError &&
          (err.status === 501 || err.code === "phase1_wiring_required")
        ) {
          ctx.logger?.info(
            `[colony] embed: sidecar returned ${err.code} — returning null provider`,
          );
        } else {
          ctx.logger?.warn(
            `[colony] embed: health probe failed — returning null provider (${String(err)})`,
          );
        }
        return { provider: null };
      }

      const embedOnce = async (inputs: string[]): Promise<number[][]> => {
        const res = await ctx.client.memoryEmbed({
          identity: ctx.identity(),
          inputs,
          model,
        });
        return res.vectors;
      };

      const provider: MemoryEmbeddingProvider = {
        id: "colony-embed",
        model,
        embedQuery: async (text: string): Promise<number[]> => {
          const vectors = await embedOnce([text]);
          const first = vectors[0];
          if (!first) {
            throw new Error(
              "colony embed: sidecar returned empty vectors array",
            );
          }
          return first;
        },
        embedBatch: async (texts: string[]): Promise<number[][]> => {
          if (texts.length === 0) return [];
          if (texts.length <= COLONY_EMBED_BATCH_CHUNK) {
            return embedOnce(texts);
          }
          const all: number[][] = [];
          for (let i = 0; i < texts.length; i += COLONY_EMBED_BATCH_CHUNK) {
            const slice = texts.slice(i, i + COLONY_EMBED_BATCH_CHUNK);
            const chunkVectors = await embedOnce(slice);
            all.push(...chunkVectors);
          }
          return all;
        },
        // ``embedBatchInputs`` intentionally omitted — Colony sidecar
        // doesn't support multimodal embedding inputs.
      };

      return {
        provider,
        runtime: {
          id: "colony-embed",
          cacheKeyData: {
            provider: "colony",
            sidecarUrl: ctx.config.sidecarUrl,
            model,
          },
        },
      };
    },

    formatSetupError: (err: unknown): string => {
      if (err instanceof ColonyApiError) {
        return `Colony sidecar (${err.status} ${err.code}): ${err.message}`;
      }
      if (err instanceof Error) {
        return `Colony sidecar unreachable: ${err.message}`;
      }
      return `Colony sidecar unreachable: ${String(err)}`;
    },

    shouldContinueAutoSelection: (err: unknown): boolean => {
      if (err instanceof ColonyApiError) {
        // 501 or explicit ``embed_not_wired`` → Colony can't embed here;
        // let auto-selection try the next provider. A 5xx/4xx from a
        // wired endpoint (e.g. 503 sidecar overloaded) is a real
        // failure the host should surface instead of silently swapping
        // providers.
        return err.status === 501 || err.code === "embed_not_wired";
      }
      // Transport-level errors (network, timeout, DNS, …) — let
      // auto-selection try the next provider.
      return true;
    },
  };
}

/**
 * Build the ``ContextEngineFactory`` that plugs Colony's context-assembly
 * sidecar into OpenClaw's ``ContextEngine`` contract.
 *
 * The engine is intentionally a *thin* adapter:
 *
 *  - ``ingest`` is a no-op. Colony derives its context freshly each turn
 *    from its own server-side stores (memory graph, continuity, etc.),
 *    so there is nothing to buffer on the plugin side. Post-turn
 *    cognition sync is handled by the ``reply_dispatch`` hook wired
 *    separately in Phase 6 — calling turn-sync here would double-fire.
 *  - ``assemble`` calls ``/v1/host/context/assemble`` and converts the
 *    returned ``sections`` + ``notices`` into a single
 *    ``systemPromptAddition``. Messages are passed through unchanged —
 *    Colony only augments the system prompt, it never rewrites the
 *    transcript.
 *  - ``compact`` delegates to OpenClaw's built-in runtime compaction.
 *    Colony's ``MemoryConsolidator`` does *long-term episodic*
 *    consolidation, not transcript compaction, so we deliberately don't
 *    claim ``ownsCompaction``.
 *
 * Degradation policy follows the rest of the plugin: 501 /
 * ``phase1_wiring_required`` and 5xx return a pass-through result with a
 * warn-log, 4xx structured errors re-throw, transport errors warn and
 * pass through. See ``withDegradation`` for the shared implementation.
 */
function contextEngineFactory(
  ctx: ColonyPluginContext,
  caps: ReturnType<typeof capabilityProbe>,
  logger?: { warn(m: string): void; info?(m: string): void },
): ContextEngineFactory {
  const info: ContextEngineInfo = {
    id: "colony",
    name: "Colony Context Engine",
    version: PLUGIN_VERSION,
    // Colony augments the system prompt; compaction is delegated to the
    // OpenClaw runtime (see ``compact`` below).
    ownsCompaction: false,
  };

  const engine: ContextEngine = {
    info,

    async ingest(_params): Promise<IngestResult> {
      // No-op: Colony derives context freshly each turn from its own
      // server-side stores. The reply_dispatch hook (Phase 6) handles
      // post-turn cognition sync separately — do not double-wire here.
      return { ingested: true };
    },

    async assemble(params): Promise<AssembleResult> {
      const passThrough = (notice?: string): AssembleResult => {
        const addition = notice ? toAddition([], [notice]) : undefined;
        return {
          messages: params.messages,
          estimatedTokens: estimateTokens(params.messages, addition),
          systemPromptAddition: addition,
        };
      };

      // Capability gate: skip the sidecar call only when the probe
      // *succeeded* and reported "context" is off. Unknown probe state
      // (probe failed) still tries the call so operators see the real
      // transport / contract error rather than a silent no-op.
      if (
        (await caps.hasProbedSuccessfully()) &&
        !(await caps.has("context"))
      ) {
        return passThrough();
      }

      const incoming = buildIncomingMessage(params.messages, params.prompt);
      if (!incoming) {
        // No user message to prompt with — skip the sidecar call and pass
        // through. Colony's assembler is keyed on a user turn.
        return passThrough();
      }

      const res = await withDegradation(
        { name: "context.assemble", logger },
        () =>
          ctx.client.contextAssemble({
            identity: ctx.identity(),
            context: {
              session_id: params.sessionId,
              // KNOWN GAP: OpenClaw's ``assemble`` params don't carry a
              // person / contact id. Use ``sessionKey`` (or
              // ``sessionId``) as a surrogate so the sidecar has *some*
              // stable routing key; tracked as a follow-up under
              // aevonix/colony-ai#7.
              contact_id: params.sessionKey ?? params.sessionId,
            },
            incoming_message: incoming,
            available_tools: params.availableTools
              ? Array.from(params.availableTools)
              : undefined,
            citations_mode: mapCitations(params.citationsMode),
          }),
        () => ({ sections: [], notices: ["colony-context: degraded"] }),
      );

      const addition = toAddition(res.sections, res.notices);
      return {
        // Pass-through: Colony only augments the system prompt, it
        // does not rewrite the transcript. Keep the array identity so
        // callers that diff-by-reference see no change.
        messages: params.messages,
        estimatedTokens: estimateTokens(params.messages, addition),
        systemPromptAddition: addition,
      };
    },

    async compact(params): Promise<CompactResult> {
      // Colony has no transcript-compaction of its own.
      // ``MemoryConsolidator`` is long-term episodic consolidation — it
      // is NOT transcript compaction. Delegate to OpenClaw's built-in
      // runtime compaction path instead.
      return delegateCompactionToRuntime(params);
    },
  };

  return () => engine;
}

// ---------------------------------------------------------------------------
// ContextEngine helpers
// ---------------------------------------------------------------------------

/**
 * Pull a ``HostMessage`` to send as the sidecar's ``incoming_message``:
 *
 *  1. If ``prompt`` is set, prefer it verbatim — the SDK documents
 *     ``prompt`` as "the incoming user prompt for this turn" and it's
 *     the authoritative signal.
 *  2. Otherwise walk ``messages`` backwards and return the most recent
 *     user message, flattening content-part arrays down to their
 *     ``text`` fields.
 *
 * Returns ``undefined`` when neither yields a user turn; the caller
 * uses that as the signal to skip the sidecar call entirely.
 */
function buildIncomingMessage(
  messages: AgentMessage[],
  prompt: string | undefined,
): HostMessage | undefined {
  if (prompt !== undefined && prompt !== "") {
    return { role: "user", content: prompt };
  }
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i] as { role?: string; content?: unknown };
    if (m.role === "user") {
      const c = m.content;
      const text =
        typeof c === "string"
          ? c
          : Array.isArray(c)
            ? c
                .filter((p: unknown) => (p as { type?: string })?.type === "text")
                .map((p: unknown) => (p as { text?: string }).text ?? "")
                .join("\n")
            : "";
      return { role: "user", content: text };
    }
  }
  return undefined;
}

/**
 * Translate the SDK's ``citationsMode`` vocabulary
 * (``"auto" | "on" | "off"``) into the Colony sidecar's
 * ``"off" | "inline" | "appendix"`` vocabulary.
 *
 * This is a judgement-call mapping — the two vocabularies don't line up
 * 1:1. We picked:
 *
 *  - ``"auto"`` → ``"inline"`` (low-friction default, matches Colony's
 *    behavior when the host doesn't opt in to a specific rendering).
 *  - ``"on"``   → ``"appendix"`` (explicit opt-in by the host → surface
 *    citations visibly at end of the assembled context).
 *  - ``"off"``  → ``"off"``     (straight mapping).
 *  - ``undefined`` → ``undefined`` (omit from the wire so the sidecar
 *    applies its own default).
 *
 * Revisit this mapping if either side's vocabulary grows.
 */
function mapCitations(
  mode: "auto" | "on" | "off" | undefined,
): "off" | "inline" | "appendix" | undefined {
  if (mode === undefined) return undefined;
  if (mode === "auto") return "inline";
  if (mode === "on") return "appendix";
  return "off";
}

/**
 * Render ``sections`` + ``notices`` into a single
 * ``systemPromptAddition`` string. Sections are sorted by priority
 * descending (higher numbers go first) so the sidecar's ordering is
 * preserved; missing ``priority`` counts as 0.
 *
 * Returns ``undefined`` when there's nothing to add — the SDK treats
 * that as "no addition" rather than requiring an empty string.
 */
function toAddition(
  sections: ContextSection[],
  notices: string[] | undefined,
): string | undefined {
  const sorted = [...sections].sort(
    (a, b) => (b.priority ?? 0) - (a.priority ?? 0),
  );
  const body = sorted
    .map((s) => (s.title ? `## ${s.title}\n${s.body}` : s.body))
    .filter(Boolean)
    .join("\n\n");
  const noticeBlock =
    notices && notices.length > 0
      ? "Notices:\n" + notices.map((n) => `- ${n}`).join("\n")
      : "";
  const combined = [noticeBlock, body].filter(Boolean).join("\n\n");
  return combined.length > 0 ? combined : undefined;
}

/**
 * Cheap token estimate (~4 chars/token) covering the pass-through
 * ``messages`` array plus the ``systemPromptAddition``. This matches
 * pi-agent-core's internal approximation closely enough for
 * compaction-threshold decisions; the OpenClaw runtime does its own
 * precise accounting before it actually invokes a provider.
 */
function estimateTokens(
  messages: AgentMessage[],
  addition: string | undefined,
): number {
  let chars = addition?.length ?? 0;
  for (const m of messages) {
    const c = (m as { content?: unknown }).content;
    if (typeof c === "string") {
      chars += c.length;
    } else if (Array.isArray(c)) {
      for (const p of c) {
        const t = (p as { text?: unknown }).text;
        if (typeof t === "string") chars += t.length;
      }
    }
  }
  return Math.ceil(chars / 4);
}

function agentHarness(ctx: ColonyPluginContext) {
  return {
    id: "colony-harness",
    async runTurn(args: {
      context: HostTurnContext;
      messages: HostMessage[];
      availableTools?: string[];
      modelOverride?: string;
    }) {
      return ctx.client.reasoningTurn({
        identity: ctx.identity(),
        context: args.context,
        messages: args.messages,
        available_tools: args.availableTools,
        model_override: args.modelOverride,
      });
    },
  };
}

function safetyHook(ctx: ColonyPluginContext) {
  return async (event: {
    context: HostTurnContext;
    responseText: string;
    incomingMessageText?: string;
    targetGateway?: string;
    trustTier?: string;
    mentionedEntities?: string[];
  }) => {
    const res = await ctx.client.safetyCheck({
      identity: ctx.identity(),
      context: event.context,
      response_text: event.responseText,
      incoming_message_text: event.incomingMessageText ?? "",
      target_gateway: event.targetGateway,
      trust_tier: event.trustTier,
      mentioned_entities: event.mentionedEntities,
    });
    if (res.blocked) {
      return { cancel: true, reason: res.reason ?? "blocked by colony safety pipeline" };
    }
    return undefined;
  };
}

/**
 * Lazy, single-flight capability probe. We cache the sidecar's
 * ``/v1/host/health.capabilities`` on first use so the post-turn hook
 * knows whether to call ``/v1/host/turns/sync`` without paying an
 * extra round trip on every reply. A failed probe leaves the cache
 * empty so the next turn re-probes instead of silently skipping
 * forever.
 */
function capabilityProbe(ctx: ColonyPluginContext) {
  let capsPromise: Promise<ReadonlySet<string>> | null = null;
  // Observation-safe: both ``has()`` and ``hasProbedSuccessfully()``
  // await ``capsPromise`` before reading this flag, and the flag is
  // set *inside* the promise body. They always agree at observation
  // time, so no race between "capabilities available" and "probe
  // succeeded" signals.
  let lastProbeSucceeded = false;

  const load = async (): Promise<ReadonlySet<string>> => {
    try {
      const health = await ctx.client.health();
      lastProbeSucceeded = true;
      return new Set(health.capabilities);
    } catch {
      // Don't cache failures — reset so the next call re-probes.
      capsPromise = null;
      lastProbeSucceeded = false;
      return new Set<string>();
    }
  };

  return {
    async has(cap: string): Promise<boolean> {
      if (capsPromise === null) {
        capsPromise = load();
      }
      return (await capsPromise).has(cap);
    },
    /**
     * Did the last probe complete without throwing? Lets callers
     * distinguish "sidecar said this capability is off" (probe succeeded,
     * set doesn't contain cap → skip) from "probe failed" (unknown
     * state → let the real call surface the error so we don't silently
     * no-op when the sidecar is just briefly unreachable).
     */
    async hasProbedSuccessfully(): Promise<boolean> {
      if (capsPromise === null) {
        capsPromise = load();
      }
      await capsPromise;
      return lastProbeSucceeded;
    },
    reset(): void {
      capsPromise = null;
      lastProbeSucceeded = false;
    },
  };
}

function postTurnHook(
  ctx: ColonyPluginContext,
  caps: ReturnType<typeof capabilityProbe>,
) {
  return async (event: {
    context: HostTurnContext;
    incomingMessage?: HostMessage;
    outgoingMessage?: HostMessage;
    correction?: string;
    topics?: string[];
    entities?: string[];
    pendingTasks?: string[];
    toolsUsed?: string[];
    summary?: string;
  }) => {
    const signals = ctx.client.signalsIngest({
      identity: ctx.identity(),
      context: event.context,
      incoming_message: event.incomingMessage,
      outgoing_message: event.outgoingMessage,
      correction: event.correction,
    });

    const turnSyncIfSupported = caps.has("turn_sync").then(async (enabled) => {
      if (!enabled) {
        return;
      }
      await ctx.client.turnsSync({
        identity: ctx.identity(),
        context: event.context,
        topics: event.topics,
        entities: event.entities,
        pending_tasks: event.pendingTasks,
        tools_used: event.toolsUsed,
        summary: event.summary,
      });
    });

    // Fan out both calls concurrently; surface the first failure but
    // let the other complete so partial state isn't lost.
    const results = await Promise.allSettled([signals, turnSyncIfSupported]);
    for (const r of results) {
      if (r.status === "rejected") {
        throw r.reason;
      }
    }
  };
}

function eventsLifecycleService(
  ctx: ColonyPluginContext,
  logger?: OpenClawPluginApi["logger"],
) {
  let subscription: { close: () => void } | null = null;

  return {
    id: "colony-events",
    async start() {
      if (!ctx.config.forwardProactiveDeliveries) {
        logger?.info(
          "[colony] events: forwardProactiveDeliveries=false — skipping subscription",
        );
        return;
      }
      try {
        subscription = ctx.client.openEvents((event: HostEvent) => {
          // Surface the event via OpenClaw's logger so operators see
          // the cognition stream in plugin diagnostics. The actual
          // reply_dispatch wiring that turns proactive_message events
          // into channel posts is host-side and wired separately once
          // adapter contracts are corrected (see tracking issue).
          // Individual event-formatting failures must not kill the
          // subscription — wrap the body so one malformed frame can't
          // take the stream down.
          try {
            logger?.info(`[colony.event] ${summarizeHostEvent(event)}`);
          } catch (cbErr) {
            logger?.warn(
              `[colony] events: callback error on ${event.type} (${String(cbErr)})`,
            );
          }
        });
        logger?.info(
          `[colony] events: subscribed to ${ctx.config.sidecarUrl}/v1/host/events`,
        );
      } catch (err) {
        logger?.warn(
          `[colony] events: subscription failed — proactive deliveries disabled until restart (${String(err)})`,
        );
      }
    },
    async stop() {
      subscription?.close();
      subscription = null;
    },
  };
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export async function createColonyPlugin(): Promise<unknown> {
  const definePluginEntry = await loadDefinePluginEntry();

  return definePluginEntry({
    id: PLUGIN_ID,
    name: PLUGIN_NAME,
    description: PLUGIN_DESCRIPTION,
    register(api: OpenClawPluginApi) {
      const ctx = buildContext(api);
      const caps = capabilityProbe(ctx);

      api.registerService(eventsLifecycleService(ctx, api.logger));

      // Adapter shape mismatches surfaced by the real SDK types —
      // each @ts-expect-error is a placeholder for a follow-up phase
      // of aevonix/colony-ai#7. Removing an error marker without
      // fixing the adapter will break the build. The scaffolded
      // values still run the sidecar client calls we need during
      // development smoke-testing; OpenClaw's runtime will either
      // silently no-op on these until the shapes are corrected.

      if (ctx.config.ownMemoryCapability) {
        api.registerMemoryCapability(memoryCapability(ctx, caps));
      }

      // #7 Phase 3 — ``memoryEmbeddingProvider`` returns the real
      // ``MemoryEmbeddingProviderAdapter`` shape (see its doc comment),
      // so no ``@ts-expect-error`` is needed here.
      api.registerMemoryEmbeddingProvider(memoryEmbeddingProvider(ctx));

      // #7 Phase 4 — ``contextEngineFactory`` implements the real
      // ``ContextEngine`` contract (see its doc comment), so no
      // ``@ts-expect-error`` is needed here.
      api.registerContextEngine(
        "colony",
        contextEngineFactory(ctx, caps, api.logger),
      );

      if (ctx.config.ownReasoningLoop) {
        // #7 Phase 5 — rewrite against AgentHarness
        // ({ id, label, supports(ctx), runAttempt(EmbeddedRunAttemptParams) })
        // @ts-expect-error — scaffold shape, see issue #7 Phase 5
        api.registerAgentHarness(agentHarness(ctx));
      }

      // #7 Phase 6 — rewrite safety hook handler against the real
      // InternalHookHandler / PluginHookMessageSendingEvent shapes.
      // @ts-expect-error — bespoke event shape, see issue #7 Phase 6
      api.registerHook(["message_sending"], safetyHook(ctx));

      // #7 Phase 6 — rewrite reply_dispatch handler against
      // PluginHookReplyDispatchEvent + PluginHookReplyDispatchContext.
      // @ts-expect-error — bespoke event shape, see issue #7 Phase 6
      api.on("reply_dispatch", postTurnHook(ctx, caps));

      api.logger.info(`[colony] plugin registered against ${ctx.config.sidecarUrl}`);

      // Best-effort capability check so operators see the wiring status
      // in OpenClaw's plugin diagnostics on startup.
      ctx.client
        .health()
        .then((h: HostHealthResponse) =>
          api.logger.info(
            `[colony] sidecar capabilities=${h.capabilities.join(",")} status=${h.status}`,
          ),
        )
        .catch((err: unknown) =>
          api.logger.warn(`[colony] sidecar health check failed: ${String(err)}`),
        );
    },
  });
}

// Re-export internals for the smoke tests / programmatic consumers.
export { ColonySidecarClient, ColonyApiError } from "./sidecar-client.js";
export type { ColonyPluginConfig } from "./config.js";
export {
  memoryCapability as __memoryCapability,
  memoryEmbeddingProvider as __memoryEmbeddingProvider,
  contextEngineFactory as __contextEngineFactory,
  agentHarness as __agentHarness,
  safetyHook as __safetyHook,
  postTurnHook as __postTurnHook,
  capabilityProbe as __capabilityProbe,
  eventsLifecycleService as __eventsLifecycleService,
};
