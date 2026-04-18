/**
 * Mock for `openclaw/plugin-sdk` and `openclaw/plugin-sdk/agent-harness`.
 *
 * The real SDK is a peer dependency only available at runtime inside an
 * OpenClaw host process. Tests mock it out so they can run standalone.
 */

// --- Types (mirrors of SDK types) ---

export interface AssembleResult {
  context: string;
  addition?: string;
  notices?: string[];
}

export interface CompactResult {
  compacted: string;
}

export interface ContextEngine {
  assemble(input: unknown): Promise<AssembleResult>;
  compact(input: unknown): Promise<CompactResult>;
  ingest(input: unknown): Promise<IngestResult>;
}

export interface ContextEngineFactory {
  (api: unknown): ContextEngine;
}

export interface ContextEngineInfo {
  name: string;
  factory: ContextEngineFactory;
}

export interface IngestResult {
  accepted: boolean;
}

// --- Functions ---

export function delegateCompactionToRuntime(): unknown {
  return { type: "delegate_compaction" };
}

export function normalizeUsage(usage: unknown): unknown {
  return usage;
}

// --- Agent harness types and functions ---

export interface AgentHarness {
  name: string;
  supports(context: unknown): Promise<AgentHarnessSupport>;
  attempt(params: AgentHarnessAttemptParams): Promise<AgentHarnessAttemptResult>;
}

export interface AgentHarnessSupport {
  supported: boolean;
  priority?: number;
}

export interface AgentHarnessSupportContext {
  sessionKey?: string;
  channelId?: string;
  trigger?: string;
  modelProviderId?: string;
}

export interface AgentHarnessAttemptParams {
  sessionKey: string;
  messages: unknown[];
  tools?: unknown[];
  context?: string;
  modelOverride?: string;
  maxAttempts?: number;
}

export interface AgentHarnessAttemptResult {
  success: boolean;
  response?: string;
  usage?: unknown;
  error?: string;
}

// --- Plugin API (used by existing tests via vi.mock) ---

export interface OpenClawPluginApi {
  logger: {
    info: (...args: unknown[]) => void;
    warn: (...args: unknown[]) => void;
    error: (...args: unknown[]) => void;
    debug: (...args: unknown[]) => void;
  };
  getConfig(): Record<string, unknown>;
  on(event: string, handler: unknown, options?: unknown): void;
  registerContextEngine(name: string, factory: unknown): void;
  registerMemoryCapability(capability: unknown): void;
  registerAgentHarness(harness: unknown): void;
}

// Re-export everything for agent-harness subpath — no duplicates
