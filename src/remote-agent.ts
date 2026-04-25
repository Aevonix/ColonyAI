/**
 * Remote Agent WebSocket Client for Colony Plugin
 *
 * Handles WebSocket connection for remote agents connecting to Colony
 * via `colony agent connect`.
 *
 * Usage:
 *   const agent = await loadAgentConfig();
 *   if (agent) {
 *     const client = new RemoteAgentClient(agent, logger);
 *     client.onInitiative = (initiative) => handleInitiative(initiative);
 *     await client.connect();
 *   }
 */

import { createRequire } from "module";
import { promises as fs } from "fs";
import * as path from "path";
import * as os from "os";
import type { PluginLogger } from "openclaw/plugin-sdk/plugin-entry";

const require = createRequire(import.meta.url);
const WebSocket = require("ws");

/**
 * Agent config saved by `colony agent connect`.
 * Located at ~/.colony/agent.json
 */
export interface AgentConfig {
  agent_id: string;
  node_id: string;
  colony_id: string;
  websocket_url?: string;
  name: string;
  capabilities: string[];
  is_primary: boolean;
  max_concurrent: number;
  node_cert?: {
    colony_id: string;
    node_id: string;
    public_key?: string;
    signature: string;
    issued_at: string;
    expires_at?: string;
  };
  connection_mode: "local" | "remote";
  registered_at?: string;
}

/**
 * Initiative pushed from Colony to agent.
 */
export interface Initiative {
  id: string;
  type: string;
  description: string;
  priority: number;
  timeout_seconds?: number;
  context?: Record<string, unknown>;
  assigned_at: string;
}

/**
 * WebSocket message types.
 */
interface WsMessage {
  type: string;
  seq?: number;
  [key: string]: unknown;
}

interface InitiativeMessage extends WsMessage {
  type: "initiative";
  initiative: Initiative;
  seq: number;
}

/**
 * Load agent config from ~/.colony/agent.json
 */
export async function loadAgentConfig(): Promise<AgentConfig | null> {
  const agentConfigPath = path.join(os.homedir(), ".colony", "agent.json");
  try {
    const content = await fs.readFile(agentConfigPath, "utf-8");
    return JSON.parse(content) as AgentConfig;
  } catch {
    return null;
  }
}

/**
 * Check if this plugin is running as a remote agent.
 * Returns true if ~/.colony/agent.json exists and connection_mode is "remote".
 */
export async function isRemoteAgent(): Promise<boolean> {
  const config = await loadAgentConfig();
  return config?.connection_mode === "remote" && !!config.websocket_url;
}

/**
 * WebSocket client for remote Colony agents.
 */
export class RemoteAgentClient {
  private config: AgentConfig;
  private logger: PluginLogger;
  private ws: ReturnType<typeof WebSocket> | null = null;
  private running = false;
  private seq = 0;
  private reconnectDelay = 1000;
  private maxReconnectDelay = 60000;
  private heartbeatInterval: ReturnType<typeof setInterval> | null = null;

  /** Called when Colony pushes an initiative */
  onInitiative?: (initiative: Initiative) => Promise<void>;

  /** Called when WebSocket disconnects */
  onDisconnect?: (reason: string) => void;

  /** Called when WebSocket connects */
  onConnect?: () => void;

  constructor(config: AgentConfig, logger: PluginLogger) {
    this.config = config;
    this.logger = logger;
  }

  /**
   * Connect to Colony WebSocket and start message loop.
   */
  async connect(): Promise<void> {
    if (!this.config.websocket_url) {
      this.logger.error?.("No websocket_url in agent config");
      return;
    }

    this.running = true;
    await this._connect();
  }

  /**
   * Disconnect from Colony.
   */
  async disconnect(): Promise<void> {
    this.running = false;
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }

  /**
   * Send initiative acknowledgment.
   */
  async acknowledge(initiativeId: string): Promise<boolean> {
    return this._send({
      type: "initiative_ack",
      initiative_id: initiativeId,
    });
  }

  /**
   * Mark initiative as completed.
   */
  async complete(
    initiativeId: string,
    result?: string,
    metadata?: Record<string, unknown>
  ): Promise<boolean> {
    return this._send({
      type: "initiative_complete",
      initiative_id: initiativeId,
      result,
      result_metadata: metadata,
    });
  }

  /**
   * Mark initiative as failed.
   */
  async fail(initiativeId: string, reason: string, retry = true): Promise<boolean> {
    return this._send({
      type: "initiative_fail",
      initiative_id: initiativeId,
      reason,
      retry,
    });
  }

  /**
   * Update agent status.
   */
  async updateStatus(status: "online" | "busy" | "offline", currentAssignments = 0): Promise<boolean> {
    return this._send({
      type: "status_update",
      status,
      current_assignments: currentAssignments,
    });
  }

  private async _connect(): Promise<void> {
    if (!this.running || !this.config.websocket_url) return;

    try {
      // Connect to Colony WebSocket
      // Note: Auth is handled via the first message after connection
      this.ws = new WebSocket(this.config.websocket_url);

      this.ws.on("open", () => {
        this.logger.info?.("WebSocket connected to Colony");
        this.reconnectDelay = 1000; // Reset on success
        
        // Send auth message with agent ID and cert
        this._send({
          type: "auth",
          agent_id: this.config.agent_id,
          node_id: this.config.node_id,
          signature: this.config.node_cert?.signature,
        });
        
        this._startHeartbeat();
        this.onConnect?.();
      });

      this.ws.on("message", (data: Buffer) => {
        try {
          const msg = JSON.parse(data.toString()) as WsMessage;
          this._handleMessage(msg);
        } catch (e) {
          this.logger.error?.(`Failed to parse WebSocket message: ${e}`);
        }
      });

      this.ws.on("close", () => {
        this.logger.warn?.("WebSocket disconnected");
        this._stopHeartbeat();
        this.onDisconnect?.("connection closed");
        this._scheduleReconnect();
      });

      this.ws.on("error", (err: Error) => {
        this.logger.error?.(`WebSocket error: ${err.message}`);
      });

    } catch (err) {
      this.logger.error?.(`Failed to connect WebSocket: ${err}`);
      this._scheduleReconnect();
    }
  }

  private _handleMessage(msg: WsMessage): void {
    switch (msg.type) {
      case "initiative": {
        const initiativeMsg = msg as InitiativeMessage;
        if (this.onInitiative && initiativeMsg.initiative) {
          this.onInitiative(initiativeMsg.initiative).catch((err) => {
            this.logger.error?.(`Initiative handler error: ${err}`);
          });
        }
        // Auto-ack receipt
        this._send({ type: "ack", ack_seq: initiativeMsg.seq });
        break;
      }

      case "ping": {
        this._send({ type: "pong", seq: msg.seq });
        break;
      }

      case "disconnect": {
        this.logger.warn?.(`Colony requested disconnect: ${msg.reason}`);
        this.disconnect().catch(() => {});
        break;
      }

      case "config": {
        this.logger.info?.("Received config update from Colony");
        break;
      }

      default:
        this.logger.debug?.(`Unknown WebSocket message type: ${msg.type}`);
    }
  }

  private _send(msg: Record<string, unknown>): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return false;
    }

    this.seq++;
    const payload = JSON.stringify({ ...msg, seq: this.seq });
    this.ws.send(payload);
    return true;
  }

  private _startHeartbeat(): void {
    this.heartbeatInterval = setInterval(() => {
      this.updateStatus("online", 0).catch(() => {});
    }, 30000);
  }

  private _stopHeartbeat(): void {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  private _scheduleReconnect(): void {
    if (!this.running) return;

    this.logger.info?.(`Reconnecting in ${this.reconnectDelay / 1000}s...`);
    setTimeout(() => {
      this._connect().catch(() => {});
    }, this.reconnectDelay);

    // Exponential backoff
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
  }
}
