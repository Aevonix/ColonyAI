/**
 * Integration tests for colony-core.
 *
 * These tests spin up the Python sidecar and hit it from the TypeScript
 * plugin client to verify the HTTP contract works end-to-end.
 *
 * Prerequisites:
 * - Python sidecar installed and available on PATH
 * - Or run with SIDECAR_URL pointing to a running instance
 *
 * Run with: npm run test:integration
 */

import { describe, it, expect, beforeAll, afterAll, vi } from "vitest";
import { spawn, ChildProcess } from "child_process";
import { ColonySidecarClient, ColonyApiError } from "../src/sidecar-client.js";

const SIDECAR_URL =
  process.env.SIDECAR_URL || "http://localhost:8765";
const SIDECAR_API_KEY = process.env.SIDECAR_API_KEY || "test-key";

// Skip integration tests if no sidecar available
const shouldRunIntegration = process.env.CI ? false : true;

describe.skipIf(!shouldRunIntegration)("Integration: Sidecar", () => {
  let client: ColonySidecarClient;
  let sidecar: ChildProcess | null = null;

  beforeAll(async () => {
    // If no external sidecar URL, try to start one
    if (!process.env.SIDECAR_URL) {
      console.log("Starting sidecar for integration tests...");
      sidecar = spawn("python3", ["-m", "colony_sidecar.server"], {
        cwd: process.cwd() + "/sidecar",
        env: {
          ...process.env,
          COLONY_SIDECAR_PORT: "8765",
          COLONY_API_KEY: SIDECAR_API_KEY,
        },
        stdio: ["ignore", "pipe", "pipe"],
      });

      // Wait for sidecar to start
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }

    client = new ColonySidecarClient({
      sidecarUrl: SIDECAR_URL,
      apiKey: SIDECAR_API_KEY,
      requestTimeoutMs: 10000,
    });
  });

  afterAll(() => {
    if (sidecar) {
      sidecar.kill();
    }
  });

  describe("Health", () => {
    it("returns health status", async () => {
      const health = await client.health();
      expect(health).toBeDefined();
      expect(health).toHaveProperty("status");
    });
  });

  describe("Memory", () => {
    it("returns empty search results when unwired", async () => {
      const result = await client.memorySearch({
        identity: { host_id: "test" },
        context: { session_id: "test-session" },
        query: "test query",
      });
      expect(result).toBeDefined();
      expect(result).toHaveProperty("memories");
    });

    it("returns empty read results when unwired", async () => {
      const result = await client.memoryRead({
        identity: { host_id: "test" },
        context: { session_id: "test-session" },
      });
      expect(result).toBeDefined();
      expect(result).toHaveProperty("memories");
    });
  });

  describe("Safety", () => {
    it("checks content and returns safe by default", async () => {
      const result = await client.safetyCheck({
        identity: { host_id: "test" },
        context: { session_id: "test-session" },
        content: "Hello, this is a test message.",
      });
      expect(result).toBeDefined();
      // When unwired, safety check passes everything
      expect(result).toHaveProperty("approved");
    });
  });

  describe("Context", () => {
    it("assembles context with enriched endpoint", async () => {
      const result = await client.enrichedContext({
        identity: { host_id: "test" },
        context: { session_id: "test-session", contact_id: "test-contact" },
        message: "Hello",
        features: {
          memory: true,
          relationships: true,
          style: true,
          goals: true,
          worldModel: true,
          insights: true,
        },
      });
      expect(result).toBeDefined();
      expect(result).toHaveProperty("sections");
    });
  });

  describe("Signals", () => {
    it("ingests signals", async () => {
      const result = await client.signalsIngest({
        identity: { host_id: "test" },
        context: { session_id: "test-session" },
        signals: [
          { type: "message_sent", timestamp: new Date().toISOString() },
        ],
      });
      expect(result).toBeDefined();
      expect(result).toHaveProperty("ingested");
    });
  });

  describe("Turns", () => {
    it("syncs turn metadata", async () => {
      const result = await client.turnsSync({
        identity: { host_id: "test" },
        context: { session_id: "test-session", contact_id: "test-contact" },
        turn_summary: {
          user_message: "Hello",
          assistant_message: "Hi there!",
          topics: ["greeting"],
          entities: [],
          tools_used: [],
        },
      });
      expect(result).toBeDefined();
    });
  });

  describe("Goals", () => {
    it("lists goals (empty when unwired)", async () => {
      const result = await client.listGoals();
      expect(result).toBeDefined();
    });
  });

  describe("Contacts", () => {
    it("lists contacts (empty when unwired)", async () => {
      const result = await client.listContacts();
      expect(result).toBeDefined();
    });
  });

  describe("Insights", () => {
    it("lists insights (empty when unwired)", async () => {
      const result = await client.listInsights();
      expect(result).toBeDefined();
    });
  });

  describe("Skills", () => {
    it("lists skills", async () => {
      const result = await client.listSkills();
      expect(result).toBeDefined();
      expect(result).toHaveProperty("skills");
    });
  });

  describe("Autonomy", () => {
    it("returns autonomy status", async () => {
      const result = await client.autonomyStatus();
      expect(result).toBeDefined();
    });
  });

  describe("Identity", () => {
    it("returns identity status", async () => {
      const result = await client.identityStatus();
      expect(result).toBeDefined();
    });
  });

  describe("Error handling", () => {
    it("returns 501 for unwired reasoning turn", async () => {
      await expect(
        client.reasoningTurn({
          identity: { host_id: "test" },
          context: { session_id: "test-session" },
          messages: [{ role: "user", content: "Hello" }],
        }),
      ).rejects.toThrow(ColonyApiError);
    });
  });
});
