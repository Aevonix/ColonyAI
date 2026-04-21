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
 * for a default export shaped by `definePluginEntry`.
 */
const _plugin = createColonyPlugin();
export default _plugin;
