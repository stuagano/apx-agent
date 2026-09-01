"""Generate the internal AppKit host skeleton for Apps deployments."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ._apps_host_manifest import AppsHostManifest


def write_appkit_host_skeleton(
    project_root: Path,
    manifest: AppsHostManifest,
    *,
    runtime_dependency: str = "file:../../typescript",
) -> Path:
    """Write `.build/apx_appkit_host` and return the host directory."""
    host_dir = project_root / ".build" / "apx_appkit_host"
    agents_dir = host_dir / "server" / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
    agent_id = _agent_id(manifest.agent.name)
    files = {
        "package.json": _package_json(manifest.agent.name, runtime_dependency),
        "tsconfig.json": _tsconfig_json(),
        "apx-host-manifest.json": json.dumps(
            manifest.model_dump(mode="json"), indent=2, sort_keys=True
        )
        + "\n",
        "scripts/start.mjs": _start_mjs(),
        "server/server.ts": _server_ts(agent_id),
        f"server/agents/{agent_id}/agent.ts": _agent_ts(),
    }
    for rel, content in files.items():
        path = host_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return host_dir


def _agent_id(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "agent"


def _package_json(name: str, runtime_dependency: str) -> str:
    payload = {
        "name": f"{_agent_id(name)}-apx-appkit-host",
        "private": True,
        "type": "module",
        "scripts": {
            "build": "tsc --noEmit",
            "start": "node scripts/start.mjs",
        },
        "dependencies": {
            "@databricks/appkit": "^0.66.1",
            "apx-internal-runtime": runtime_dependency,
            "tsx": "^4.20.0",
            "zod": "^4.0.0",
            "zod-to-json-schema": "^3.25.0",
        },
        "devDependencies": {
            "@types/express": "^4.17.25",
            "typescript": "~5.9.0",
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _start_mjs() -> str:
    return """\
import { spawn } from 'node:child_process';
import { createWorkspaceClient } from '@databricks/appkit';

const appPort = process.env.DATABRICKS_APP_PORT ?? process.env.PORT ?? '3000';
const bridgePort = process.env.APX_PYTHON_BRIDGE_PORT ?? (appPort === '8000' ? '8001' : '8000');
const bridgeUrl = process.env.APX_PYTHON_BRIDGE_URL ?? `http://127.0.0.1:${bridgePort}`;
const bridgeApp = process.env.APX_PYTHON_BRIDGE_APP ?? 'agent_server.appkit_bridge:app';
const children = [];
let shuttingDown = false;

async function appKitTelemetryEnv() {
  const destination = process.env.MLFLOW_TRACING_DESTINATION?.trim();
  if (!destination) {
    return {};
  }
  const client = createWorkspaceClient();
  await client.config.ensureResolved();
  const host = client.config.host?.replace(/[/]+$/, '');
  const headers = new Headers();
  await client.config.authenticate(headers);
  const authorization = headers.get('authorization');
  if (!host || !authorization) {
    throw new Error('MLflow UC tracing requires an authenticated Databricks workspace client');
  }
  const signalHeaders = (table) => [
    `Authorization=${encodeURIComponent(authorization)}`,
    `X-Databricks-UC-Table-Name=${encodeURIComponent(`${destination}.${table}`)}`,
  ].join(',');
  return {
    OTEL_EXPORTER_OTLP_ENDPOINT: process.env.OTEL_EXPORTER_OTLP_ENDPOINT ?? `${host}/api/2.0/otel`,
    OTEL_EXPORTER_OTLP_TRACES_HEADERS: process.env.OTEL_EXPORTER_OTLP_TRACES_HEADERS
      ?? signalHeaders('mlflow_experiment_trace_otel_spans'),
    OTEL_EXPORTER_OTLP_METRICS_HEADERS: process.env.OTEL_EXPORTER_OTLP_METRICS_HEADERS
      ?? signalHeaders('mlflow_experiment_trace_otel_metrics'),
    OTEL_EXPORTER_OTLP_LOGS_HEADERS: process.env.OTEL_EXPORTER_OTLP_LOGS_HEADERS
      ?? signalHeaders('mlflow_experiment_trace_otel_logs'),
  };
}

const telemetryEnv = await appKitTelemetryEnv();

function start(label, command, args, env = {}) {
  const child = spawn(command, args, {
    cwd: env.APX_PROCESS_CWD ?? process.cwd(),
    env: { ...process.env, ...env },
    stdio: 'inherit',
  });
  children.push(child);
  child.on('exit', (code, signal) => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    for (const other of children) {
      if (other !== child) {
        other.kill('SIGTERM');
      }
    }
    console.error(`${label} exited`, signal ?? code ?? 1);
    process.exit(code ?? 1);
  });
  return child;
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    for (const child of children) {
      child.kill(signal);
    }
  });
}

start('APX Python bridge', process.env.PYTHON ?? 'python', [
  '-m',
  'uvicorn',
  bridgeApp,
  '--host',
  '127.0.0.1',
  '--port',
  bridgePort,
], {
  APX_PROCESS_CWD: process.env.APX_PYTHON_BRIDGE_CWD ?? '..',
});

start('APX AppKit host', process.execPath, ['--import', 'tsx', 'server/server.ts'], {
  ...telemetryEnv,
  APX_PYTHON_BRIDGE_URL: bridgeUrl,
  DATABRICKS_APP_PORT: appPort,
  NODE_OPTIONS: [process.env.NODE_OPTIONS, '--preserve-symlinks'].filter(Boolean).join(' '),
  PORT: appPort,
});
"""


def _tsconfig_json() -> str:
    payload = {
        "compilerOptions": {
            "target": "ES2022",
            "module": "ESNext",
            "moduleResolution": "bundler",
            "strict": True,
            "skipLibCheck": True,
            "resolveJsonModule": True,
            "isolatedModules": True,
            "allowSyntheticDefaultImports": True,
            "noEmit": True,
        },
        "include": ["server/**/*.ts", "apx-host-manifest.json"],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _server_ts(agent_id: str) -> str:
    return """\
import { createApp, server, type IAppRouter } from '@databricks/appkit';
import http from 'node:http';
import { agents } from '@databricks/appkit/beta';
import type { Request, Response } from 'express';
import { z } from 'zod';

import manifest from '../apx-host-manifest.json';
import {
  createInternalApxAppKitDevRuntime,
  internalApxAppKitAgentsOptionsFromManifest,
  internalApxAppKitGovernance,
  type InternalApxAppsHostManifest,
} from 'apx-internal-runtime/internal/appkit-host';

const apxManifest = manifest as InternalApxAppsHostManifest;
const agentId = __APX_AGENT_ID__;
const dev = createInternalApxAppKitDevRuntime(apxManifest);
const devEnabled = process.env.APX_DEV_UI !== '0';
const pythonBridgeUrl = process.env.APX_PYTHON_BRIDGE_URL ?? 'http://127.0.0.1:8000';

await createApp({
  plugins: [
    server({ staticPath: process.env.APX_APPKIT_STATIC_PATH || undefined }),
    internalApxAppKitGovernance({
      manifest: apxManifest,
      pythonBridge: { baseUrl: pythonBridgeUrl },
    }),
    agents(internalApxAppKitAgentsOptionsFromManifest(apxManifest)),
  ],
  onPluginsReady(appkit) {
    appkit.server.extend((app: IAppRouter) => {
      app.get('/api/dev-ui', (_req: Request, res: Response) => res.json({ enabled: devEnabled }));
      const applyDevChange = async (res: Response, change: () => void) => {
        try {
          change();
          await appkit.agents.register(agentId, dev.definition());
          res.json(dev.snapshot());
        } catch (error) {
          res.status(400).json({
            detail: error instanceof Error ? error.message : String(error),
          });
        }
      };

      if (devEnabled) {
        app.get('/api/dev/config', (_req: Request, res: Response) => res.json(dev.snapshot()));
        app.patch('/api/dev/config', async (req: Request, res: Response) => {
          const parsed = z.object({ model: z.string() }).safeParse(req.body);
          if (!parsed.success) {
            res.status(400).json({ detail: 'model is required' });
            return;
          }
          await applyDevChange(res, () => dev.setModel(parsed.data.model));
        });
        app.get('/api/dev/instructions', (_req: Request, res: Response) => res.json(dev.snapshot()));
        app.patch('/api/dev/instructions', async (req: Request, res: Response) => {
          const parsed = z.object({ instructions: z.string() }).safeParse(req.body);
          if (!parsed.success) {
            res.status(400).json({ detail: 'instructions are required' });
            return;
          }
          await applyDevChange(res, () => dev.setInstructions(parsed.data.instructions));
        });
        app.delete('/api/dev/instructions', async (_req: Request, res: Response) => {
          await applyDevChange(res, () => dev.setInstructions(null));
        });
        app.get('/api/dev/tools', (_req: Request, res: Response) => res.json(dev.snapshot()));
        app.patch('/api/dev/tools/:name', async (req: Request, res: Response) => {
          const parsed = z.object({ enabled: z.boolean() }).safeParse(req.body);
          if (!parsed.success) {
            res.status(400).json({ detail: 'enabled must be a boolean' });
            return;
          }
          await applyDevChange(res, () => dev.setToolEnabled(req.params.name, parsed.data.enabled));
        });
        app.put('/api/dev/skills/:name', async (req: Request, res: Response) => {
          const parsed = z.object({
            description: z.string(),
            content: z.string(),
          }).safeParse(req.body);
          if (!parsed.success) {
            res.status(400).json({ detail: 'description and content are required' });
            return;
          }
          await applyDevChange(res, () => dev.setSkill({ name: req.params.name, ...parsed.data }));
        });
        app.delete('/api/dev/skills/:name', async (req: Request, res: Response) => {
          await applyDevChange(res, () => {
            if (!dev.deleteSkill(req.params.name)) throw new Error(`Unknown skill: ${req.params.name}`);
          });
        });
        app.get('/api/dev/prompt', (_req: Request, res: Response) => {
          res.json({ systemPrompt: dev.snapshot().systemPrompt });
        });
      }

      const proxyToPython = (req: Request, res: Response) => {
        const target = new URL(req.originalUrl, pythonBridgeUrl);
        const headers = { ...req.headers };
        const connection = Array.isArray(headers.connection)
          ? headers.connection.join(',')
          : headers.connection ?? '';
        for (const name of connection.split(',')) delete headers[name.trim().toLowerCase()];
        delete headers.host;
        delete headers.connection;
        delete headers['keep-alive'];
        delete headers['proxy-authenticate'];
        delete headers['proxy-authorization'];
        delete headers.te;
        delete headers.trailer;
        delete headers['transfer-encoding'];
        delete headers.upgrade;
        delete headers['content-length'];
        const body = req.readableEnded && req.body !== undefined
          ? JSON.stringify(req.body)
          : undefined;
        if (body !== undefined) headers['content-length'] = Buffer.byteLength(body).toString();
        const upstream = http.request(target, { method: req.method, headers }, (response) => {
          upstream.setTimeout(0);
          const headers = { ...response.headers };
          const connection = Array.isArray(headers.connection)
            ? headers.connection.join(',')
            : headers.connection ?? '';
          for (const name of connection.split(',')) delete headers[name.trim().toLowerCase()];
          delete headers.host;
          delete headers.connection;
          delete headers['keep-alive'];
          delete headers['proxy-authenticate'];
          delete headers['proxy-authorization'];
          delete headers.te;
          delete headers.trailer;
          delete headers['transfer-encoding'];
          delete headers.upgrade;
          delete headers['content-length'];
          res.writeHead(response.statusCode ?? 502, headers);
          response.once('aborted', fail);
          response.once('error', fail);
          response.pipe(res);
        });
        let failed = false;
        const fail = () => {
          if (failed) return;
          failed = true;
          upstream.destroy();
          if (!res.headersSent) res.status(502).json({ detail: 'APX Python bridge unavailable' });
          else res.destroy();
        };
        upstream.once('error', fail);
        upstream.setTimeout(5_000, fail);
        if (body !== undefined) upstream.end(body);
        else req.pipe(upstream);
      };
      app.use('/_apx', proxyToPython);
      app.use('/mcp', proxyToPython);
      app.get('/.well-known/agent.json', proxyToPython);
      app.post('/', proxyToPython);
      app.get('/readyz', proxyToPython);
    });
  },
});
""".replace("__APX_AGENT_ID__", repr(agent_id))


def _agent_ts() -> str:
    return """\
import manifest from '../../../apx-host-manifest.json';
import {
  createInternalApxAppKitAgentDefinitionFromManifest,
  type InternalApxAppsHostManifest,
} from 'apx-internal-runtime/internal/appkit-host';

const apxManifest = manifest as InternalApxAppsHostManifest;

export default createInternalApxAppKitAgentDefinitionFromManifest(apxManifest);
"""
