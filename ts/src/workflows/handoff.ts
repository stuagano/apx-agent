/**
 * HandoffAgent — multi-agent system where agents can hand off control mid-conversation.
 *
 * Each agent gets transfer tools (`transfer_to_<name>`) injected automatically.
 * When an agent calls a transfer tool, execution switches to the target agent
 * with the full conversation history.
 *
 * @example
 * const system = new HandoffAgent({
 *   agents: { billing: billingAgent, support: supportAgent, triage: triageAgent },
 *   start: 'triage',
 *   maxHandoffs: 3,
 * });
 *
 * // triage agent can call transfer_to_billing or transfer_to_support
 * const result = await system.run([{ role: 'user', content: 'I have a billing question' }]);
 */

import type { Message, Runnable } from './types.js';

export interface HandoffConfig {
  /** Named agents that can hand off to each other. */
  agents: Record<string, Runnable>;
  /** Which agent starts the conversation. */
  start: string;
  /** Maximum number of handoffs before forcing a response. */
  maxHandoffs?: number;
  /**
   * Handoff callback — called when a handoff occurs. Use for logging,
   * metrics, or injecting context into the conversation.
   */
  onHandoff?: (from: string, to: string, context: string) => void;
}

/**
 * Wraps a Runnable to detect handoff requests in its output.
 *
 * The wrapped agent's response is checked for `transfer_to_<name>` patterns.
 * This is a simple text-matching approach — for real production use, the
 * underlying agent should use function calling with transfer tools.
 */
export class HandoffAgent implements Runnable {
  private agents: Record<string, Runnable>;
  private start: string;
  private maxHandoffs: number;
  private onHandoff: HandoffConfig['onHandoff'];

  constructor(config: HandoffConfig) {
    if (!(config.start in config.agents)) {
      throw new Error(`HandoffAgent start='${config.start}' not found in agents`);
    }
    this.agents = config.agents;
    this.start = config.start;
    this.maxHandoffs = config.maxHandoffs ?? 5;
    this.onHandoff = config.onHandoff;
  }

  async run(messages: Message[]): Promise<string> {
    let currentName = this.start;
    let context = [...messages];
    let result = '';

    for (let i = 0; i <= this.maxHandoffs; i++) {
      const agent = this.agents[currentName];
      if (!agent) {
        return `Error: agent '${currentName}' not found`;
      }

      // Inject transfer instructions
      const transferNames = Object.keys(this.agents).filter((n) => n !== currentName);
      const transferInstructions = transferNames.length
        ? `\nYou can hand off to: ${transferNames.map((n) => `transfer_to_${n}`).join(', ')}. ` +
          `To hand off, respond with exactly "TRANSFER: <agent_name>" on its own line.`
        : '';

      const agentMessages: Message[] = [
        ...context,
        ...(transferInstructions
          ? [{ role: 'system', content: transferInstructions }]
          : []),
      ];

      result = await agent.run(agentMessages);

      // Check for handoff in response
      const handoffMatch = result.match(/TRANSFER:\s*(\w+)/i);
      if (handoffMatch) {
        const targetName = handoffMatch[1];
        if (targetName in this.agents && targetName !== currentName) {
          this.onHandoff?.(currentName, targetName, result);
          context = [
            ...context,
            { role: 'assistant', content: result },
            { role: 'system', content: `[Handed off from ${currentName} to ${targetName}]` },
          ];
          currentName = targetName;
          continue;
        }
      }

      // No handoff — we're done
      break;
    }

    return result;
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    yield await this.run(messages);
  }

  collectTools() {
    return Object.values(this.agents).flatMap((a) => a.collectTools?.() ?? []);
  }
}
