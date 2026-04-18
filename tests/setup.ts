import { vi } from "vitest";

// Mock openclaw/plugin-sdk — peer dependency only available at runtime
vi.mock("openclaw/plugin-sdk", () => ({
  delegateCompactionToRuntime: vi.fn(() => ({ type: "delegate_compaction" })),
  AssembleResult: undefined,
  CompactResult: undefined,
  ContextEngine: undefined,
  ContextEngineFactory: undefined,
  ContextEngineInfo: undefined,
  IngestResult: undefined,
}));

vi.mock("openclaw/plugin-sdk/agent-harness", () => ({
  normalizeUsage: vi.fn((u) => u),
  AgentHarness: undefined,
  AgentHarnessAttemptParams: undefined,
  AgentHarnessAttemptResult: undefined,
  AgentHarnessSupport: undefined,
  AgentHarnessSupportContext: undefined,
}));
