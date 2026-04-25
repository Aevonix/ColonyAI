import {
  ColonyPluginConfigSchema,
  withHostLLMEnvOverrides,
  type ColonyPluginConfig,
} from "./config.js";
import { ColonyApiError, ColonySidecarClient } from "./sidecar-client.js";
import type {
  ContextSection,
  HostEvent,
  HostEventType,
  HostHealthResponse,
  HostIdentity,
  HostMessage,
} from "./types.js";
import { SessionTextCache } from "./hooks/session-text-cache.js";
import { TurnExtractionPipeline } from "./extraction/pipeline.js";
import { createContextCache, type ContextCache } from "./context-cache.js";
import { dispatchHostEvent } from "./event-handlers.js";
import {
  registerColonyTools,
  type ToolRegistrarHandle,
} from "./tool-registrar.js";
import {
  loadAgentConfig,
  isRemoteAgent,
  RemoteAgentClient,
  type Initiative,
} from "./remote-agent.js";

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
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

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
import { normalizeUsage } from "openclaw/plugin-sdk/agent-harness";
import { readJsonBodyWithLimit } from "openclaw/plugin-sdk/webhook-request-guards";
import type {
  AgentHarness,
  AgentHarnessAttemptParams,
  AgentHarnessAttemptResult,
  AgentHarnessSupport,
  AgentHarnessSupportContext,
} from "openclaw/plugin-sdk/agent-harness";

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
 * `definePluginEntry` is imported directly from the OpenClaw SDK.
 * See `openclaw/plugin-sdk/plugin-entry`'s `DefinePluginEntryOptions` for
 * the authoritative shape — the fields listed here are a subset of
 * those we actually use.
 */

const PLUGIN_ID = "colony";
const PLUGIN_NAME = "Colony Intelligence";
const PLUGIN_DESCRIPTION =
  "Mount Colony's graph memory, autonomy loop, context assembly, and response gate into OpenClaw via the colony /v1/host API.";
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
 * Shared degradation policy for calls the plugin makes into colony.
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
 * events flowing from colony without dumping full payloads. The
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

/**
 * Handle proactive_message events by spawning a subagent turn to deliver
 * the message. This is a workaround until OpenClaw provides a direct
 * `sendProactiveMessage(channelId, content)` API.
 *
 * The subagent approach works because:
 * 1. Sidecar pushes `proactive_message` event via WebSocket
 * 2. Plugin calls `subagent.run({ sessionKey, message, deliver: true })`
 * 3. Subagent spawns minimal agent turn with the notification
 * 4. OpenClaw routes the agent's response to the channel
 *
 * Trade-off: Spawns a full LLM turn just to echo a message (adds latency,
 * burns tokens) but works with zero infrastructure changes.
 */
async function handleProactiveMessage(
  event: HostEvent,
  api: OpenClawPluginApi,
  logger: PluginLogger | undefined,
): Promise<void> {
  const p = event.payload ?? {};
  const sessionKey = typeof p.session_key === "string" ? p.session_key : undefined;
  const content = typeof p.content === "string" ? p.content : undefined;

  if (!sessionKey) {
    logger?.warn("[colony] proactive_message missing session_key — cannot deliver");
    return;
  }

  if (!content) {
    logger?.warn("[colony] proactive_message missing content — nothing to deliver");
    return;
  }

  // Check if runtime API is available
  const runtime = (api as unknown as { runtime?: { subagent?: { run: unknown } } }).runtime;
  if (!runtime?.subagent?.run) {
    logger?.warn("[colony] runtime.subagent.run not available — cannot deliver proactive message");
    return;
  }

  const subagentRun = runtime.subagent.run as (params: {
    sessionKey: string;
    message: string;
    deliver?: boolean;
  }) => Promise<{ runId: string }>;

  try {
    logger?.info(`[colony] delivering proactive message to session=${sessionKey}`);

    const result = await subagentRun({
      sessionKey,
      message: `Deliver this notification to the user: ${content}`,
      deliver: true,
    });

    logger?.info(`[colony] proactive delivery started runId=${result.runId}`);
  } catch (err) {
    logger?.error(`[colony] proactive delivery failed: ${String(err)}`);
    throw err;
  }
}

/**
 * Snapshot of colony / node identity fields resolved from the sidecar's
 * ``/v1/host/identity/status`` at plugin registration time. Cached on the
 * context so ``identity()`` can produce a full ``HostIdentity`` without
 * re-hitting the sidecar each turn.
 */
export interface IdentitySnapshot {
  colony_id?: string;
  node_id?: string;
  node_cert_fingerprint?: string;
  trust_tier?: "REGULAR" | "TRUSTED" | "PRIVILEGED" | "GENESIS";
  /** Result of the last ``chainVerify`` call at plugin startup. */
  chain_valid?: boolean;
  /** Hex-encoded Ed25519 signature of ``colony_id:data:timestamp`` returned
   *  by the sidecar. Present only when the key manager is loaded. */
  signed_attestation?: string;
  attested_at?: string;
  signer_public_key?: string;
}

export interface ColonyPluginContext {
  config: ColonyPluginConfig;
  client: ColonySidecarClient;
  identity: () => HostIdentity;
  /**
   * Resolve identity fields (colony_id, node_id, trust_tier) from the
   * sidecar and cache them on the context. Safe to call multiple times;
   * returns the current snapshot. Never throws — failures log and return
   * the existing (possibly empty) snapshot.
   */
  refreshIdentity: () => Promise<IdentitySnapshot>;
  /**
   * Ask the sidecar to verify the chain state and sign an attestation
   * over ``data``. Caches the result in the identity snapshot. Never
   * throws.
   */
  verifyChain: (data?: string) => Promise<IdentitySnapshot>;
  /**
   * Plugin-wide cache-invalidation bus. The WS event dispatcher writes
   * to it on memory/goal/world-model/briefing/skill updates so derived
   * caches (identity snapshot, skill-tool registrations) can refresh.
   */
  cache: ContextCache;
  /**
   * Plugin-scoped logger. Optional so ``buildContext`` can be stubbed in
   * tests without requiring a logger; adapters that want to emit
   * diagnostics should use the ``logger?.info(...)`` safe-access form.
   */
  logger?: PluginLogger;
}

function buildContext(api: OpenClawPluginApi): ColonyPluginContext {
  const parsed = ColonyPluginConfigSchema.parse(api.pluginConfig ?? {});
  const config = withHostLLMEnvOverrides(parsed);
  const client = new ColonySidecarClient(config);
  const snapshot: IdentitySnapshot = {};

  const identity = (): HostIdentity => ({
    host_id: config.hostId,
    plugin_version: PLUGIN_VERSION,
    ...(snapshot.colony_id ? { colony_id: snapshot.colony_id } : {}),
    ...(snapshot.node_id ? { node_id: snapshot.node_id } : {}),
    ...(snapshot.node_cert_fingerprint
      ? { node_cert_fingerprint: snapshot.node_cert_fingerprint }
      : {}),
    ...(snapshot.trust_tier ? { trust_tier: snapshot.trust_tier } : {}),
  });

  const refreshIdentity = async (): Promise<IdentitySnapshot> => {
    try {
      const status = (await client.identityStatus()) as {
        colony_id?: string | null;
        node_id?: string | null;
        node_cert_fingerprint?: string | null;
        trust_tier?: IdentitySnapshot["trust_tier"] | null;
      };
      if (status?.colony_id) snapshot.colony_id = status.colony_id;
      if (status?.node_id) snapshot.node_id = status.node_id;
      if (status?.node_cert_fingerprint)
        snapshot.node_cert_fingerprint = status.node_cert_fingerprint;
      if (status?.trust_tier) snapshot.trust_tier = status.trust_tier;
    } catch (err) {
      api.logger?.warn(
        `[colony] identity bootstrap failed: ${String(err)}`,
      );
    }
    return { ...snapshot };
  };

  const verifyChain = async (
    data: string = "bootstrap-probe",
  ): Promise<IdentitySnapshot> => {
    try {
      const res = await client.chainVerify(data, identity());
      snapshot.chain_valid = Boolean(res?.valid);
      if (res?.signed_attestation)
        snapshot.signed_attestation = res.signed_attestation;
      if (res?.attested_at) snapshot.attested_at = res.attested_at;
      if (res?.signer_public_key)
        snapshot.signer_public_key = res.signer_public_key;
    } catch (err) {
      api.logger?.warn(`[colony] chain verify failed: ${String(err)}`);
    }
    return { ...snapshot };
  };

  const cache = createContextCache();

  return {
    config,
    client,
    identity,
    refreshIdentity,
    verifyChain,
    cache,
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
 * A ``MemorySearchManager`` backed by the colony sidecar.
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

  /**
   * Persist a new memory through the sidecar. Agent turns call this
   * via the ``colony_memory_write`` tool (registered by the tool
   * registrar) so learning from a conversation actually lands in the
   * graph instead of living only in the turn transcript.
   */
  async write(params: {
    content: string;
    kind?: string;
    personId?: string;
    entities?: string[];
    tags?: string[];
  }): Promise<{ id?: string; accepted: boolean }> {
    return withDegradation(
      { name: "memory.write" },
      async () => {
        const res = await this.ctx.client.memoryWrite({
          identity: this.ctx.identity(),
          content: params.content,
          type: params.kind,
          person_id: params.personId,
          entities: params.entities,
          tags: params.tags,
        });
        return { id: res.id ?? undefined, accepted: res.accepted ?? false };
      },
      () => ({ accepted: false } as { id?: string; accepted: boolean }),
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
        const entry = res.entries?.[0];
        if (!entry?.content) return { text: "", path: params.relPath };
        return { text: entry.content, path: params.relPath };
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
    // Vector operations require the embed capability (vector store is part of memory subsystem)
    const hasEmbed = await this.caps.has("embed");
    return hasEmbed;
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
 * requests to the colony sidecar.
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
      logger?.info?.("[colony] context engine assemble() called — sessionId=" + (params.sessionId ?? "?"));

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
          ctx.client.enrichedContext({
            identity: ctx.identity(),
            context: {
              session_id: params.sessionId,
              contact_id: params.sessionKey ?? params.sessionId,
            },
            message: incoming.content,
            features: {
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
            },
            compression: ctx.config.compression !== "off" ? ctx.config.compression : undefined,
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
  notices: string[] | null | undefined,
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

// ---------------------------------------------------------------------------
// AgentHarness
// ---------------------------------------------------------------------------

/**
 * Safe defaults for ``EmbeddedRunAttemptResult``. Every result returned
 * from ``runAttempt`` — success, shaped error, or abort — must fill all
 * required fields. We centralise the zero-value shape here so each
 * branch of ``runAttempt`` merges only what's relevant.
 */
function baseAttemptResult(
  sessionId: string,
): AgentHarnessAttemptResult {
  return {
    aborted: false,
    externalAbort: false,
    timedOut: false,
    idleTimedOut: false,
    timedOutDuringCompaction: false,
    promptError: null,
    promptErrorSource: null,
    sessionIdUsed: sessionId,
    messagesSnapshot: [],
    assistantTexts: [],
    toolMetas: [],
    lastAssistant: undefined,
    didSendViaMessagingTool: false,
    messagingToolSentTexts: [],
    messagingToolSentMediaUrls: [],
    messagingToolSentTargets: [],
    cloudCodeAssistFormatError: false,
    replayMetadata: {
      hadPotentialSideEffects: false,
      replaySafe: true,
    },
    itemLifecycle: {
      startedCount: 0,
      completedCount: 0,
      activeCount: 0,
    },
  };
}

/**
 * Zero-usage snapshot for the ``AssistantMessage.usage`` field. Colony's
 * sidecar responds with a loose ``Record<string, unknown>`` usage bag we
 * normalise via ``normalizeUsage`` at the OpenClaw-result level
 * (``attemptUsage``); the ``AssistantMessage.usage`` shape pi-ai expects
 * is stricter and we fill it with zeros since no per-message
 * breakdown is available.
 */
function zeroAssistantUsage(): {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  totalTokens: number;
  cost: {
    input: number;
    output: number;
    cacheRead: number;
    cacheWrite: number;
    total: number;
  };
} {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

/**
 * Build the Colony ``AgentHarness`` adapter.
 *
 * Contract reference: ``openclaw/plugin-sdk/agent-harness`` — especially
 * ``AgentHarness``, ``AgentHarnessSupport``, and
 * ``EmbeddedRunAttemptResult`` (which ``AgentHarnessAttemptResult``
 * aliases). See the Codex reference harness at
 * ``openclaw/dist/harness-*.js`` (``buildResult``) for the canonical
 * "safe defaults" shape of a result.
 *
 * Supports semantics:
 *
 *  - ``ownReasoningLoop=false`` → never supported.
 *  - ``requestedRuntime`` must be ``"colony"`` or ``"auto"`` (we skip
 *    ``"pi"``, ``"codex"``, any other explicit opt-in).
 *  - Capability probe must have completed and advertised ``"reasoning"``.
 *    The first ``supports`` call kicks the probe in the background so
 *    subsequent turns see the resolved set; the *very first* call
 *    returns ``{ supported: false }`` with the "not yet probed" reason.
 *
 * Priority is ``50`` — below Codex's ``100`` so Codex wins when both
 * advertise support for ``codex/*`` model refs.
 *
 * ``runAttempt`` NEVER throws. Every failure mode is returned as a
 * shaped ``EmbeddedRunAttemptResult`` with ``promptError`` set, which
 * lets OpenClaw's fallback policy route to PI transparently.
 *
 * TODO(colony): The reasoning capability is now advertised by the
 * sidecar when ``ReasoningLoop`` is wired. The harness will start
 * advertising support automatically via the capability probe once
 * the sidecar reports ``"reasoning"`` in its capabilities list.
 */
function agentHarness(
  ctx: ColonyPluginContext,
  caps: ReturnType<typeof capabilityProbe>,
  logger?: { warn(m: string): void; info?(m: string): void },
): AgentHarness {
  const supports = (sctx: AgentHarnessSupportContext): AgentHarnessSupport => {
    if (!ctx.config.ownReasoningLoop) {
      return {
        supported: false,
        reason: "colony: ownReasoningLoop=false",
      };
    }
    const rt = sctx.requestedRuntime;
    if (rt !== "colony" && rt !== "auto") {
      return {
        supported: false,
        reason: `colony: requestedRuntime=${String(rt)}`,
      };
    }
    const snap = caps.snapshot();
    if (!snap.probed) {
      // First-call miss: kick the probe in the background so the next
      // turn sees the resolved capability set. ``kick()`` is reentrant.
      void caps.kick().catch(() => {
        /* capabilityProbe swallows errors internally */
      });
      return {
        supported: false,
        reason: "colony: capabilities not yet probed",
      };
    }
    if (!snap.caps.has("reasoning")) {
      return {
        supported: false,
        reason: "colony: sidecar does not advertise 'reasoning' capability",
      };
    }
    return { supported: true, priority: 50 };
  };

  const runAttempt = async (
    params: AgentHarnessAttemptParams,
  ): Promise<AgentHarnessAttemptResult> => {
    const sessionId = params.sessionId;

    // Pre-check: honour a pre-aborted signal without making any call.
    if (params.abortSignal?.aborted) {
      return {
        ...baseAttemptResult(sessionId),
        aborted: true,
        externalAbort: true,
      };
    }

    // KNOWN GAP: Colony's ``HostTurnContext`` requires a single
    // ``contact_id``; OpenClaw's attempt params carry several candidate
    // identifiers but no canonical contact id. Prefer the most
    // user-stable value available, falling back to the session id so the
    // sidecar always has *some* stable routing key. Document this as
    // a follow-up under aevonix/colony-ai#7.
    const contactId =
      params.senderId ??
      params.senderUsername ??
      params.agentAccountId ??
      params.agentId ??
      `openclaw:${sessionId}`;

    const metadata: Record<string, unknown> = {
      session_key: params.sessionKey,
      agent_id: params.agentId,
      think_level: params.thinkLevel,
      reasoning_level: params.reasoningLevel,
    };
    for (const k of Object.keys(metadata)) {
      if (metadata[k] === undefined) delete metadata[k];
    }

    const modelOverride = `${params.provider}/${params.modelId}`;
    // ``ClientToolDefinition`` is the OpenResponses hosted-tools shape —
    // the caller-facing name lives at ``function.name``.
    const availableTools = (params.clientTools ?? []).map(
      (t) => t.function.name,
    );

    let res;
    try {
      res = await ctx.client.reasoningTurn(
        {
          identity: ctx.identity(),
          context: {
            session_id: sessionId,
            contact_id: contactId,
            channel_id: params.messageChannel,
            turn_id: params.runId,
            metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
          },
          messages: [{ role: "user", content: params.prompt }],
          available_tools:
            availableTools.length > 0 ? availableTools : undefined,
          model_override: modelOverride,
        },
        { signal: params.abortSignal },
      );
    } catch (err) {
      // Abort propagation: if the caller aborted mid-flight, prefer the
      // "aborted" shape even when fetch surfaces a transport error.
      if (params.abortSignal?.aborted) {
        return {
          ...baseAttemptResult(sessionId),
          aborted: true,
          externalAbort: true,
        };
      }

      if (err instanceof ColonyApiError) {
        // 501 / phase1_wiring_required — endpoint not wired on this
        // sidecar build. Promote to promptError so OpenClaw's harness
        // fallback can route to PI.
        if (err.code === "phase1_wiring_required" || err.status === 501) {
          logger?.warn(
            `[colony] reasoning.turn: sidecar returned ${err.code} (${err.status}) — falling back`,
          );
          return {
            ...baseAttemptResult(sessionId),
            promptError: "colony: reasoning endpoint not wired (501)",
            promptErrorSource: "prompt",
          };
        }
        if (err.status >= 500 && err.status < 600) {
          logger?.warn(
            `[colony] reasoning.turn: sidecar ${err.status} ${err.code} — falling back`,
          );
          return {
            ...baseAttemptResult(sessionId),
            promptError: `colony: sidecar error (${err.status} ${err.code})`,
            promptErrorSource: "prompt",
          };
        }
        // 4xx — NOTE: this differs from context-engine's behaviour
        // (which re-throws). For the harness, returning a shaped error
        // lets OpenClaw's harness-selection fallback kick in; throwing
        // would abort the entire turn instead of routing to PI.
        logger?.warn(
          `[colony] reasoning.turn: contract error ${err.status} ${err.code} — falling back`,
        );
        return {
          ...baseAttemptResult(sessionId),
          promptError: `colony: contract error (${err.status} ${err.code})`,
          promptErrorSource: "prompt",
        };
      }
      // Network / transport / timeout — treat as fallback.
      logger?.warn(
        `[colony] reasoning.turn: transport error — falling back (${String(err)})`,
      );
      return {
        ...baseAttemptResult(sessionId),
        promptError: `colony: transport error: ${String(err)}`,
        promptErrorSource: "prompt",
      };
    }

    // Success path: translate Colony's response into a fully-populated
    // EmbeddedRunAttemptResult.
    const text = res.message?.content ?? "";
    const toolCalls = (res.tool_calls ?? []).map((tc) => ({
      type: "toolCall" as const,
      id: tc.id,
      name: tc.name,
      arguments: tc.arguments as Record<string, unknown>,
    }));

    const content: AssistantMessageContent[] = [];
    if (text.length > 0) {
      content.push({ type: "text", text });
    }
    for (const tc of toolCalls) {
      content.push(tc);
    }

    const timestamp = Date.now();
    // KNOWN GAP: ``api`` and ``provider`` below are best-effort — Colony
    // does not report which API dialect it used, so we echo the caller's
    // model metadata. OpenClaw treats this as a hint, not a promise.
    const assistant: AssistantMessageLike = {
      role: "assistant",
      content,
      api: params.model.api,
      provider: params.model.provider,
      model: params.modelId,
      usage: zeroAssistantUsage(),
      stopReason: toolCalls.length > 0 ? "toolUse" : "stop",
      timestamp,
    };

    const userMsg: UserMessageLike = {
      role: "user",
      content: params.prompt,
      timestamp,
    };

    const attemptUsage = normalizeUsage(
      res.usage as Parameters<typeof normalizeUsage>[0],
    );

    return {
      ...baseAttemptResult(sessionId),
      messagesSnapshot: [userMsg, assistant] as AgentHarnessAttemptResult["messagesSnapshot"],
      assistantTexts: text.length > 0 ? [text] : [],
      lastAssistant: assistant as AgentHarnessAttemptResult["lastAssistant"],
      currentAttemptAssistant:
        assistant as AgentHarnessAttemptResult["lastAssistant"],
      attemptUsage,
    };
  };

  return {
    id: "colony",
    label: "Colony reasoning harness",
    pluginId: PLUGIN_ID,
    supports,
    runAttempt,
    // ``reset`` / ``dispose`` are no-ops today but are declared so
    // forward-compat extensions (per-session sidecar bindings, shared
    // fetch pools) can attach cleanup here without touching the
    // registration call site.
    reset: async (_params): Promise<void> => {
      // intentional no-op — Colony does not bind per-session sidecar state
    },
    dispose: async (): Promise<void> => {
      // intentional no-op — ``ColonySidecarClient`` has no shared handles
    },
    // ``compact`` deliberately omitted: Colony has no transcript-
    // compaction endpoint. The harness registry checks ``compact?`` and
    // skips when absent, so omitting is safe and correct.
  };
}

/**
 * Structural shape of ``AssistantMessage.content[n]``. Declared here to
 * avoid pulling ``@mariozechner/pi-ai`` as a direct dependency just for
 * one content-union type; fields match the pi-ai public type.
 */
type AssistantMessageContent =
  | { type: "text"; text: string }
  | {
      type: "toolCall";
      id: string;
      name: string;
      arguments: Record<string, unknown>;
    };

/**
 * Structural shape of ``AssistantMessage``. We construct the value
 * here and cast it at the boundary onto
 * ``AgentHarnessAttemptResult["lastAssistant"]`` so the rest of the
 * adapter stays free of external dependency imports.
 */
type AssistantMessageLike = {
  role: "assistant";
  content: AssistantMessageContent[];
  api: string;
  provider: string;
  model: string;
  usage: ReturnType<typeof zeroAssistantUsage>;
  stopReason: "stop" | "toolUse";
  timestamp: number;
};

type UserMessageLike = {
  role: "user";
  content: string;
  timestamp: number;
};

/**
 * Structural mirrors of the OpenClaw SDK lifecycle-hook event / context /
 * result types this plugin consumes. These match the authoritative
 * shapes declared in the SDK at
 *
 *  - ``openclaw/dist/plugin-sdk/src/plugins/hook-message.types.d.ts``
 *    (``PluginHookMessageSendingEvent``, ``PluginHookMessageContext``,
 *    ``PluginHookMessageSendingResult``)
 *  - ``openclaw/dist/plugin-sdk/src/plugins/hook-types.d.ts``
 *    (``PluginHookReplyDispatchEvent``,
 *    ``PluginHookReplyDispatchContext``)
 *  - ``openclaw/dist/plugin-sdk/src/auto-reply/templating.d.ts``
 *    (``FinalizedMsgContext``, which is ``Omit<MsgContext,
 *    "CommandAuthorized"> & { CommandAuthorized: boolean }``)
 *
 * Why a local mirror instead of an import: none of these types are
 * re-exported from any subpath in the ``openclaw`` package's
 * ``package.json`` ``exports`` map. ``plugin-sdk/plugin-entry``,
 * ``plugin-sdk/core`` and ``plugin-sdk`` (the index) together expose
 * ``OpenClawPluginApi`` and ``PluginHookReplyDispatch{Event,Context}``,
 * but NOT the ``message_sending`` trio. Importing from
 * ``openclaw/dist/plugin-sdk/src/plugins/*`` reaches into a private
 * path that isn't part of the published surface.
 *
 * ``tsc`` still keeps us honest: ``api.on("message_sending", …)`` and
 * ``api.on("reply_dispatch", …)`` are typed against the SDK's real
 * ``PluginHookHandlerMap``, and if the mirrors drift from the SDK
 * shapes the registration call sites will fail to compile. We relied on
 * exactly that drift-check when rewriting these adapters.
 *
 * If a future ``openclaw`` release publishes these types under a stable
 * subpath, collapse the mirrors down to re-exports.
 */
interface PluginHookMessageContextMirror {
  channelId: string;
  accountId?: string;
  conversationId?: string;
}

interface PluginHookMessageSendingEventMirror {
  to: string;
  content: string;
  metadata?: Record<string, unknown>;
}

interface PluginHookMessageSendingResultMirror {
  content?: string;
  cancel?: boolean;
}

/**
 * Minimal slice of ``FinalizedMsgContext`` the post-turn adapter reads.
 * The full type has dozens of optional fields; we pull only the ones
 * we actually project onto a ``HostTurnContext``. All listed fields
 * match the optionality declared in
 * ``auto-reply/templating.d.ts::MsgContext``.
 */
interface FinalizedMsgContextSlice {
  Body?: string;
  BodyForAgent?: string;
  RawBody?: string;
  From?: string;
  To?: string;
  SessionKey?: string;
  AccountId?: string;
  SenderId?: string;
  SenderName?: string;
  Provider?: string;
  Surface?: string;
}

interface PluginHookReplyDispatchEventMirror {
  ctx: FinalizedMsgContextSlice;
  runId?: string;
  sessionKey?: string;
  inboundAudio: boolean;
  sessionTtsAuto?: string;
  ttsChannel?: string;
  suppressUserDelivery?: boolean;
  shouldRouteToOriginating: boolean;
  originatingChannel?: string;
  originatingTo?: string;
  shouldSendToolSummaries: boolean;
  sendPolicy: "allow" | "deny";
  isTailDispatch?: boolean;
}

/**
 * Structural mirror of `PluginHookLlmOutputEvent` from the OpenClaw SDK.
 * See `openclaw/dist/plugin-sdk/src/plugins/hook-types.d.ts` for the
 * authoritative shape. Imported locally because the type is not
 * re-exported from any stable subpath.
 */
interface PluginHookLlmOutputEventMirror {
  runId: string;
  sessionId: string;
  provider: string;
  model: string;
  assistantTexts: string[];
  lastAssistant?: unknown;
  usage?: {
    input?: number;
    output?: number;
    cacheRead?: number;
    cacheWrite?: number;
    total?: number;
  };
}

/**
 * Structural mirror of `PluginHookAgentContext` from the OpenClaw SDK.
 */
interface PluginHookAgentContextMirror {
  runId?: string;
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  workspaceDir?: string;
  modelProviderId?: string;
  modelId?: string;
  messageProvider?: string;
  trigger?: string;
  channelId?: string;
}

/**
 * Structural mirror of `PluginHookMessageReceivedEvent` from the
 * OpenClaw SDK. See `hook-message.types.d.ts` for the authoritative
 * shape.
 */
interface PluginHookMessageReceivedEventMirror {
  from: string;
  content: string;
  timestamp?: number;
  metadata?: Record<string, unknown>;
}

/**
 * Only the fields the adapter actually touches. The real SDK context
 * carries ``cfg``, ``dispatcher``, ``abortSignal``, ``onReplyStart``,
 * ``recordProcessed``, ``markIdle`` — all of which we ignore because
 * we're observer-only. Using ``unknown`` for each keeps the mirror from
 * pulling in the full OpenClaw config + dispatcher type graph.
 */
interface PluginHookReplyDispatchContextMirror {
  cfg: unknown;
  dispatcher: unknown;
  abortSignal?: AbortSignal;
  onReplyStart?: () => Promise<void> | void;
  recordProcessed: (
    outcome: "completed" | "skipped" | "error",
    opts?: { reason?: string; error?: string },
  ) => void;
  markIdle: (reason: string) => void;
}

type MessageSendingEvent = PluginHookMessageSendingEventMirror;
type MessageSendingContext = PluginHookMessageContextMirror;
type MessageSendingResult = PluginHookMessageSendingResultMirror;
type ReplyDispatchEvent = PluginHookReplyDispatchEventMirror;
type ReplyDispatchContext = PluginHookReplyDispatchContextMirror;
type LlmOutputEvent = PluginHookLlmOutputEventMirror;
type LlmOutputContext = PluginHookAgentContextMirror;
type MessageReceivedEvent = PluginHookMessageReceivedEventMirror;
type MessageReceivedContext = PluginHookMessageContextMirror;

/**
 * Safety hook — runs on every outbound ``message_sending`` event and
 * decides whether to cancel the chunk. Priority 100 (set at registration
 * time) ensures this runs before any other plugin's content-rewrite hook
 * can mutate ``event.content``.
 *
 * Degradation policy is deliberately hand-rolled (not via
 * ``withDegradation``): ``withDegradation`` defaults to *allow* on
 * failure, which is the opposite of the fail-closed default safety
 * requires. The handler also MUST NOT throw — OpenClaw treats
 * hook-handler exceptions as pass-through, which would subvert
 * ``failSafetyClosed=true``. Always translate to an explicit
 * ``{ cancel: true }`` or ``undefined`` return.
 *
 * KNOWN GAPS (tracked as follow-ups under aevonix/colony-ai#7 Phase 7+):
 *
 *  - ``incoming_message_text`` is passed as ``""`` — the sending hook
 *    has no access to the triggering inbound message. Wiring the
 *    ``inbound_claim`` / ``message_received`` hook to cache per-session
 *    inbound text is future work.
 *  - ``session_id`` is a heuristic surrogate derived from
 *    ``channelId:conversationId`` (or ``channelId:event.to`` when no
 *    conversation id is available). OpenClaw's hook surface intentionally
 *    does not carry a session key at message-send time.
 *  - ``contact_id`` is chained through ``accountId → conversationId →
 *    event.to → "unknown"`` — same reason.
 *  - ``trust_tier`` is left ``undefined`` so the sidecar applies its
 *    server-side default (REGULAR).
 *  - ``mentioned_entities`` is always ``[]`` — we don't have an NLP
 *    entity-extraction pass on the outbound content yet.
 */
function safetyHook(
  ctx: ColonyPluginContext,
  caps: ReturnType<typeof capabilityProbe>,
  cache: SessionTextCache,
  logger?: OpenClawPluginApi["logger"],
) {
  return async (
    event: MessageSendingEvent,
    hookCtx: MessageSendingContext,
  ): Promise<MessageSendingResult | void> => {
    // Capability short-circuit: when the sidecar probe succeeded AND
    // it reported no ``response_gate`` capability, skip the call entirely. If
    // the probe failed (state: unknown) we still call the endpoint so
    // ``failSafetyClosed`` can make the right decision — silently
    // no-oping when the sidecar is temporarily unreachable would
    // subvert the fail-closed default.
    const hasGate = await caps.has("response_gate");
    const probeOk = await caps.hasProbedSuccessfully();
    if (!hasGate && probeOk) {
      return; // pass-through — sidecar affirmatively has no response gate
    }

    // Best-effort identity mapping for Colony. The hook surface only
    // carries channel-level scope (``channelId``, optional ``accountId``
    // and ``conversationId``); we synthesise a ``session_id`` from those
    // so the sidecar has *some* stable routing key.
    const session_id = hookCtx.conversationId
      ? `${hookCtx.channelId}:${hookCtx.conversationId}`
      : `${hookCtx.channelId}:${event.to}`;
    const contact_id =
      hookCtx.accountId ?? hookCtx.conversationId ?? event.to ?? "unknown";

    try {
      const res = await ctx.client.safetyCheck({
        identity: ctx.identity(),
        context: {
          session_id,
          contact_id,
          channel_id: hookCtx.channelId,
        },
        response_text: event.content,
        // Read cached inbound text from the `message_received` hook.
        // Key must match what messageReceivedHook uses. We rely on
        // `conversationId` as the primary key since it's stable across
        // both hooks. Without it, the `to`/`from` identifiers may not
        // align, so the fallback is best-effort.
        incoming_message_text: cache.getInbound(
          hookCtx.conversationId ?? "",
        ),
        target_gateway: hookCtx.channelId,
        // ``trust_tier`` intentionally undefined — server defaults to
        // REGULAR. Populate here once OpenClaw exposes a trust-tier
        // signal on the hook surface.
        trust_tier: undefined,
        mentioned_entities: [],
      });

      if (res.blocked || res.decision === "block") {
        // Log the human-readable reason here — the SDK strips any
        // ``reason`` field off ``PluginHookMessageSendingResult``, so we
        // can only communicate the reason via logs.
        logger?.warn(
          `[colony.safety] blocked chunk to ${event.to}: ${res.reason ?? "<no reason>"}`,
        );
        return { cancel: true };
      }

      if (res.decision === "pending") {
        // Colony has a "pending — hold for review" decision that doesn't
        // map cleanly onto the SDK's immediate allow/cancel semantics;
        // the chunk is cancelled (safe direction) but surfaced on a
        // distinct log tag with layer/reason metadata so operators can
        // tell held messages apart from outright blocks until a proper
        // re-send queue lands.
        logger?.warn(
          `[colony.safety.held] held for review — to=${event.to} layer=${res.blocking_layer ?? "?"} reason=${res.reason ?? "<no reason>"}`,
        );
        return { cancel: true };
      }

      return; // pass: allow the chunk through
    } catch (err) {
      logger?.warn(`[colony.safety] safetyCheck errored: ${String(err)}`);
      // ``ColonyApiError`` (including 501 / phase1_wiring_required) is
      // handled the same as any other transport failure: honour
      // ``failSafetyClosed``. This is deliberately asymmetric from
      // ``withDegradation``'s allow-on-failure default — safety is the
      // one place where the conservative direction is *cancel*.
      if (ctx.config.failSafetyClosed) {
        return { cancel: true };
      }
      return; // fail-open — operator has explicitly opted out
    }
  };
}

/**
 * `message_received` hook — caches inbound text per session so the
 * safety hook can read the triggering user message instead of passing
 * an empty string.
 *
 * This is an observer-only handler: it never returns a result that
 * could modify or cancel the inbound message. Errors are logged and
 * swallowed so they don't disrupt message delivery.
 */
function messageReceivedHook(
  cache: SessionTextCache,
  logger?: OpenClawPluginApi["logger"],
) {
  return (
    event: MessageReceivedEvent,
    hookCtx: MessageReceivedContext,
  ): void => {
    try {
      // Derive a stable session key matching the safety hook's key
      // derivation. Both hooks must use the same formula so cached
      // inbound text is findable by the safety hook.
      // Safety hook uses `event.to`; here we use `event.from` which is
      // the same person in a 1:1 chat. For group chats, conversationId
      // is the stable key.
      const sessionKey =
        hookCtx.conversationId ??
        `${hookCtx.channelId}:${event.from}`;
      cache.setInbound(sessionKey, event.content);
    } catch (err) {
      logger?.warn(
        `[colony.messageReceived] error caching inbound text: ${String(err)}`,
      );
    }
  };
}

/**
 * `llm_output` hook — caches assistant text per session so the
 * post-turn hook can extract topics/entities/summary from the full
 * turn context, and so the signals ingest includes the outgoing
 * message.
 *
 * This is an observer-only handler: it never returns a result that
 * could modify or cancel the output. Errors are logged and swallowed.
 */
function llmOutputHook(
  cache: SessionTextCache,
  ctx: ColonyPluginContext,
  caps: ReturnType<typeof capabilityProbe>,
  logger?: OpenClawPluginApi["logger"],
) {
  return async (
    event: LlmOutputEvent,
    hookCtx: LlmOutputContext,
  ): Promise<void> => {
    try {
      const sessionKey =
        hookCtx.sessionKey ?? hookCtx.sessionId ?? event.sessionId ?? "unknown";

      // Cache assistant texts for the extraction pipeline to read.
      if (event.assistantTexts.length > 0) {
        cache.setAssistant(sessionKey, event.assistantTexts);
      }

      // Send the assistant output to Colony's signal ingestion so the
      // cognition pipeline has the full turn (inbound + outbound).
      const wantSignals = await caps.has("signals");
      if (wantSignals || !(await caps.hasProbedSuccessfully())) {
        await withDegradation(
          { name: "llm_output.signalsIngest", logger },
          () =>
            ctx.client.signalsIngest({
              identity: ctx.identity(),
              context: {
                session_id: sessionKey,
                contact_id: "unknown", // best-effort; no contact info on llm_output
                channel_id: hookCtx.channelId,
                turn_id: event.runId,
              },
              outgoing_message: {
                role: "assistant",
                content: event.assistantTexts.join("\n"),
              },
            }),
          () => undefined,
        );
      }
    } catch (err) {
      logger?.warn(
        `[colony.llmOutput] error in hook: ${String(err)}`,
      );
    }
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
  // Cached result set, populated synchronously the moment ``load()``
  // resolves. ``snapshot()`` reads this without awaiting so
  // ``AgentHarness.supports()`` — which must be synchronous per the SDK
  // contract — can still consult probe state.
  let capsSnapshot: ReadonlySet<string> = new Set<string>();

  const load = async (): Promise<ReadonlySet<string>> => {
    try {
      const health = await ctx.client.health();
      const set = new Set(health.capabilities);
      capsSnapshot = set;
      lastProbeSucceeded = true;
      return set;
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
    /**
     * Synchronous view of the probe's current state. Returns
     * ``probed: false`` with an empty ``caps`` set until a probe has
     * completed successfully at least once; on success ``caps`` is the
     * most recently observed ``/v1/host/health.capabilities`` set.
     *
     * The ``AgentHarness.supports`` contract forbids awaits, so
     * ``supports()`` must kick off the probe in the background (via a
     * discard-await on ``has()``) and rely on the next turn seeing the
     * populated set.
     */
    snapshot(): { probed: boolean; caps: ReadonlySet<string> } {
      return { probed: lastProbeSucceeded, caps: capsSnapshot };
    },
    /**
     * Begin loading capabilities if no load is in-flight. Returns the
     * in-flight (or freshly started) promise so callers that want to
     * ``void``-await it from a synchronous context can still attach
     * error handlers if needed. Reentrant — second call during a load
     * returns the same promise.
     */
    kick(): Promise<ReadonlySet<string>> {
      if (capsPromise === null) {
        capsPromise = load();
      }
      return capsPromise;
    },
    reset(): void {
      capsPromise = null;
      lastProbeSucceeded = false;
      capsSnapshot = new Set<string>();
    },
  };
}

/**
 * Post-turn hook — runs on ``reply_dispatch``. This is an observer-only
 * adapter: we call ``/v1/host/signals/ingest`` and (conditionally)
 * ``/v1/host/turns/sync`` to mirror the turn into Colony's cognition
 * store, then return ``undefined`` so OpenClaw's default dispatcher
 * runs. Returning a ``PluginHookReplyDispatchResult`` would take over
 * dispatch — we deliberately don't.
 *
 * The SDK's ``PluginHookReplyDispatchEvent`` (see
 * ``openclaw/dist/plugin-sdk/src/plugins/hook-types.d.ts``) carries the
 * inbound ``FinalizedMsgContext`` but no outgoing assistant text. The
 * previous scaffold pretended fields like ``outgoingMessage``,
 * ``topics``, ``entities`` were on the event — they aren't. Phase 6
 * ships with those fields empty/undefined on the outbound request; the
 * real extraction is Phase 7+ work (see TODOs below).
 *
 * CRITICAL: this handler MUST NOT throw. OpenClaw propagates hook
 * exceptions; the previous scaffold re-threw one rejected branch via
 * ``throw r.reason`` which would abort the entire reply dispatch. We
 * now log-and-swallow: ``Promise.allSettled`` collects both outcomes
 * and the handler always resolves ``void``.
 *
 * KNOWN GAPS (tracked under aevonix/colony-ai#7 Phase 7+):
 *
 *  - ``outgoing_message`` is omitted — the reply-dispatch hook fires
 *    *after* the model's text has already been handed to the dispatcher
 *    but the event payload doesn't carry it back. Capturing the
 *    assistant text requires an ``llm_output`` hook correlated by
 *    ``runId``.
 *  - ``channel_id`` falls through ``Provider → Surface →
 *    originatingChannel`` to give the sidecar *some* stable channel
 *    label; none of these are a perfect match for colony's
 *    ``channel_id`` vocabulary.
 */
function postTurnHook(
  ctx: ColonyPluginContext,
  caps: ReturnType<typeof capabilityProbe>,
  cache: SessionTextCache,
  extraction: TurnExtractionPipeline,
  logger?: OpenClawPluginApi["logger"],
) {
  return async (
    event: ReplyDispatchEvent,
    _hookCtx: ReplyDispatchContext,
  ): Promise<void> => {
    // Skip turns where the dispatcher has been told to deny, and skip
    // ACP tail-dispatch turns — neither carries new cognition state to
    // sync.
    if (event.sendPolicy === "deny") return;
    if (event.isTailDispatch === true) return;

    const msgCtx = event.ctx;
    const sessionKey =
      event.sessionKey ?? msgCtx.SessionKey ?? event.runId ?? "unknown";

    // Run extraction on the cached turn text.
    const inboundText =
      msgCtx.BodyForAgent ?? msgCtx.Body ?? msgCtx.RawBody ?? cache.getInbound(sessionKey);
    const assistantText = cache.getCombinedAssistant(sessionKey);
    const extracted = extraction.extract(inboundText, assistantText);

    const turnCtx = {
      session_id: sessionKey,
      contact_id:
        msgCtx.SenderId ?? msgCtx.From ?? msgCtx.AccountId ?? "unknown",
      channel_id:
        msgCtx.Provider ?? msgCtx.Surface ?? event.originatingChannel,
      turn_id: event.runId,
      metadata: {
        inbound_audio: event.inboundAudio,
        send_policy: event.sendPolicy,
        suppress_user_delivery: event.suppressUserDelivery ?? false,
      },
    };

    const wantSignals = await caps.has("signals");
    const wantTurnSync = await caps.has("turn_sync");
    const probeFailed = !(await caps.hasProbedSuccessfully());

    const calls: Array<Promise<unknown>> = [];

    // Probe-failed state: call both endpoints best-effort so a
    // momentarily-unreachable sidecar doesn't silently skip the turn
    // forever. On success the capability check gates each call.
    if (wantSignals || probeFailed) {
      calls.push(
        ctx.client.signalsIngest({
          identity: ctx.identity(),
          context: turnCtx,
          incoming_message: inboundText
            ? { role: "user", content: inboundText }
            : undefined,
          outgoing_message: assistantText
            ? { role: "assistant", content: assistantText }
            : undefined,
        }),
      );
    }
    if (wantTurnSync || probeFailed) {
      calls.push(
        ctx.client.turnsSync({
          identity: ctx.identity(),
          context: turnCtx,
          topics: extracted.topics,
          entities: extracted.entities,
          pending_tasks: extracted.pending_tasks,
          tools_used: extracted.tools_used,
          summary: extracted.summary || undefined,
        }),
      );
    }

    // Fire-and-log: never throw out of an observer-only hook. Each
    // rejection is logged so operators can diagnose, but the handler
    // always resolves ``void`` — OpenClaw's default dispatcher must
    // not see our bookkeeping failure as a dispatch failure.
    const results = await Promise.allSettled(calls);
    for (const r of results) {
      if (r.status === "rejected") {
        logger?.warn(`[colony.postTurn] call failed: ${String(r.reason)}`);
      }
    }
    return;
  };
}

function eventsLifecycleService(
  ctx: ColonyPluginContext,
  api: OpenClawPluginApi,
  logger?: OpenClawPluginApi["logger"],
  toolRegistrar?: ToolRegistrarHandle,
) {
  let subscription: { close: () => void } | null = null;
  let lastEventTimestamp: string | undefined;

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
        subscription = ctx.client.openEvents(
          (event: HostEvent) => {
            // Track the timestamp of every event for reconnect replay
            const ts = (event as unknown as Record<string, unknown>).occurred_at as string | undefined;
            if (ts) lastEventTimestamp = ts;

            // Ignore replay_complete frames (these are journal replays, not live events)
            if (event.type === ("replay_complete" as HostEventType)) return;

            // Surface the event via OpenClaw's logger so operators see
            // the cognition stream in plugin diagnostics.
            try {
              logger?.info(`[colony.event] ${summarizeHostEvent(event)}`);
            } catch (cbErr) {
              logger?.warn(
                `[colony] events: callback error on ${event.type} (${String(cbErr)})`,
              );
            }

          // Proactive_message is the only event that spawns a subagent
          // turn — handled inline because it needs the OpenClaw API.
          if (event.type === "proactive_message") {
            handleProactiveMessage(event, api, logger).catch((err) => {
              logger?.warn(`[colony] proactive delivery failed: ${String(err)}`);
            });
            return;
          }

          // Every other declared event type goes through the dispatcher,
          // which updates the cache-invalidation bus and runs any
          // attached hooks (e.g. skill_draft_approved → tool refresh).
          try {
            dispatchHostEvent(event, {
              cache: ctx.cache,
              logger,
              onSkillApproved: toolRegistrar
                ? async () => {
                    await toolRegistrar.refreshSkillTools();
                  }
                : undefined,
            });
          } catch (dispatchErr) {
            logger?.warn(`[colony] events: dispatch error on ${event.type} (${String(dispatchErr)})`);
          }
        },
        lastEventTimestamp,
        (code, reason) => {
          logger?.warn(`[colony] events: WebSocket closed (code=${code} reason=${reason}) — proactive deliveries disabled until reconnect`);
          subscription = null;
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

export function createColonyPlugin(): unknown {
  return definePluginEntry({
    id: PLUGIN_ID,
    name: PLUGIN_NAME,
    description: PLUGIN_DESCRIPTION,
    configSchema: ColonyPluginConfigSchema,
    register(api: OpenClawPluginApi) {
      const ctx = buildContext(api);
      const caps = capabilityProbe(ctx);
      const cache = new SessionTextCache();
      const extraction = new TurnExtractionPipeline();

      // As of issue aevonix/colony-ai#7 Phase 6 all adapter shapes
      // match their real OpenClaw SDK contracts — every Phase 1–6
      // ``@ts-expect-error`` marker is gone. Follow-up work (Phase 7+)
      // is all *content* (extractors, cross-hook state, identity
      // surrogates), not adapter-shape wiring.

      if (ctx.config.ownMemoryCapability) {
        api.registerMemoryCapability(memoryCapability(ctx, caps));
      }

      // Register Colony's native tools + active skills as first-class
      // OpenClaw tools. The registrar returns a handle the events
      // lifecycle service uses to refresh when skill_draft_approved
      // arrives over the WS.
      const memoryManagerForWrites = new ColonyMemorySearchManager(ctx, caps);
      const toolRegistrar = registerColonyTools(
        ctx,
        api,
        memoryManagerForWrites,
      );

      api.registerService(
        eventsLifecycleService(ctx, api, api.logger, toolRegistrar),
      );

      // #7 Phase 3 — ``memoryEmbeddingProvider`` returns the real
      // ``MemoryEmbeddingProviderAdapter`` shape (see its doc comment),
      // so no ``@ts-expect-error`` is needed here.
      api.registerMemoryEmbeddingProvider(memoryEmbeddingProvider(ctx));

      if (ctx.config.ownContextEngine) {
        // #7 Phase 4 — ``contextEngineFactory`` implements the real
        // ``ContextEngine`` contract (see its doc comment), so no
        // ``@ts-expect-error`` is needed here.
        api.registerContextEngine(
          "colony",
          contextEngineFactory(ctx, caps, api.logger),
        );
      }

      if (ctx.config.ownReasoningLoop) {
        // #7 Phase 5 — ``agentHarness`` implements the real
        // ``AgentHarness`` contract (see its doc comment), so no
        // ``@ts-expect-error`` is needed here.
        api.registerAgentHarness(agentHarness(ctx, caps, api.logger));
      }

      // #7 Phase 6 — ``safetyHook`` and ``postTurnHook`` now match the
      // real ``PluginHookHandlerMap[K]`` contract (see their doc
      // comments). ``message_sending`` is wired via ``api.on(...)``
      // rather than ``api.registerHook(...)`` — ``registerHook`` takes
      // an ``InternalHookHandler`` for a different namespace
      // (``InternalHookEvent``), not the lifecycle-hook map. Priority
      // 100 on ``message_sending`` ensures the safety gate runs before
      // any other plugin's content-rewrite hook can mutate the chunk.
      api.on("message_sending", safetyHook(ctx, caps, cache, api.logger), {
        priority: 100,
      });
      api.on("reply_dispatch", postTurnHook(ctx, caps, cache, extraction, api.logger));

      // #7 Phase 6 — ``message_received`` caches inbound text for the
      // safety hook and extraction pipeline. ``llm_output`` caches
      // assistant text and forwards it to Colony's signal ingestion.
      api.on("message_received", messageReceivedHook(cache, api.logger));
      api.on("llm_output", llmOutputHook(cache, ctx, caps, api.logger));

      // Register internal initiative endpoint for proactive suggestions from Colony sidecar
      api.registerHttpRoute({
        path: "/internal/initiative",
        auth: "plugin",  // No gateway auth — we check our own
        match: "exact",
        handler: async (req, res) => {
          // Parse JSON body
          const bodyResult = await readJsonBodyWithLimit(req, { maxBytes: 64 * 1024 });
          if (!bodyResult.ok) {
            res.statusCode = 400;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: "Invalid request body" }));
            return true;
          }

          const { initiative, source, timestamp } = bodyResult.value as Record<string, unknown>;

          // Auth check
          const colonyApiKey = api.pluginConfig?.apiKey as string | undefined;
          const authHeader = req.headers["authorization"] as string | undefined;
          const presentedKey = authHeader?.startsWith("Bearer ") ? authHeader.slice(7) : null;

          if (presentedKey !== colonyApiKey) {
            res.statusCode = 401;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: "Unauthorized" }));
            return true;
          }

          // Validate initiative structure
          if (!initiative || typeof initiative !== "object") {
            res.statusCode = 400;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: "Missing initiative object" }));
            return true;
          }

          const init = initiative as Record<string, unknown>;

          // Format as readable text for LLM
          const text = formatInitiativeText(init);

          // Enqueue as system event in main session
          try {
            const enqueued = api.runtime.system.enqueueSystemEvent(text, {
              sessionKey: "main",
              contextKey: `colony:initiative:${init.id}`,
              trusted: true,
            });

            if (!enqueued) {
              api.logger.warn?.(`[colony] Duplicate initiative blocked: ${init.id}`);
            }

            res.statusCode = 200;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ ok: true, enqueued, initiativeId: init.id }));

            api.logger.info?.(`[colony] Initiative enqueued: ${init.id} (priority=${init.priority})`);
          } catch (err) {
            api.logger.error?.(`[colony] Failed to enqueue initiative: ${err}`);
            res.statusCode = 500;
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: String(err) }));
          }
          return true;
        }
      });

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

      // Bootstrap identity so ctx.identity() returns colony_id / node_id /
      // trust_tier once resolved. Fire-and-forget — the first few turns
      // may see a partial identity until the sidecar responds.
      (async () => {
        try {
          const snap = await ctx.refreshIdentity();
          if (snap.colony_id || snap.node_id) {
            api.logger.info(
              `[colony] identity resolved colony_id=${snap.colony_id ?? "?"} node_id=${snap.node_id ?? "?"} tier=${snap.trust_tier ?? "unset"}`,
            );
          }
          const verified = await ctx.verifyChain();
          if (verified.chain_valid) {
            api.logger.info(
              `[colony] chain verified${verified.signed_attestation ? " (signed attestation)" : ""}`,
            );
          }
        } catch {
          /* refreshIdentity / verifyChain never throw — belt-and-braces */
        }
      })();

      // Forward host LLM credentials if configured so the sidecar's
      // ReasoningLoop can talk to the provider without its own keys.
      if (ctx.config.hostLLM) {
        const { hostLLM } = ctx.config;
        ctx.client
          .configureHost(
            {
              provider: hostLLM.provider,
              api_key: hostLLM.apiKey,
              base_url: hostLLM.baseUrl,
              models: hostLLM.models ?? {},
            },
            ctx.identity(),
          )
          .then((res) =>
            api.logger.info(
              `[colony] host LLM configured provider=${res.provider ?? "?"}`,
            ),
          )
          .catch((err) =>
            api.logger.warn(
              `[colony] host LLM configure failed: ${String(err)}`,
            ),
          );
      }

      // --- Remote Agent WebSocket Connection (v0.7.0) ---
      // If this plugin is running as a remote agent (connected via `colony agent connect`),
      // establish WebSocket connection for initiative delivery.
      (async () => {
        try {
          const agentConfig = await loadAgentConfig();
          if (agentConfig?.connection_mode === "remote" && agentConfig.websocket_url) {
            api.logger.info?.(`[colony] Remote agent detected, connecting WebSocket to ${agentConfig.websocket_url}`);
            
            const remoteClient = new RemoteAgentClient(agentConfig, api.logger);
            
            // Handle initiatives pushed from Colony
            remoteClient.onInitiative = async (initiative: Initiative) => {
              api.logger.info?.(`[colony] Received initiative via WebSocket: ${initiative.id}`);
              
              // Format and enqueue as system event (same as HTTP path)
              const text = formatInitiativeText({
                id: initiative.id,
                type: initiative.type,
                priority: initiative.priority,
                title: "",  // Not in Initiative type
                description: initiative.description,
                rationale: "",  // Not in Initiative type
                suggested_action: "notify_user",
              });
              
              const enqueued = api.runtime.system.enqueueSystemEvent(text, {
                sessionKey: "main",
                contextKey: `colony:initiative:${initiative.id}`,
                trusted: true,
              });
              
              if (enqueued) {
                // Acknowledge receipt
                await remoteClient.acknowledge(initiative.id);
                api.logger.info?.(`[colony] Initiative ${initiative.id} acknowledged`);
              }
            };
            
            remoteClient.onDisconnect = (reason: string) => {
              api.logger.warn?.(`[colony] Remote agent WebSocket disconnected: ${reason}`);
            };
            
            remoteClient.onConnect = () => {
              api.logger.info?.(`[colony] Remote agent WebSocket connected`);
            };
            
            await remoteClient.connect();
          }
        } catch (err) {
          api.logger.warn?.(`[colony] Remote agent setup failed: ${String(err)}`);
        }
      })();
    },
  });
}

// Helper to format initiative as readable text for LLM
function formatInitiativeText(init: Record<string, unknown>): string {
  const lines = [
    `[colony_initiative]`,
    `ID: ${init.id ?? "unknown"}`,
    `Type: ${init.type ?? "unknown"}`,
    `Priority: ${init.priority ?? 0}`,
    `Title: ${init.title ?? "(no title)"}`,
    `Description: ${init.description ?? "(no description)"}`,
    `Rationale: ${init.rationale ?? "(no rationale)"}`,
    `Suggested action: ${init.suggested_action ?? "notify_user"}`,
  ];

  // Add context if present
  const ctx = init.context as Record<string, unknown> | undefined;
  if (ctx) {
    if (ctx.pending_tasks && Array.isArray(ctx.pending_tasks) && ctx.pending_tasks.length > 0) {
      const task = ctx.pending_tasks[0] as Record<string, unknown>;
      lines.push(`Context - Pending task: ${task.description ?? "unknown"} (${task.days_pending ?? 0} days pending)`);
    }
    if (ctx.neglected_contacts && Array.isArray(ctx.neglected_contacts) && ctx.neglected_contacts.length > 0) {
      const contact = ctx.neglected_contacts[0] as Record<string, unknown>;
      lines.push(`Context - Neglected contact: ${contact.name ?? contact.entity_id ?? "unknown"}`);
    }
    if (ctx.scheduling_opportunities && Array.isArray(ctx.scheduling_opportunities) && ctx.scheduling_opportunities.length > 0) {
      const opp = ctx.scheduling_opportunities[0] as Record<string, unknown>;
      lines.push(`Context - Scheduling: ${opp.description ?? "unknown"}`);
    }
  }

  return lines.join("\n");
}

// Re-export internals for the smoke tests / programmatic consumers.
export { ColonySidecarClient, ColonyApiError } from "./sidecar-client.js";
export type { ColonyPluginConfig } from "./config.js";
export {
  buildContext as __buildContext,
  memoryCapability as __memoryCapability,
  memoryEmbeddingProvider as __memoryEmbeddingProvider,
  contextEngineFactory as __contextEngineFactory,
  agentHarness as __agentHarness,
  safetyHook as __safetyHook,
  postTurnHook as __postTurnHook,
  capabilityProbe as __capabilityProbe,
  eventsLifecycleService as __eventsLifecycleService,
  messageReceivedHook as __messageReceivedHook,
  llmOutputHook as __llmOutputHook,
};
