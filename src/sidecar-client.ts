import WebSocket from "ws";

import type { ColonyPluginConfig } from "./config.js";
import type {
  ContextAssembleRequest,
  ContextAssembleResponse,
  HostEvent,
  HostHealthResponse,
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

  reasoningTurn(body: ReasoningTurnRequest): Promise<ReasoningTurnResponse> {
    return this.post<ReasoningTurnResponse>("/v1/host/reasoning/turn", body);
  }

  // --- Signals -------------------------------------------------------------

  signalsIngest(body: SignalIngestRequest): Promise<SignalIngestResponse> {
    return this.post<SignalIngestResponse>("/v1/host/signals/ingest", body);
  }

  // --- Safety --------------------------------------------------------------

  safetyCheck(body: SafetyCheckRequest): Promise<SafetyCheckResponse> {
    return this.post<SafetyCheckResponse>("/v1/host/safety/check", body);
  }

  // --- Events stream -------------------------------------------------------

  /**
   * Open the host events WebSocket and invoke `onEvent` for every frame.
   * Returns a `close()` handle. The caller is responsible for retry —
   * this client does not auto-reconnect because reconnect policy is a
   * host-side concern (registerService will be restarted by OpenClaw if
   * the lifecycle service throws).
   */
  openEvents(onEvent: (event: HostEvent) => void): { close: () => void } {
    const url = this.base.replace(/^http/, "ws") + "/v1/host/events";
    const ws = new WebSocket(url, {
      headers: { Authorization: `Bearer ${this.config.apiKey}` },
    });

    ws.on("message", (raw) => {
      try {
        const event = JSON.parse(raw.toString()) as HostEvent;
        onEvent(event);
      } catch {
        // Ignore malformed frames; the server should never emit them but
        // we don't want a parse error to kill the subscriber.
      }
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

  private async post<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>("POST", path, body);
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.requestTimeoutMs);

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
    }
  }
}
