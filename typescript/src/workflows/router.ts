/**
 * RouterAgent — deterministic or LLM-based routing to one of several sub-agents.
 *
 * Two routing modes, tried in order:
 *   1. Deterministic — each route's `condition` callback is checked; first match wins.
 *   2. LLM-based — when `model` + `instructions` are set and no condition matched,
 *      a single FMAPI call picks the route via forced tool_choice on a `select_route`
 *      function whose `route_name` enum is the set of route names.
 *
 * If both modes fail (no condition, no LLM config, or LLM error), falls back to
 * `fallback` agent or the first route.
 *
 * @example
 * // Deterministic routing
 * const router = new RouterAgent({
 *   routes: [
 *     { name: 'billing', description: 'Billing questions', agent: billingAgent,
 *       condition: (msgs) => msgs.some(m => m.content.includes('bill')) },
 *     { name: 'triage', description: 'Data triage', agent: triageAgent,
 *       condition: (msgs) => msgs.some(m => m.content.includes('missing')) },
 *   ],
 *   fallback: generalAgent,
 * });
 *
 * @example
 * // LLM-based routing (no conditions — let the model decide)
 * const router = new RouterAgent({
 *   model: 'databricks-claude-sonnet-4-6',
 *   instructions: 'Route the user to the most appropriate agent.',
 *   routes: [
 *     { name: 'billing', description: 'Handles billing and payment inquiries', agent: billingAgent },
 *     { name: 'support', description: 'Handles technical support issues', agent: supportAgent },
 *   ],
 * });
 */

import { resolveToken } from '../connectors/types.js';
import type { Message, Runnable } from './types.js';

export interface Route {
  name: string;
  description: string;
  agent: Runnable;
  /** Deterministic condition. If provided and returns true, this route is selected. */
  condition?: (messages: Message[]) => boolean;
}

export interface RouterConfig {
  routes: Route[];
  /**
   * Model identifier for LLM-based routing (e.g. 'databricks-claude-sonnet-4-6').
   * LLM routing activates only when both `model` and `instructions` are set.
   */
  model?: string;
  /** Instructions for LLM-based routing (used when no deterministic condition matches). */
  instructions?: string;
  /** Fallback agent when no route matches (deterministic or LLM). */
  fallback?: Runnable;
}

export class RouterAgent implements Runnable {
  private routes: Route[];
  private model: string | null;
  private instructions: string;
  private fallback: Runnable | null;

  constructor(config: RouterConfig) {
    if (config.routes.length === 0) {
      throw new Error('RouterAgent requires at least one route');
    }
    this.routes = config.routes;
    this.model = config.model ?? null;
    this.instructions = config.instructions ?? '';
    this.fallback = config.fallback ?? null;
  }

  async run(messages: Message[]): Promise<string> {
    const target = await this.selectRoute(messages);
    return target.run(messages);
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    const target = await this.selectRoute(messages);
    if (target.stream) {
      yield* target.stream(messages);
    } else {
      yield await target.run(messages);
    }
  }

  collectTools() {
    return this.routes.flatMap((r) => r.agent.collectTools?.() ?? []);
  }

  private async selectRoute(messages: Message[]): Promise<Runnable> {
    // 1. Try deterministic conditions first
    for (const route of this.routes) {
      if (route.condition?.(messages)) {
        return route.agent;
      }
    }

    // 2. Try LLM-based routing if model + instructions are configured
    if (this.model && this.instructions) {
      const picked = await this.llmSelectRoute(messages);
      if (picked) return picked;
    }

    // 3. Fallback
    return this.fallback ?? this.routes[0].agent;
  }

  // ---------------------------------------------------------------------------
  // LLM routing — single FMAPI call with forced tool_choice
  // ---------------------------------------------------------------------------

  private async llmSelectRoute(messages: Message[]): Promise<Runnable | null> {
    try {
      const routeNames = this.routes.map((r) => r.name);
      const routeDescriptions = this.routes
        .map((r) => `  - "${r.name}": ${r.description}`)
        .join('\n');

      const systemPrompt = [
        this.instructions,
        '',
        'Available routes:',
        routeDescriptions,
        '',
        'Select the most appropriate route by calling the select_route tool.',
      ].join('\n');

      const tool = {
        type: 'function' as const,
        function: {
          name: 'select_route',
          description: 'Select which route to send the conversation to',
          parameters: {
            type: 'object',
            properties: {
              route_name: {
                type: 'string',
                enum: routeNames,
                description: 'The name of the route to select',
              },
            },
            required: ['route_name'],
            additionalProperties: false,
          },
        },
      };

      const chatMessages = [
        { role: 'system' as const, content: systemPrompt },
        ...messages.map((m) => ({
          role: m.role as 'user' | 'assistant',
          content: m.content,
        })),
      ];

      const host = process.env.DATABRICKS_HOST;
      if (!host) {
        console.warn('[RouterAgent] DATABRICKS_HOST not set, skipping LLM routing');
        return null;
      }
      const normalizedHost = host.startsWith('http') ? host.replace(/\/$/, '') : `https://${host}`;

      const token = await resolveToken();

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 30_000);

      const res = await fetch(`${normalizedHost}/serving-endpoints/chat/completions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          model: this.model,
          messages: chatMessages,
          tools: [tool],
          tool_choice: { type: 'function', function: { name: 'select_route' } },
        }),
        signal: controller.signal,
      });
      clearTimeout(timer);

      if (!res.ok) {
        const text = await res.text();
        console.warn(`[RouterAgent] LLM routing failed (${res.status}): ${text.slice(0, 200)}`);
        return null;
      }

      const data = await res.json() as {
        choices?: Array<{
          message?: {
            tool_calls?: Array<{
              function: { name: string; arguments: string };
            }>;
          };
        }>;
      };

      const toolCall = data.choices?.[0]?.message?.tool_calls?.[0];
      if (!toolCall || toolCall.function.name !== 'select_route') {
        console.warn('[RouterAgent] LLM did not return expected select_route tool call');
        return null;
      }

      const args = JSON.parse(toolCall.function.arguments) as { route_name: string };
      const matched = this.routes.find((r) => r.name === args.route_name);
      if (!matched) {
        console.warn(`[RouterAgent] LLM selected unknown route "${args.route_name}"`);
        return null;
      }

      return matched.agent;
    } catch (err) {
      console.warn('[RouterAgent] LLM routing error, falling back:', (err as Error).message);
      return null;
    }
  }
}
