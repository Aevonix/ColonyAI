import { vi } from "vitest";

// Node 18 lacks the ES2023 change-array-by-copy methods; openclaw's
// config-runtime calls Array.prototype.toSorted at import time, which
// otherwise crashes every suite that (transitively) imports plugin.ts.
// Test-only polyfill — production runs on Node 20+ where it's native.
/* eslint-disable @typescript-eslint/no-explicit-any */
if (typeof (Array.prototype as any).toSorted !== "function") {
  Object.defineProperty(Array.prototype, "toSorted", {
    value(this: unknown[], cmp?: (a: unknown, b: unknown) => number) {
      return [...this].sort(cmp);
    },
    writable: true,
    configurable: true,
  });
}
if (typeof (Array.prototype as any).toReversed !== "function") {
  Object.defineProperty(Array.prototype, "toReversed", {
    value(this: unknown[]) {
      return [...this].reverse();
    },
    writable: true,
    configurable: true,
  });
}
/* eslint-enable @typescript-eslint/no-explicit-any */

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
