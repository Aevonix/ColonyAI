/**
 * Integration smoke test — drives the real ColonySidecarClient against
 * a live colony-core sidecar to verify end-to-end round-trip correctness.
 *
 * Prerequisites:
 *   1. Start colony-core: python /tmp/start-sidecar.py  (port 17777)
 *   2. API key "sk-colony-smoke-integration" registered with all scopes
 *   3. Run: COLONY_SMOKE_URL=http://127.0.0.1:17777 pnpm test -- tests/integration/
 *
 * These tests are NOT part of the default `pnpm test` — they require a
 * running sidecar. They're gated by COLONY_SMOKE_URL being set.
 */
import { describe, expect, it, beforeAll } from "vitest";

import { ColonyApiError, ColonySidecarClient } from "../../src/sidecar-client.js";
import type { ColonyPluginConfig } from "../../src/config.js";

const SIDECAR_URL = process.env.COLONY_SMOKE_URL ?? "";
const API_KEY = process.env.COLONY_SMOKE_KEY ?? "sk-colony-smoke-integration";

const skip = !SIDECAR_URL;

function makeClient(): ColonySidecarClient {
  const config: ColonyPluginConfig = {
    sidecarUrl: SIDECAR_URL,
    apiKey: API_KEY,
    ownReasoningLoop: false,
    hostId: "smoke-test",
    ownMemoryCapability: false,
    forwardProactiveDeliveries: false,
    failSafetyClosed: true,
    requestTimeoutMs: 10_000,
  };
  return new ColonySidecarClient(config);
}

const identity = { host_id: "smoke-test", plugin_version: "0.0.1" };
const ctx = { session_id: "smoke-sess-1", contact_id: "smoke-person-1" };

describe.skipIf(skip)("Integration: colony-core sidecar round-trip", () => {
  let client: ColonySidecarClient;

  beforeAll(() => {
    client = makeClient();
  });

  // --- Health ---

  it("GET /v1/host/health returns status + capabilities", async () => {
    const h = await client.health();
    expect(h.status).toBe("ok");
    expect(h.api_version).toBeDefined();
    expect(Array.isArray(h.capabilities)).toBe(true);
    expect(h.capabilities.length).toBeGreaterThan(0);
  });

  // --- Memory ---

  it("POST /v1/host/memory/search returns empty entries (no graph)", async () => {
    const res = await client.memorySearch({ identity, query: "test" });
    expect(Array.isArray(res.entries)).toBe(true);
    // No graph wired → empty, but 200 and correct shape
  });

  it("POST /v1/host/memory/write returns accepted shape or 503 (no graph)", async () => {
    try {
      const res = await client.memoryWrite({
        identity,
        content: "The user likes TypeScript",
        type: "preference",
      });
      expect(res).toHaveProperty("id");
      expect(res).toHaveProperty("accepted");
    } catch (err) {
      // Neo4j not running → 503 memory_unavailable is correct behavior
      expect(err).toBeInstanceOf(ColonyApiError);
      expect((err as ColonyApiError).status).toBeGreaterThanOrEqual(500);
    }
  });

  it("POST /v1/host/memory/read returns entries array", async () => {
    try {
      const res = await client.memoryRead({ identity, person_id: "smoke-person-1" });
      expect(Array.isArray(res.entries)).toBe(true);
    } catch (err) {
      // Neo4j not running → 503 is acceptable
      expect(err).toBeInstanceOf(ColonyApiError);
      expect((err as ColonyApiError).status).toBeGreaterThanOrEqual(500);
    }
  });

  it("POST /v1/host/memory/flush returns accepted shape", async () => {
    const res = await client.memoryFlush({ identity });
    expect(res).toHaveProperty("accepted");
  });

  // --- Embed (expected 501 without embedder) ---

  it("POST /v1/host/memory/embed returns 501 embed_not_wired", async () => {
    try {
      await client.memoryEmbed({ identity, inputs: ["test"] });
      // If it succeeds (embedder IS wired), that's also fine
    } catch (err) {
      expect(err).toBeInstanceOf(ColonyApiError);
      const e = err as ColonyApiError;
      expect(e.status).toBe(501);
      expect(e.code).toBe("embed_not_wired");
    }
  });

  // --- Context ---

  it("POST /v1/host/context/assemble returns sections + notices", async () => {
    const res = await client.contextAssemble({
      identity,
      context: ctx,
      incoming_message: { role: "user", content: "What do you know about me?" },
    });
    expect(Array.isArray(res.sections)).toBe(true);
    // notices may be undefined or array
    if (res.notices !== undefined) {
      expect(Array.isArray(res.notices)).toBe(true);
    }
  });

  // --- Safety ---

  it("POST /v1/host/safety/check returns decision shape", async () => {
    const res = await client.safetyCheck({
      identity,
      context: ctx,
      response_text: "Hello world",
      incoming_message_text: "Hi",
    });
    expect(res).toHaveProperty("decision");
    expect(res).toHaveProperty("blocked");
    // When ResponseGate IS wired (full server), Layer 1 may block
    // unknown sessions — that's correct behavior (RecipientVerifier
    // needs a matching session in the store). When no gate is wired,
    // passes through. Both are valid.
  });

  it("POST /v1/host/safety/check with PII triggers detection when gate is wired", async () => {
    const res = await client.safetyCheck({
      identity,
      context: ctx,
      response_text: "Your SSN is 123-45-6789",
      incoming_message_text: "Tell me my SSN",
    });
    // Depending on whether ResponseGate is wired:
    // - No gate: passes through (blocked: false)
    // - Gate wired: blocks (blocked: true, blocking_layer: 2)
    expect(res).toHaveProperty("decision");
    expect(res).toHaveProperty("blocked");
    if (res.blocked) {
      expect(res.blocking_layer).toBeDefined();
    }
  });

  // --- Signals ---

  it("POST /v1/host/signals/ingest accepts and returns shape", async () => {
    const res = await client.signalsIngest({
      identity,
      context: ctx,
      incoming_message: { role: "user", content: "hello" },
    });
    expect(res).toHaveProperty("accepted");
    expect(res.accepted).toBe(true);
  });

  // --- Turns sync ---

  it("POST /v1/host/turns/sync returns accepted shape", async () => {
    const res = await client.turnsSync({
      identity,
      context: ctx,
      topics: ["greeting"],
      entities: ["Colony"],
      tools_used: ["memory"],
      summary: "A hello turn",
    });
    expect(res).toHaveProperty("accepted");
    expect(res.accepted).toBe(true);
  });

  // --- Reasoning (expected 501) ---

  it("POST /v1/host/reasoning/turn returns 501 or 429 (not wired / rate-limited)", async () => {
    try {
      await client.reasoningTurn({
        identity,
        context: ctx,
        messages: [{ role: "user", content: "hi" }],
      });
      // If it succeeds (reasoning IS wired), that's fine too
    } catch (err) {
      expect(err).toBeInstanceOf(ColonyApiError);
      const e = err as ColonyApiError;
      // 501 = not wired (expected), 429 = rate-limited (also acceptable)
      expect([429, 501]).toContain(e.status);
    }
  });

  // --- WebSocket events (auth handshake) ---

  it("WS /v1/host/events completes first-message auth handshake", async () => {
    // The sidecar client's openEvents uses WebSocket and first-message auth.
    // We verify the handshake completes by checking we receive the
    // "subscribed" hello event.
    const events: unknown[] = [];
    const subscription = client.openEvents((event) => {
      events.push(event);
    });

    // Give the WS time to connect + handshake + receive hello
    await new Promise((r) => setTimeout(r, 2000));
    subscription.close();

    // Should have received at least the "subscribed" hello
    expect(events.length).toBeGreaterThanOrEqual(1);
    const first = events[0] as { type?: string; payload?: { message?: string } };
    expect(first.type).toBe("log");
    expect(first.payload?.message).toBe("subscribed");
  });
});
