/**
 * Discovery plugin for Databricks AppKit.
 *
 * Serves an A2A agent card at /.well-known/agent.json and optionally
 * auto-registers with an agent registry on startup.
 */

import type { Request, Response } from 'express';
import type { AgentExports } from '../agent/index.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgentCard {
  schemaVersion: string;
  name: string;
  description: string;
  url: string;
  protocolVersion: string;
  capabilities: { streaming: boolean; multiTurn: boolean };
  authentication: { schemes: string[]; credentials: string };
  skills: Array<{ id: string; name: string; description: string }>;
  mcpEndpoint?: string;
}

export interface DiscoveryConfig {
  name?: string;
  description?: string;
  /** Public URL of this agent (supports $ENV_VAR). */
  url?: string;
  /** URL of an agent registry to auto-register with on startup. */
  registry?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolveEnvVar(value: string): string {
  if (!value.startsWith('$')) return value;
  const varName = value.replace(/^\$\{?/, '').replace(/\}$/, '');
  return process.env[varName] ?? '';
}

// ---------------------------------------------------------------------------
// Plugin factory
// ---------------------------------------------------------------------------

export function createDiscoveryPlugin(config: DiscoveryConfig, agentExports: () => AgentExports | null) {
  return {
    name: 'discovery' as const,
    displayName: 'Agent Discovery',
    description: 'A2A agent card and registry auto-registration',

    setup() {
      if (config.registry) {
        const registryUrl = resolveEnvVar(config.registry);
        const publicUrl = config.url ? resolveEnvVar(config.url) : '';
        if (registryUrl) {
          setTimeout(() => registerWithHub(registryUrl, publicUrl), 2000);
        }
      }
    },

    injectRoutes(router: { get: Function }) {
      router.get('/.well-known/agent.json', (req: Request, res: Response) => {
        const exports = agentExports();
        const tools = exports?.getTools() ?? [];
        const agentConfig = exports?.getConfig();
        const baseUrl = `${req.protocol}://${req.get('host')}`;

        const card: AgentCard = {
          schemaVersion: '1.0',
          name: config.name ?? agentConfig?.model ?? 'agent',
          description: config.description ?? '',
          url: baseUrl,
          protocolVersion: '0.3.0',
          capabilities: { streaming: true, multiTurn: true },
          authentication: { schemes: ['bearer'], credentials: 'same_origin' },
          skills: tools.map((t) => ({
            id: t.name,
            name: t.name,
            description: t.description,
          })),
          mcpEndpoint: `${baseUrl}/mcp`,
        };

        res.json(card);
      });
    },
  };
}

async function registerWithHub(registryUrl: string, publicUrl: string): Promise<void> {
  try {
    const url = registryUrl.replace(/\/$/, '');
    const body: Record<string, string> = {};
    if (publicUrl) body.url = publicUrl.replace(/\/$/, '');

    const response = await fetch(`${url}/api/agents/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      console.warn(`Registry registration failed: ${response.status}`);
      return;
    }

    const data = await response.json() as Record<string, unknown>;
    console.log(`Registered with agent registry at ${url} as '${data.id ?? 'unknown'}'`);
  } catch (err) {
    console.warn(`Failed to register with agent registry:`, err);
  }
}
