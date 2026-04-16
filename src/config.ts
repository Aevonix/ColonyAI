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
  /** Base URL of the colony-core sidecar (the FastAPI server). */
  sidecarUrl: z.string().url().default("http://127.0.0.1:7777"),

  /** Colony API key (sk-colony-...) — used as Authorization: Bearer. */
  apiKey: z.string().min(1),

  /**
   * If true, register Colony as the active agent harness. Off by default
   * because reasoning extraction is Phase 1 work; until then OpenClaw
   * drives reasoning and Colony provides memory + context + safety only.
   */
  ownReasoningLoop: z.boolean().default(false),

  /** Identity reported back to colony-core for audit/scoping. */
  hostId: z.string().default("openclaw"),

  /**
   * If true, register Colony as the active memory capability via
   * ``api.registerMemoryCapability``. Off by default because this is
   * an **exclusive slot** — enabling it claims memory from any other
   * memory plugin the host may already use.
   */
  ownMemoryCapability: z.boolean().default(false),

  /**
   * Whether to automatically connect to the events WebSocket and forward
   * proactive deliveries to OpenClaw via reply_dispatch.
   */
  forwardProactiveDeliveries: z.boolean().default(true),

  /** Per-call HTTP timeout (ms). */
  requestTimeoutMs: z.number().int().positive().default(30_000),
});

export type ColonyPluginConfig = z.infer<typeof ColonyPluginConfigSchema>;
