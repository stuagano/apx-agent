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
        "server/server.ts": _server_ts(),
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

const appPort = process.env.DATABRICKS_APP_PORT ?? process.env.PORT ?? '3000';
const bridgePort = process.env.APX_PYTHON_BRIDGE_PORT ?? (appPort === '8000' ? '8001' : '8000');
const bridgeUrl = process.env.APX_PYTHON_BRIDGE_URL ?? `http://127.0.0.1:${bridgePort}`;
const bridgeApp = process.env.APX_PYTHON_BRIDGE_APP ?? 'agent_server.appkit_bridge:app';
const children = [];
let shuttingDown = false;

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


def _server_ts() -> str:
    return """\
import { createApp, server, type IAppRouter } from '@databricks/appkit';
import http from 'node:http';
import { agents } from '@databricks/appkit/beta';
import type { Request, Response } from 'express';

import manifest from '../apx-host-manifest.json';
import {
  internalApxAppKitAgentsOptionsFromManifest,
  internalApxAppKitGovernance,
  type InternalApxAppsHostManifest,
} from 'apx-internal-runtime/internal/appkit-host';

const apxManifest = manifest as InternalApxAppsHostManifest;
const pythonBridgeUrl = process.env.APX_PYTHON_BRIDGE_URL ?? 'http://127.0.0.1:8000';
const proxyPaths = (process.env.APX_PYTHON_BRIDGE_PROXY_PATHS ?? '')
  .split(',')
  .map((path) => path.trim())
  .filter(Boolean);

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
    if (proxyPaths.length === 0) return;
    appkit.server.extend((app: IAppRouter) => {
      for (const prefix of proxyPaths) {
        app.use(prefix, (req: Request, res: Response) => {
          const target = new URL(req.originalUrl, pythonBridgeUrl);
          const headers = { ...req.headers };
          delete headers.host;
          const upstream = http.request(target, { method: req.method, headers }, (response) => {
            res.writeHead(response.statusCode ?? 502, response.headers);
            response.pipe(res);
          });
          upstream.on('error', () => {
            if (!res.headersSent) res.status(502).json({ detail: 'APX Python bridge unavailable' });
          });
          req.pipe(upstream);
        });
      }
    });
  },
});
"""


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
