from __future__ import annotations

import json
from pathlib import Path

from apx_agent import AgentConfig, LlmAgent
from apx_agent._appkit_host_generator import write_appkit_host_skeleton
from apx_agent._apps_host_manifest import compile_apps_host_manifest


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_writes_generated_appkit_host_skeleton(tmp_path: Path) -> None:
    def lookup_policy(resource: str) -> str:
        """Return policy for a resource."""
        return resource

    manifest = compile_apps_host_manifest(
        LlmAgent(name="Pricing Agent", tools=[lookup_policy]),
        AgentConfig(name="Pricing Agent", model="databricks-claude-sonnet-4-5"),
    )

    host_dir = write_appkit_host_skeleton(
        tmp_path,
        manifest,
        runtime_dependency="file:/repo/typescript",
    )

    assert host_dir == tmp_path / ".build" / "apx_appkit_host"
    start_mjs = (host_dir / "scripts" / "start.mjs").read_text()
    assert "agent_server.appkit_bridge:app" in start_mjs
    assert "agent_server.start_server:app" not in start_mjs
    assert "APX_PYTHON_BRIDGE_PORT" in start_mjs
    assert "APX_PYTHON_BRIDGE_URL" in start_mjs
    assert "APX_PYTHON_BRIDGE_CWD" in start_mjs
    assert "--preserve-symlinks" in start_mjs
    assert "server/server.ts" in start_mjs
    assert "'127.0.0.1'" in start_mjs
    assert (host_dir / "server" / "server.ts").read_text() == (
        "import { createApp, server } from '@databricks/appkit';\n"
        "import { agents } from '@databricks/appkit/beta';\n"
        "\n"
        "import manifest from '../apx-host-manifest.json';\n"
        "import {\n"
        "  internalApxAppKitGovernance,\n"
        "  type InternalApxAppsHostManifest,\n"
        "} from 'apx-internal-runtime/internal/appkit-host';\n"
        "\n"
        "const apxManifest = manifest as InternalApxAppsHostManifest;\n"
        "\n"
        "await createApp({\n"
        "  plugins: [\n"
        "    server(),\n"
        "    internalApxAppKitGovernance({\n"
        "      manifest: apxManifest,\n"
        "      pythonBridge: { baseUrl: process.env.APX_PYTHON_BRIDGE_URL ?? 'http://127.0.0.1:8000' },\n"
        "    }),\n"
        "    agents(),\n"
        "  ],\n"
        "});\n"
    )
    agent_ts = host_dir / "server" / "agents" / "pricing-agent" / "agent.ts"
    assert "createInternalApxAppKitAgentDefinitionFromManifest" in agent_ts.read_text()

    package_json = read_json(host_dir / "package.json")
    assert package_json["private"] is True
    assert (
        package_json["dependencies"]["apx-internal-runtime"] == "file:/repo/typescript"
    )
    assert package_json["dependencies"]["tsx"] == "^4.20.0"
    assert package_json["dependencies"]["zod"] == "^4.0.0"
    assert package_json["dependencies"]["zod-to-json-schema"] == "^3.25.0"
    assert package_json["scripts"]["build"] == "tsc --noEmit"
    assert package_json["scripts"]["start"] == "node scripts/start.mjs"

    manifest_json = read_json(host_dir / "apx-host-manifest.json")
    assert manifest_json["agent"]["name"] == "Pricing Agent"
    assert manifest_json["tools"][0]["name"] == "lookup_policy"


def test_generated_appkit_host_skeleton_slug_fallback(tmp_path: Path) -> None:
    manifest = compile_apps_host_manifest(LlmAgent(name="!!!", tools=[]))

    host_dir = write_appkit_host_skeleton(tmp_path, manifest)

    assert (host_dir / "server" / "agents" / "agent" / "agent.ts").exists()
