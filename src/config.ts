import { z } from "zod";

/**
 * Plugin configuration schema, validated by Zod the same way OpenClaw's
 * own plugin SDK validates `plugins.entries.<id>.config` blocks.
 *
 * Users put this under `plugins.entries.colony.config` in their OpenClaw
 * config:
 *
 *   "plugins": {
 *     "entries": {
 *       "colony": {
 *         "config": {
 *           "sidecarUrl": "http://127.0.0.1:7777",
 *           "apiKey": "sk-colony-...",
 *           "ownReasoningLoop": false
 *         }
 *       }
 *     }
 *   }
 */
export const ColonyPluginConfigSchema = z.object({
  /** Base URL of the colony sidecar (the FastAPI server). */
  sidecarUrl: z.string().url().default("http://127.0.0.1:7777"),

  /** Colony API key (sk-colony-...) — used as Authorization: Bearer. Optional for local dev. */
  apiKey: z.string().min(1).optional(),

  /**
   * If true, register Colony as the active agent harness. Off by default
   * because reasoning extraction is Phase 1 work; until then OpenClaw
   * drives reasoning and Colony provides memory + context + safety only.
   */
  ownReasoningLoop: z.boolean().default(false),

  /** Identity reported back to colony for audit/scoping. */
  hostId: z.string().default("openclaw"),

  /**
   * If true, register Colony as the active memory capability via
   * ``api.registerMemoryCapability``. Off by default because this is
   * an **exclusive slot** — enabling it claims memory from any other
   * memory plugin the host may already use.
   */
  ownMemoryCapability: z.boolean().default(false),

  /**
   * If true, register Colony as the active context engine via
   * ``api.registerContextEngine``. Off by default because this is
   * an **exclusive slot** — enabling it claims context assembly from
   * any other context engine plugin the host may already use.
   */
  ownContextEngine: z.boolean().default(false),

  /**
   * Whether to automatically connect to the events WebSocket and forward
   * proactive deliveries to OpenClaw via reply_dispatch.
   */
  forwardProactiveDeliveries: z.boolean().default(true),

  /**
   * If the safety sidecar errors or is unreachable, block the outbound
   * message ("fail closed") rather than letting it pass. Defaults to
   * ``true`` — safety-conscious by default. Operators running
   * sidecar-less smoke tests can set this to ``false`` to let messages
   * through when the sidecar is unavailable.
   */
  failSafetyClosed: z.boolean().default(true),

  /** Per-call HTTP timeout (ms). */
  requestTimeoutMs: z.number().int().positive().default(30_000),

  /**
   * Optional host LLM configuration forwarded to the sidecar at startup.
   * When present the plugin POSTs this to ``/v1/host/configure`` so the
   * sidecar can reason with the host's provider instead of requiring
   * its own credentials. Leave unset for sidecar-self-configured LLM.
   *
   * For security, API keys should be supplied via the
   * ``COLONY_HOST_LLM_API_KEY`` environment variable rather than
   * committed to plugin config files.
   */
  hostLLM: z
    .object({
      provider: z.string().min(1),
      apiKey: z.string().min(1).optional(),
      baseUrl: z.string().url().optional(),
      models: z
        .object({
          small: z.string().optional(),
          medium: z.string().optional(),
          large: z.string().optional(),
        })
        .partial()
        .optional(),
    })
    .optional(),
});

export type ColonyPluginConfig = z.infer<typeof ColonyPluginConfigSchema>;

/**
 * Fold environment-variable fallbacks into a parsed plugin config. The
 * plugin config schema doesn't know about process-level secrets — this
 * helper layers in ``COLONY_HOST_LLM_API_KEY`` / ``COLONY_HOST_LLM_BASE_URL``
 * so credentials can stay out of the committed config file.
 */
export function withHostLLMEnvOverrides(
  cfg: ColonyPluginConfig,
  env: NodeJS.ProcessEnv = process.env,
): ColonyPluginConfig {
  if (!cfg.hostLLM) return cfg;
  const apiKey = cfg.hostLLM.apiKey ?? env.COLONY_HOST_LLM_API_KEY;
  const baseUrl = cfg.hostLLM.baseUrl ?? env.COLONY_HOST_LLM_BASE_URL;
  return {
    ...cfg,
    hostLLM: {
      ...cfg.hostLLM,
      ...(apiKey ? { apiKey } : {}),
      ...(baseUrl ? { baseUrl } : {}),
    },
  };
}
