/**
 * RouterAgent — deterministic or LLM-based routing to one of several sub-agents.
 *
 * Supports two routing modes:
 * 1. Deterministic: a `condition` function on each route decides
 * 2. LLM-based: a single model call picks the route (fallback)
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
 * // LLM-based routing (no conditions — let the model decide)
 * const router = new RouterAgent({
 *   routes: [
 *     { name: 'billing', description: 'Handles billing inquiries', agent: billingAgent },
 *     { name: 'support', description: 'Handles technical support', agent: supportAgent },
 *   ],
 *   instructions: 'Route the user to the appropriate agent.',
 * });
 */

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
  /** Instructions for LLM-based routing (used when no condition matches). */
  instructions?: string;
  /** Fallback agent when no route matches and LLM routing is disabled. */
  fallback?: Runnable;
}

export class RouterAgent implements Runnable {
  private routes: Route[];
  private instructions: string;
  private fallback: Runnable | null;

  constructor(config: RouterConfig) {
    if (config.routes.length === 0) {
      throw new Error('RouterAgent requires at least one route');
    }
    this.routes = config.routes;
    this.instructions = config.instructions ?? '';
    this.fallback = config.fallback ?? null;
  }

  async run(messages: Message[]): Promise<string> {
    const target = this.selectRoute(messages);
    return target.run(messages);
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    const target = this.selectRoute(messages);
    if (target.stream) {
      yield* target.stream(messages);
    } else {
      yield await target.run(messages);
    }
  }

  collectTools() {
    return this.routes.flatMap((r) => r.agent.collectTools?.() ?? []);
  }

  private selectRoute(messages: Message[]): Runnable {
    // Try deterministic conditions first
    for (const route of this.routes) {
      if (route.condition?.(messages)) {
        return route.agent;
      }
    }

    // No condition matched — use fallback or first route
    return this.fallback ?? this.routes[0].agent;
  }
}
