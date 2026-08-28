"""Generate the internal AppKit host skeleton for Apps deployments."""

from __future__ import annotations

import json
import re
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
    agent_id = _agent_id(manifest.agent.name)
    files = {
        "package.json": _package_json(manifest.agent.name, runtime_dependency),
        "tsconfig.json": _tsconfig_json(),
        "apx-host-manifest.json": json.dumps(
            manifest.model_dump(mode="json"), indent=2, sort_keys=True
        )
        + "\n",
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
        "scripts": {"build": "tsc --noEmit"},
        "dependencies": {
            "@databricks/appkit": "^0.66.1",
            "apx-internal-runtime": runtime_dependency,
        },
        "devDependencies": {"typescript": "~5.9.0"},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


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
import { createApp, server } from '@databricks/appkit';
import { agents } from '@databricks/appkit/beta';

import manifest from '../apx-host-manifest.json';
import {
  internalApxAppKitGovernance,
  type InternalApxAppsHostManifest,
} from 'apx-internal-runtime/internal/appkit-host';

const apxManifest = manifest as InternalApxAppsHostManifest;

await createApp({
  plugins: [
    server(),
    internalApxAppKitGovernance({ manifest: apxManifest }),
    agents(),
  ],
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
