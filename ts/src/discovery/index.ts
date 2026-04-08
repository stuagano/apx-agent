/**
 * Discovery plugin for Databricks AppKit.
 *
 * Serves an A2A agent card at /.well-known/agent.json and optionally
 * auto-registers with an agent registry on startup.
 *
 * Usage:
 *   import { discovery } from 'appkit-agent';
 *
 *   createApp({
 *     plugins: [
 *       agent({ model: '...', tools: [...] }),
 *       discovery({ registry: '$AGENT_HUB_URL' }),
 *     ],
 *   });
 */

import type { IAppRouter } from '@databricks/appkit';

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
  skills: Array<{
    id: string;
    name: string;
    description: string;
  }>;
  mcpEndpoint?: string;
}

export interface DiscoveryConfig {
  /** Agent name (defaults to agent plugin config name). */
  name?: string;
  /** Agent description. */
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
// Discovery plugin factory
// ---------------------------------------------------------------------------

export function discovery(config: DiscoveryConfig = {}) {
  let agentExports: {
    getTools: () => Array<{ name: string; description: string }>;
    getConfig: () => { model: string; instructions?: string };
  } | null = null;

  return {
    name: 'discovery',
    displayName: 'Agent Discovery',
    description: 'A2A agent card and registry auto-registration',

    async setup(appkit: { agent?: { getTools: () => unknown; getConfig: () => unknown } }) {
      // Grab exports from the agent plugin if available
      agentExports = appkit.agent as typeof agentExports ?? null;

      // Auto-register with registry if configured
      if (config.registry) {
        const registryUrl = resolveEnvVar(config.registry);
        const publicUrl = config.url ? resolveEnvVar(config.url) : '';

        if (registryUrl) {
          // Fire-and-forget — don't block startup
          setTimeout(() => registerWithHub(registryUrl, publicUrl), 2000);
        }
      }
    },

    injectRoutes(router: IAppRouter) {
      router.get('/.well-known/agent.json', (req, res) => {
        const tools = agentExports?.getTools() ?? [];
        const agentConfig = agentExports?.getConfig();
        const baseUrl = `${req.protocol}://${req.get('host')}`;

        const card: AgentCard = {
          schemaVersion: '1.0',
          name: config.name ?? agentConfig?.model ?? 'agent',
          description: config.description ?? '',
          url: baseUrl,
          protocolVersion: '0.3.0',
          capabilities: { streaming: true, multiTurn: true },
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
      console.warn(`Registry registration failed: ${response.status} ${response.statusText}`);
      return;
    }

    const data = await response.json();
    console.log(`Registered with agent registry at ${url} as '${data.id ?? 'unknown'}'`);
  } catch (err) {
    console.warn(`Failed to register with agent registry at ${registryUrl}:`, err);
  }
}
