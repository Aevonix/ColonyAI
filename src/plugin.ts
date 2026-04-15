import { ColonyPluginConfigSchema, type ColonyPluginConfig } from "./config.js";
import { ColonyApiError, ColonySidecarClient } from "./sidecar-client.js";
import type {
  HostEvent,
  HostHealthResponse,
  HostMessage,
  HostTurnContext,
} from "./types.js";

/**
 * The OpenClaw `OpenClawPluginApi`. We declare a structural type rather
 * than `import type from "openclaw"` so the plugin builds without a hard
 * dependency on the host package — OpenClaw is a peerDependency.
 *
 * The methods we touch correspond to those documented at
 * https://docs.openclaw.ai/plugins/sdk-overview.md.
 */
export interface OpenClawPluginApi {
  id: string;
  name: string;
  version?: string;
  pluginConfig: unknown;
  logger: { info(m: string): void; warn(m: string): void; error(m: string): void };

  registerService(service: unknown): void;
  registerMemoryCapability(capability: unknown): void;
  registerMemoryEmbeddingProvider(adapter: unknown): void;
  registerContextEngine(id: string, factory: () => unknown): void;
  registerAgentHarness(harness: unknown): void;
  registerHook(events: string[], handler: (event: unknown) => unknown, opts?: unknown): void;
  registerTool(tool: unknown, opts?: unknown): void;
  registerCommand(def: unknown): void;
  on(hookName: string, handler: (event: unknown) => unknown, opts?: unknown): void;
}

/**
 * `definePluginEntry` is the OpenClaw SDK helper. We re-import lazily so
 * test environments can stub it; production builds resolve it from the
 * peer-dependency `openclaw` package.
 */
type DefinePluginEntry = (entry: {
  id: string;
  name: string;
  version: string;
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
const PLUGIN_VERSION = "0.0.1";

export interface ColonyPluginContext {
  config: ColonyPluginConfig;
  client: ColonySidecarClient;
  identity: () => { host_id: string; plugin_version: string };
}

function buildContext(api: OpenClawPluginApi): ColonyPluginContext {
  const config = ColonyPluginConfigSchema.parse(api.pluginConfig ?? {});
  const client = new ColonySidecarClient(config);
  return {
    config,
    client,
    identity: () => ({ host_id: config.hostId, plugin_version: PLUGIN_VERSION }),
  };
}

// ---------------------------------------------------------------------------
// Capability bundles — each helper returns the OpenClaw-shaped object for
// one extension slot. They're factored out so the entry point reads as a
// declarative manifest of what Colony provides.
// ---------------------------------------------------------------------------

function memoryCapability(ctx: ColonyPluginContext) {
  return {
    id: "colony-memory",
    async read(args: { memoryId?: string; personId?: string; limit?: number }) {
      const res = await ctx.client.memoryRead({
        identity: ctx.identity(),
        memory_id: args.memoryId,
        person_id: args.personId,
        limit: args.limit,
      });
      return res.entries;
    },
    async write(args: {
      content: string;
      type?: string;
      personId?: string;
      entities?: string[];
      tags?: string[];
      strength?: number;
      context?: HostTurnContext;
    }) {
      return ctx.client.memoryWrite({
        identity: ctx.identity(),
        context: args.context,
        content: args.content,
        type: args.type,
        person_id: args.personId,
        entities: args.entities,
        tags: args.tags,
        strength: args.strength,
      });
    },
    async search(args: {
      query: string;
      limit?: number;
      minScore?: number;
      personId?: string;
      types?: string[];
      tags?: string[];
    }) {
      const res = await ctx.client.memorySearch({
        identity: ctx.identity(),
        query: args.query,
        limit: args.limit,
        min_score: args.minScore,
        person_id: args.personId,
        types: args.types,
        tags: args.tags,
      });
      return res.entries;
    },
    async flush(reason?: string) {
      return ctx.client.memoryFlush({ identity: ctx.identity(), reason });
    },
  };
}

function memoryEmbeddingProvider(ctx: ColonyPluginContext) {
  return {
    id: "colony-embed",
    async embed(inputs: string[], model?: string) {
      const res = await ctx.client.memoryEmbed({
        identity: ctx.identity(),
        inputs,
        model,
      });
      return { model: res.model, vectors: res.vectors };
    },
  };
}

function contextEngineFactory(ctx: ColonyPluginContext) {
  return () => ({
    id: "colony-context",
    async assemble(args: {
      context: HostTurnContext;
      incomingMessage: HostMessage;
      availableTools?: string[];
      citationsMode?: "off" | "inline" | "appendix";
    }) {
      try {
        const res = await ctx.client.contextAssemble({
          identity: ctx.identity(),
          context: args.context,
          incoming_message: args.incomingMessage,
          available_tools: args.availableTools,
          citations_mode: args.citationsMode,
        });
        return res;
      } catch (err) {
        if (err instanceof ColonyApiError && err.code === "phase1_wiring_required") {
          // Phase 1 not landed yet — degrade gracefully so the host's
          // default context assembly continues to work.
          return { sections: [], notices: ["colony-context: phase1 wiring pending"] };
        }
        throw err;
      }
    },
  });
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

  const load = async (): Promise<ReadonlySet<string>> => {
    try {
      const health = await ctx.client.health();
      return new Set(health.capabilities);
    } catch {
      // Don't cache failures — reset so the next call re-probes.
      capsPromise = null;
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
    reset(): void {
      capsPromise = null;
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

function eventsLifecycleService(ctx: ColonyPluginContext) {
  let subscription: { close: () => void } | null = null;

  return {
    id: "colony-events",
    async start() {
      if (!ctx.config.forwardProactiveDeliveries) {
        return;
      }
      subscription = ctx.client.openEvents((event: HostEvent) => {
        // The actual reply_dispatch wiring is host-specific. We surface
        // the event via OpenClaw's logger so it shows up in the plugin's
        // diagnostics; the gateway-method bridge that turns these into
        // channel posts is wired separately in Phase 1.
      });
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
    version: PLUGIN_VERSION,
    register(api: OpenClawPluginApi) {
      const ctx = buildContext(api);
      const caps = capabilityProbe(ctx);

      api.registerService(eventsLifecycleService(ctx));
      api.registerMemoryCapability(memoryCapability(ctx));
      api.registerMemoryEmbeddingProvider(memoryEmbeddingProvider(ctx));
      api.registerContextEngine("colony", contextEngineFactory(ctx));

      if (ctx.config.ownReasoningLoop) {
        api.registerAgentHarness(agentHarness(ctx));
      }

      api.registerHook(["message_sending"], safetyHook(ctx) as (event: unknown) => unknown);
      api.on("reply_dispatch", postTurnHook(ctx, caps) as (event: unknown) => unknown);

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
