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
} from "./types.js";

/**
 * Error returned by the colony-core sidecar. Carries the structured
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
 * Thin HTTP/WS client for the colony-core /v1/host API.
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

  // --- Reasoning -----------------------------------------------------------

  /**
   * Invoke the colony-core reasoning endpoint.
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

  // --- Enriched Context ----------------------------------------------------

  /**
   * One-stop context assembly — queries all intelligence systems in
   * parallel and returns assembled sections.
   */
  enrichedContext(body: {
    identity: HostIdentity;
    context: HostTurnContext;
    message: string;
    features?: Record<string, boolean>;
  }): Promise<ContextAssembleResponse> {
    return this.post<ContextAssembleResponse>("/v1/host/context/enriched", body);
  }

  // --- Goals ---------------------------------------------------------------

  listGoals(params: {
    person_id?: string;
    status?: string;
  }): Promise<{ goals: unknown[] }> {
    const qs = new URLSearchParams();
    if (params.person_id) qs.set("person_id", params.person_id);
    if (params.status) qs.set("status_filter", params.status);
    return this.get<{ goals: unknown[] }>(`/v1/host/goals?${qs}`);
  }

  // --- Skills --------------------------------------------------------------

  listSkills(): Promise<{ skills: unknown[] }> {
    return this.get<{ skills: unknown[] }>("/v1/host/skills/registry");
  }

  // --- Insights ------------------------------------------------------------

  listInsights(params: { limit?: number }): Promise<{ insights: unknown[] }> {
    const qs = new URLSearchParams();
    if (params.limit) qs.set("limit", String(params.limit));
    return this.get<{ insights: unknown[] }>(`/v1/host/insights?${qs}`);
  }

  // --- Cognition -----------------------------------------------------------

  getCPI(): Promise<unknown> {
    return this.get("/v1/host/cognition/cpi");
  }

  // --- Learning ------------------------------------------------------------

  getLearningWeights(): Promise<unknown> {
    return this.get("/v1/host/learning/weights");
  }

  // --- Events stream -------------------------------------------------------

  /**
   * Open the host events WebSocket and invoke `onEvent` for every frame.
   * Returns a `close()` handle. The caller is responsible for retry —
   * this client does not auto-reconnect because reconnect policy is a
   * host-side concern (registerService will be restarted by OpenClaw if
   * the lifecycle service throws).
   *
   * Authenticates via the first-message handshake colony-core enforces
   * on /v1/host/events (mirroring /v1/ws): after the socket opens the
   * client sends `{"type":"auth","token":"sk-colony-..."}` within 10
   * seconds. The server replies with `{"type":"auth_ok",...}` on
   * success or closes with code 4001 on failure. The Authorization
   * header isn't read by colony-core — first-message auth is the
   * supported path.
   */
  openEvents(onEvent: (event: HostEvent) => void): { close: () => void } {
    const url = this.base.replace(/^http/, "ws") + "/v1/host/events";
    const ws = new WebSocket(url);

    let authed = false;

    ws.on("open", () => {
      try {
        ws.send(JSON.stringify({ type: "auth", token: this.config.apiKey }));
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
