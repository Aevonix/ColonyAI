import WebSocket from "ws";

import type { ColonyPluginConfig } from "./config.js";
import type {
  ContextAssembleRequest,
  ContextAssembleResponse,
  HostEvent,
  HostHealthResponse,
  HostIdentity,
  HostTurnContext,
  MemoryEmbedRequest,
  MemoryEmbedResponse,
  MemoryFlushRequest,
  MemoryFlushResponse,
  MemoryReadRequest,
  MemoryReadResponse,
  MemorySearchRequest,
  MemorySearchResponse,
  MemoryWriteRequest,
  MemoryWriteResponse,
  ReasoningTurnRequest,
  ReasoningTurnResponse,
  SafetyCheckRequest,
  SafetyCheckResponse,
  SignalIngestRequest,
  SignalIngestResponse,
  TurnSyncRequest,
  TurnSyncResponse,
  CommitmentResponse,
  CommitmentListResponse,
} from "./types.js";

/**
 * Error returned by the colony sidecar. Carries the structured
 * `{error: {code, message, ...}}` envelope so callers can branch on
 * machine-readable codes (e.g. `phase1_wiring_required`).
 */
export class ColonyApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details?: unknown,
  ) {
    super(`[${status} ${code}] ${message}`);
    this.name = "ColonyApiError";
  }
}

/**
 * Thin HTTP/WS client for the colony /v1/host API.
 *
 * Each public method maps 1:1 to a host endpoint defined in
 * `colony/api/routers/host.py`. The plugin entry point (see
 * `./plugin.ts`) wires each method to one of OpenClaw's `register*`
 * extension slots.
 */
export class ColonySidecarClient {
  private readonly base: string;

  constructor(private readonly config: ColonyPluginConfig) {
    this.base = config.sidecarUrl.replace(/\/+$/, "");
  }

  // --- Health --------------------------------------------------------------

  health(): Promise<HostHealthResponse> {
    return this.get<HostHealthResponse>("/v1/host/health");
  }

  // --- Memory --------------------------------------------------------------

  memoryRead(body: MemoryReadRequest): Promise<MemoryReadResponse> {
    return this.post<MemoryReadResponse>("/v1/host/memory/read", body);
  }

  memoryWrite(body: MemoryWriteRequest): Promise<MemoryWriteResponse> {
    return this.post<MemoryWriteResponse>("/v1/host/memory/write", body);
  }

  memorySearch(body: MemorySearchRequest): Promise<MemorySearchResponse> {
    return this.post<MemorySearchResponse>("/v1/host/memory/search", body);
  }

  memoryFlush(body: MemoryFlushRequest): Promise<MemoryFlushResponse> {
    return this.post<MemoryFlushResponse>("/v1/host/memory/flush", body);
  }

  memoryEmbed(body: MemoryEmbedRequest): Promise<MemoryEmbedResponse> {
    return this.post<MemoryEmbedResponse>("/v1/host/memory/embed", body);
  }

  // --- Context -------------------------------------------------------------

  contextAssemble(body: ContextAssembleRequest): Promise<ContextAssembleResponse> {
    return this.post<ContextAssembleResponse>("/v1/host/context/assemble", body);
  }

  /**
   * One-stop context assembly — queries all intelligence systems in
   * parallel and returns assembled sections.
   */
  enrichedContext(body: {
    identity: HostIdentity;
    context: HostTurnContext;
    message: string;
    features?: Record<string, boolean>;
    compression?: "off" | "conservative" | "balanced" | "aggressive";
  }): Promise<ContextAssembleResponse> {
    return this.post<ContextAssembleResponse>("/v1/host/context/enriched", body);
  }

  // --- Reasoning -----------------------------------------------------------

  /**
   * Invoke the colony reasoning endpoint.
   *
   * Accepts an optional ``signal`` so callers (specifically the
   * ``AgentHarness`` adapter) can propagate OpenClaw's
   * ``EmbeddedRunAttemptParams.abortSignal`` into the HTTP request. When
   * both ``signal`` and the internal per-request timeout fire, whichever
   * trips first aborts the ``fetch`` — see ``request()`` for the merge.
   */
  reasoningTurn(
    body: ReasoningTurnRequest,
    opts?: { signal?: AbortSignal },
  ): Promise<ReasoningTurnResponse> {
    return this.post<ReasoningTurnResponse>(
      "/v1/host/reasoning/turn",
      body,
      opts,
    );
  }

  // --- Signals -------------------------------------------------------------

  signalsIngest(body: SignalIngestRequest): Promise<SignalIngestResponse> {
    return this.post<SignalIngestResponse>("/v1/host/signals/ingest", body);
  }

  // --- Turns (post-turn cognition sync) ------------------------------------

  turnsSync(body: TurnSyncRequest): Promise<TurnSyncResponse> {
    return this.post<TurnSyncResponse>("/v1/host/turns/sync", body);
  }

  // --- Safety --------------------------------------------------------------

  safetyCheck(body: SafetyCheckRequest): Promise<SafetyCheckResponse> {
    return this.post<SafetyCheckResponse>("/v1/host/safety/check", body);
  }

  // --- Goals --------------------------------------------------------------

  listGoals(params?: { person_id?: string; status?: string }): Promise<unknown> {
    const qs = new URLSearchParams();
    if (params?.person_id) qs.set("person_id", params.person_id);
    if (params?.status) qs.set("status_filter", params.status);
    const query = qs.toString();
    return this.get(`/v1/host/goals${query ? `?${query}` : ""}`);
  }

  getGoal(goalId: string): Promise<unknown> {
    return this.get(`/v1/host/goals/${goalId}`);
  }

  updateGoal(goalId: string, body: { status?: string; progress?: number; notes?: string }): Promise<unknown> {
    return this.request("PATCH", `/v1/host/goals/${goalId}`, body);
  }

  // --- Commitments --------------------------------------------------------

  createCommitment(body: {
    person_id: string;
    description: string;
    due_at?: string;
    priority?: number;
    source_type?: "manual" | "autonomy" | "cognition";
    source_context?: string;
    metadata?: Record<string, unknown>;
  }): Promise<CommitmentResponse> {
    return this.post<CommitmentResponse>("/v1/host/commitments", body);
  }

  listCommitments(params?: {
    person_id?: string;
    status?: string;
    overdue_only?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<CommitmentListResponse> {
    const qs = new URLSearchParams();
    if (params?.person_id) qs.set("person_id", params.person_id);
    if (params?.status) qs.set("status", params.status);
    if (params?.overdue_only) qs.set("overdue_only", "true");
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.offset) qs.set("offset", String(params.offset));
    const query = qs.toString();
    return this.get<CommitmentListResponse>(`/v1/host/commitments${query ? `?${query}` : ""}`);
  }

  getCommitment(id: string): Promise<CommitmentResponse> {
    return this.get<CommitmentResponse>(`/v1/host/commitments/${id}`);
  }

  updateCommitment(id: string, body: {
    status?: "fulfilled" | "cancelled";
    fulfilled_at?: string;
    description?: string;
    due_at?: string;
    priority?: number;
    metadata?: Record<string, unknown>;
  }): Promise<CommitmentResponse> {
    return this.patch<CommitmentResponse>(`/v1/host/commitments/${id}`, body);
  }

  deleteCommitment(id: string): Promise<void> {
    return this.delete(`/v1/host/commitments/${id}`);
  }

  // --- Skills --------------------------------------------------------------

  listSkills(): Promise<{ skills: unknown[] }> {
    return this.get<{ skills: unknown[] }>("/v1/host/skills/registry");
  }

  getSkill(skillId: string): Promise<unknown> {
    return this.get(`/v1/host/skills/registry/${skillId}`);
  }

  /**
   * Execute an ACTIVE skill server-side. Returns the SkillExecutor
   * result with status, output, error, and execution metadata.
   */
  executeSkill(
    skillId: string,
    args: Record<string, unknown>,
    identity: HostIdentity,
  ): Promise<{
    status: "success" | "failed" | "timeout" | "violated";
    output?: unknown;
    error?: string | null;
    execution_id?: string | null;
    duration_ms?: number | null;
  }> {
    return this.post(`/v1/host/skills/${skillId}/execute`, {
      identity,
      arguments: args,
    });
  }

  // --- Host configuration --------------------------------------------------

  /**
   * Forward host LLM credentials + model assignments to the sidecar.
   * Called once at plugin startup so the sidecar's ReasoningLoop can
   * use the host's provider instead of requiring its own keys.
   */
  configureHost(
    llm: Record<string, unknown>,
    identity: HostIdentity,
  ): Promise<{
    configured: boolean;
    provider?: string | null;
    models?: Record<string, string> | null;
  }> {
    return this.post("/v1/host/configure", { identity, llm });
  }

  /**
   * Request a signed chain-verify attestation from the sidecar. The
   * sidecar signs ``colony_id:data:timestamp`` with the colony's
   * Ed25519 private key when the key manager is available.
   */
  chainVerify(
    data: string,
    identity: HostIdentity,
  ): Promise<{
    valid: boolean;
    colony_id?: string | null;
    signed_attestation?: string | null;
    attested_at?: string | null;
    signer_public_key?: string | null;
  }> {
    return this.post("/v1/host/chain/verify", { identity, data });
  }

  // --- Native tools --------------------------------------------------------

  /**
   * Invoke a sidecar-resident native tool (calculate, web_search,
   * read_file, write_file, list_directory) by name. Returns the raw
   * string result from the tool handler, or an error envelope.
   */
  toolsInvoke(
    name: string,
    args: Record<string, unknown>,
    identity: HostIdentity,
  ): Promise<{ result: string; available: boolean; error?: string | null }> {
    return this.post("/v1/host/reasoning/tools/invoke", {
      identity,
      name,
      arguments: args,
    });
  }

  // --- Insights ------------------------------------------------------------

  listInsights(params?: { limit?: number; dismissed?: boolean }): Promise<unknown> {
    const qs = new URLSearchParams();
    if (params?.limit) qs.set("limit", String(params.limit));
    if (params?.dismissed !== undefined) qs.set("dismissed", String(params.dismissed));
    const query = qs.toString();
    return this.get(`/v1/host/insights${query ? `?${query}` : ""}`);
  }

  dismissInsight(insightId: string): Promise<unknown> {
    return this.post(`/v1/host/insights/${insightId}/dismiss`, {});
  }

  // --- Cognition -----------------------------------------------------------

  getCPI(): Promise<unknown> {
    return this.get("/v1/host/cognition/cpi");
  }

  cognitionCycle(): Promise<unknown> {
    return this.post("/v1/host/cognition/cycle", {
      identity: { host_id: "colony-plugin" },
    });
  }

  // --- Learning ------------------------------------------------------------

  getLearningWeights(): Promise<unknown> {
    return this.get("/v1/host/learning/weights");
  }

  submitCorrection(params: {
    context: { session_id: string; contact_id: string };
    original: string;
    correction: string;
    component?: string;
  }): Promise<unknown> {
    return this.post("/v1/host/learning/correction", {
      identity: { host_id: "colony-plugin" },
      ...params,
    });
  }

  submitEngagement(briefingId: string, action: string, dwellSeconds?: number): Promise<unknown> {
    return this.post("/v1/host/learning/engagement", {
      identity: { host_id: "colony-plugin" },
      briefing_id: briefingId,
      action,
      dwell_seconds: dwellSeconds,
    });
  }

  // --- Autonomy ------------------------------------------------------------

  autonomyStatus(): Promise<unknown> {
    return this.get("/v1/host/autonomy/status");
  }

  autonomyStart(): Promise<unknown> {
    return this.post("/v1/host/autonomy/start", {});
  }

  autonomyStop(): Promise<unknown> {
    return this.post("/v1/host/autonomy/stop", {});
  }

  // --- Contacts ------------------------------------------------------------

  listContacts(): Promise<unknown> {
    return this.get("/v1/host/contacts");
  }

  getContact(contactId: string): Promise<unknown> {
    return this.get(`/v1/host/contacts/${contactId}`);
  }

  getContactStyle(contactId: string): Promise<unknown> {
    return this.post("/v1/host/contacts/" + contactId + "/style", {
      identity: { host_id: "colony-plugin" },
      person_id: contactId,
    });
  }

  // --- Briefings -----------------------------------------------------------

  listBriefings(limit = 10): Promise<unknown> {
    return this.get(`/v1/host/briefings?limit=${limit}`);
  }

  // --- World Model ---------------------------------------------------------

  listEntities(params?: { entity_type?: string; limit?: number }): Promise<unknown> {
    const qs = new URLSearchParams();
    if (params?.entity_type) qs.set("entity_type", params.entity_type);
    if (params?.limit) qs.set("limit", String(params.limit));
    const query = qs.toString();
    return this.get(`/v1/host/world/entities${query ? `?${query}` : ""}`);
  }

  queryEntities(query: string, limit = 10): Promise<unknown> {
    return this.post("/v1/host/world/entities/query", {
      identity: { host_id: "colony-plugin" },
      query,
      limit,
    });
  }

  // --- Research ------------------------------------------------------------

  startResearch(topic: string, depth = "standard"): Promise<unknown> {
    return this.post("/v1/host/research/start", {
      identity: { host_id: "colony-plugin" },
      topic,
      depth,
    });
  }

  listResearch(limit = 20): Promise<unknown> {
    return this.get(`/v1/host/research?limit=${limit}`);
  }

  // --- Delivery ------------------------------------------------------------

  listPendingDeliveries(gatewayId?: string, limit = 20): Promise<unknown> {
    const qs = new URLSearchParams();
    if (gatewayId) qs.set("gateway_id", gatewayId);
    qs.set("limit", String(limit));
    return this.get(`/v1/host/delivery/pending?${qs}`);
  }

  markDeliverySent(deliveryId: string): Promise<unknown> {
    return this.post("/v1/host/delivery/mark-sent", {
      identity: { host_id: "colony-plugin" },
      delivery_id: deliveryId,
    });
  }

  // --- Synthesis -----------------------------------------------------------

  discoverConnections(personId?: string, minNovelty = 0.3): Promise<unknown> {
    return this.post("/v1/host/synthesis/discover", {
      identity: { host_id: "colony-plugin" },
      person_id: personId,
      min_novelty: minNovelty,
    });
  }

  // --- Identity / Chain ----------------------------------------------------

  identityStatus(): Promise<unknown> {
    return this.get("/v1/host/identity/status");
  }

  identityInit(force = false): Promise<unknown> {
    return this.post("/v1/host/identity/init", {
      identity: { host_id: "colony-plugin" },
      force,
    });
  }


  // --- Secrets -------------------------------------------------------------

  listSecrets(prefix?: string): Promise<unknown> {
    return this.post("/v1/host/secrets/list", {
      identity: { host_id: "colony-plugin" },
      prefix,
    });
  }

  getSecret(key: string): Promise<unknown> {
    return this.post("/v1/host/secrets/get", {
      identity: { host_id: "colony-plugin" },
      key,
    });
  }

  setSecret(key: string, value: string, secretType?: string): Promise<unknown> {
    return this.post("/v1/host/secrets/set", {
      identity: { host_id: "colony-plugin" },
      key,
      value,
      secret_type: secretType,
    });
  }

  deleteSecret(key: string): Promise<unknown> {
    return this.post("/v1/host/secrets/delete", {
      identity: { host_id: "colony-plugin" },
      key,
    });
  }

  // --- Events stream -------------------------------------------------------

  /**
   * Replay missed events from the persistent journal.
   *
   * Call this on WebSocket reconnect with the timestamp of the last
   * event you successfully processed. Returns events recorded after
   * that timestamp, up to ``limit``.
   */
  async replayEvents(
    since: string,
    limit: number = 500,
    types?: string[],
  ): Promise<{
    events: Array<{
      seq: number;
      ulid: string;
      type: string;
      recordedAt: string;
      data: Record<string, unknown>;
    }>;
    lastSeq: number;
    hasMore: boolean;
  }> {
    const params = new URLSearchParams({ since, limit: String(limit) });
    if (types && types.length > 0) {
      params.set("types", types.join(","));
    }
    return this.get(`/v1/host/events/replay?${params}`);
  }

  /**
   * Open the host events WebSocket and invoke `onEvent` for every frame.
   * Returns a `close()` handle. The caller is responsible for retry —
   * this client does not auto-reconnect because reconnect policy is a
   * host-side concern (registerService will be restarted by OpenClaw if
   * the lifecycle service throws).
   *
   * Authenticates via the first-message handshake colony enforces
   * on /v1/host/events (mirroring /v1/ws): after the socket opens the
   * client sends `{"type":"auth","token":"sk-colony-..."}` within 10
   * seconds. The server replies with `{"type":"auth_ok",...}` on
   * success or closes with code 4001 on failure. The Authorization
   * header isn't read by colony — first-message auth is the
   * supported path.
   */
  openEvents(
    onEvent: (event: HostEvent) => void,
    lastEventId?: string,
  ): { close: () => void } {
    const url = this.base.replace(/^http/, "ws") + "/v1/host/events";
    const ws = new WebSocket(url);

    let authed = false;

    ws.on("open", () => {
      try {
        const authMsg: Record<string, string> = {
          type: "auth",
          token: this.config.apiKey ?? "",
          ...(lastEventId ? { lastEventId } : {}),
        };
        ws.send(JSON.stringify(authMsg));
      } catch {
        // Socket closed mid-handshake; the close handler will fire.
      }
    });

    ws.on("message", (raw) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw.toString());
      } catch {
        return; // Ignore malformed frames.
      }

      // Swallow the auth_ok / log:subscribed handshake frames so the
      // consumer only sees real domain events.
      //
      // Server frame order on success (colony/api/routers/host.py:813-824):
      //   1. {"type":"auth_ok","scopes":[...],"connected_at":...}
      //   2. {"type":"log","payload":{"message":"subscribed"}, ...}
      //   3+ real HostEvent frames (turn_synced, memory_consolidated, ...)
      //
      // The branch below tolerates either auth_ok-first or log-first to
      // keep this client robust if the server ever reorders the
      // handshake; if reordering does happen, update both sides
      // together rather than relying on this leniency.
      if (!authed) {
        const t = (parsed as { type?: string }).type;
        if (t === "auth_ok") {
          authed = true;
          return;
        }
        if (t === "log") {
          // The "subscribed" hello also arrives before any real event;
          // pass it through so existing consumers that depend on the
          // hello frame keep working.
          authed = true;
          onEvent(parsed as HostEvent);
          return;
        }
        // Any other pre-auth-ok frame is unexpected; drop it.
        return;
      }

      onEvent(parsed as HostEvent);
    });

    return {
      close: () => {
        try {
          ws.close();
        } catch {
          // ignore
        }
      },
    };
  }

  // --- Internals -----------------------------------------------------------

  private async get<T>(path: string): Promise<T> {
    return this.request<T>("GET", path);
  }

  private async post<T>(
    path: string,
    body: unknown,
    opts?: { signal?: AbortSignal },
  ): Promise<T> {
    return this.request<T>("POST", path, body, opts);
  }

  private async patch<T>(
    path: string,
    body: unknown,
    opts?: { signal?: AbortSignal },
  ): Promise<T> {
    return this.request<T>("PATCH", path, body, opts);
  }

  private async delete<T>(
    path: string,
    opts?: { signal?: AbortSignal },
  ): Promise<T> {
    return this.request<T>("DELETE", path, undefined, opts);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    opts?: { signal?: AbortSignal },
  ): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.requestTimeoutMs);

    // Merge the internal timeout controller with the caller-supplied
    // signal (if any): whichever fires first aborts the request. Prefer
    // the native ``AbortSignal.any`` when available (Node 20.3+); fall
    // back to a manual listener otherwise so this stays compatible with
    // older runtimes.
    const external = opts?.signal;
    let unlistenExternal: (() => void) | undefined;
    if (external) {
      if (external.aborted) {
        controller.abort();
      } else {
        const onAbort = () => controller.abort();
        external.addEventListener("abort", onAbort, { once: true });
        unlistenExternal = () =>
          external.removeEventListener("abort", onAbort);
      }
    }

    try {
      const response = await fetch(this.base + path, {
        method,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${this.config.apiKey}`,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });

      if (!response.ok) {
        let code = "http_error";
        let message = response.statusText;
        let details: unknown;
        try {
          const payload = (await response.json()) as {
            error?: { code?: string; message?: string; details?: unknown };
            detail?: unknown;
          };
          const err = payload.error ?? (payload.detail as { error?: { code?: string; message?: string } } | undefined)?.error;
          if (err) {
            code = err.code ?? code;
            message = err.message ?? message;
            details = (err as { details?: unknown }).details;
          }
        } catch {
          // body wasn't JSON
        }
        throw new ColonyApiError(response.status, code, message, details);
      }

      return (await response.json()) as T;
    } finally {
      clearTimeout(timer);
      unlistenExternal?.();
    }
  }
}
