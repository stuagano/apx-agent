/**
 * RemoteAgent — card-based discovery for remote agents.
 *
 * Fetches an A2A agent card from `/.well-known/agent.json`, extracts
 * name/description/skills metadata, and proxies `run()`/`stream()` calls
 * to the remote agent via `POST /responses` with OBO header forwarding.
 *
 * Implements the `Runnable` interface so it can be composed in
 * SequentialAgent, ParallelAgent, RouterAgent, or HandoffAgent like
 * any local agent.
 *
 * @example
 * // From a full agent card URL
 * const remote = await RemoteAgent.fromCardUrl(
 *   'https://data-inspector.workspace.databricksapps.com/.well-known/agent.json'
 * );
 *
 * // From a Databricks App name (requires DATABRICKS_HOST)
 * const remote = await RemoteAgent.fromAppName('data-inspector');
 *
 * // Compose in a pipeline
 * const pipeline = new SequentialAgent([localAnalyzer, remote]);
 */

import type { AgentCard } from '../discovery/index.js';
import type { AgentTool } from '../agent/tools.js';
import type { Message, Runnable } from './types.js';
import { getRequestContext } from '../agent/request-context.js';
import {
  addSpan,
  endSpan,
  truncate,
  agentNameFromUrl,
  traceHeadersOut,
  traceIdFromResponse,
} from '../trace.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RemoteAgentConfig {
  /** Full URL to the agent card (/.well-known/agent.json). */
  cardUrl: string;
  /** Optional headers to forward on every request (e.g. OBO auth). */
  headers?: Record<string, string>;
  /** Request timeout in ms. Default: 120_000 (2 min). */
  timeoutMs?: number;
}

interface ResponsesOutput {
  output: Array<{
    type: string;
    role: string;
    content: Array<{ type: string; text: string }>;
  }>;
}

// ---------------------------------------------------------------------------
// RemoteAgent
// ---------------------------------------------------------------------------

export class RemoteAgent implements Runnable {
  /** Agent card metadata — populated after `init()`. */
  card: AgentCard | null = null;

  private cardUrl: string;
  private baseUrl: string;
  private headers: Record<string, string>;
  private timeoutMs: number;
  private initPromise: Promise<void> | null = null;

  constructor(config: RemoteAgentConfig) {
    this.cardUrl = config.cardUrl;
    // Derive base URL by stripping the well-known path
    this.baseUrl = config.cardUrl.replace(/\/?\.well-known\/agent\.json$/, '').replace(/\/$/, '');
    this.headers = config.headers ?? {};
    this.timeoutMs = config.timeoutMs ?? 120_000;
  }

  // -----------------------------------------------------------------------
  // Factory methods
  // -----------------------------------------------------------------------

  /**
   * Create a RemoteAgent from a full agent card URL.
   * The card is fetched eagerly so metadata is available immediately.
   */
  static async fromCardUrl(cardUrl: string, headers?: Record<string, string>): Promise<RemoteAgent> {
    const agent = new RemoteAgent({ cardUrl, headers });
    await agent.init();
    return agent;
  }

  /**
   * Create a RemoteAgent from a Databricks App name.
   *
   * Constructs the agent card URL from `DATABRICKS_HOST`:
   *   `https://<host>/apps/<appName>/.well-known/agent.json`
   *
   * Falls back to the apps subdomain pattern if DATABRICKS_HOST is not set
   * but DATABRICKS_WORKSPACE_ID is available.
   */
  static async fromAppName(appName: string, headers?: Record<string, string>): Promise<RemoteAgent> {
    const host = process.env.DATABRICKS_HOST?.replace(/\/$/, '');
    if (!host) {
      throw new Error(
        'RemoteAgent.fromAppName requires DATABRICKS_HOST environment variable. ' +
        'Use RemoteAgent.fromCardUrl() with a full URL instead.'
      );
    }
    const cardUrl = `${host}/apps/${appName}/.well-known/agent.json`;
    return RemoteAgent.fromCardUrl(cardUrl, headers);
  }

  // -----------------------------------------------------------------------
  // Initialization — fetch the agent card
  // -----------------------------------------------------------------------

  /** Fetch the agent card. Safe to call multiple times (idempotent). */
  async init(): Promise<void> {
    if (this.card) return;
    if (this.initPromise) return this.initPromise;

    this.initPromise = this.fetchCard();
    return this.initPromise;
  }

  private async fetchCard(): Promise<void> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10_000);

    try {
      const res = await fetch(this.cardUrl, {
        headers: this.headers,
        signal: controller.signal,
      });

      if (!res.ok) {
        throw new Error(`Failed to fetch agent card from ${this.cardUrl}: ${res.status} ${res.statusText}`);
      }

      this.card = await res.json() as AgentCard;

      // Update baseUrl from the card if it provides one
      if (this.card.url) {
        this.baseUrl = this.card.url.replace(/\/$/, '');
      }
    } finally {
      clearTimeout(timeout);
    }
  }

  // -----------------------------------------------------------------------
  // Runnable interface
  // -----------------------------------------------------------------------

  async run(messages: Message[]): Promise<string> {
    await this.init();

    const payload = {
      input: messages.map((m) => ({
        role: m.role,
        content: m.content,
      })),
    };

    const trace = getRequestContext()?.trace;
    const span = trace
      ? addSpan(trace, {
          type: 'agent_call',
          name: this.card?.name ?? agentNameFromUrl(this.baseUrl),
          input: truncate(messages),
          metadata: { childUrl: this.baseUrl },
        })
      : null;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const res = await fetch(`${this.baseUrl}/responses`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...this.headers,
          ...traceHeadersOut(trace),
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });

      const childTraceId = traceIdFromResponse(res);
      if (childTraceId && span?.metadata) span.metadata.childTraceId = childTraceId;

      if (!res.ok) {
        throw new Error(
          `Remote agent ${this.card?.name ?? this.baseUrl} returned ${res.status}: ${await res.text()}`
        );
      }

      const data = await res.json() as ResponsesOutput;
      const text = this.extractText(data);
      if (span) span.output = truncate(text);
      return text;
    } catch (err) {
      if (span?.metadata) span.metadata.error = (err as Error).message;
      throw err;
    } finally {
      clearTimeout(timeout);
      if (span) endSpan(span);
    }
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    await this.init();

    const payload = {
      input: messages.map((m) => ({
        role: m.role,
        content: m.content,
      })),
      stream: true,
    };

    const trace = getRequestContext()?.trace;
    const span = trace
      ? addSpan(trace, {
          type: 'agent_call',
          name: this.card?.name ?? agentNameFromUrl(this.baseUrl),
          input: truncate(messages),
          metadata: { childUrl: this.baseUrl, streaming: true },
        })
      : null;

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);

    try {
      const res = await fetch(`${this.baseUrl}/responses`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          ...this.headers,
          ...traceHeadersOut(trace),
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });

      const childTraceId = traceIdFromResponse(res);
      if (childTraceId && span?.metadata) span.metadata.childTraceId = childTraceId;

      if (!res.ok) {
        throw new Error(
          `Remote agent ${this.card?.name ?? this.baseUrl} stream returned ${res.status}: ${await res.text()}`
        );
      }

      // If the response is SSE, parse it
      if (res.headers.get('content-type')?.includes('text/event-stream') && res.body) {
        yield* this.parseSSE(res.body);
      } else {
        // Fallback: non-streaming response — yield the full text
        const data = await res.json() as ResponsesOutput;
        const text = this.extractText(data);
        if (span) span.output = truncate(text);
        yield text;
      }
    } catch (err) {
      if (span?.metadata) span.metadata.error = (err as Error).message;
      throw err;
    } finally {
      clearTimeout(timeout);
      if (span) endSpan(span);
    }
  }

  collectTools(): AgentTool[] {
    // Remote agents don't expose local tools — their skills are listed
    // on the card but executed remotely via run()/stream().
    return [];
  }

  // -----------------------------------------------------------------------
  // Accessors for card metadata
  // -----------------------------------------------------------------------

  get name(): string {
    return this.card?.name ?? 'remote-agent';
  }

  get description(): string {
    return this.card?.description ?? '';
  }

  get skills(): Array<{ id: string; name: string; description: string }> {
    return this.card?.skills ?? [];
  }

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  private extractText(data: ResponsesOutput): string {
    try {
      return data.output[0].content[0].text;
    } catch {
      // Fallback: serialise the whole output
      return JSON.stringify(data);
    }
  }

  private async *parseSSE(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        // Keep the last partial line in the buffer
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const payload = line.slice(6).trim();
            if (payload === '[DONE]') return;

            try {
              const event = JSON.parse(payload) as Record<string, unknown>;
              // Handle output_text delta events
              if (typeof event.delta === 'string') {
                yield event.delta;
              } else if (typeof event.text === 'string') {
                yield event.text;
              }
            } catch {
              // Non-JSON data line — yield as-is if non-empty
              if (payload) yield payload;
            }
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
}
