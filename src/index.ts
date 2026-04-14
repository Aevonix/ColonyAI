export { createColonyPlugin } from "./plugin.js";
export {
  ColonySidecarClient,
  ColonyApiError,
} from "./sidecar-client.js";
export {
  ColonyPluginConfigSchema,
  type ColonyPluginConfig,
} from "./config.js";
export type * from "./types.js";

import { createColonyPlugin } from "./plugin.js";

/**
 * Default export: the OpenClaw plugin entry. The OpenClaw loader looks
 * for a default export shaped by `definePluginEntry`, so we resolve and
 * re-export the promise here so users can simply do
 *
 *   "plugins": { "entries": { "colony": { "module": "@aevonix/openclaw-colony" } } }
 */
export default await createColonyPlugin();
