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
    stale_agent = (
        tmp_path
        / ".build"
        / "apx_appkit_host"
        / "server"
        / "agents"
        / "stale-agent"
        / "agent.ts"
    )
    stale_agent.parent.mkdir(parents=True)
    stale_agent.write_text("stale")

    host_dir = write_appkit_host_skeleton(
        tmp_path,
        manifest,
        runtime_dependency="file:/repo/typescript",
    )

    assert host_dir == tmp_path / ".build" / "apx_appkit_host"
    start_mjs = (host_dir / "scripts" / "start.mjs").read_text()
    assert "agent_server.appkit_bridge:app" in start_mjs
    assert "APX_PYTHON_BRIDGE_APP" in start_mjs
    assert "agent_server.start_server:app" not in start_mjs
    assert "APX_PYTHON_BRIDGE_PORT" in start_mjs
    assert "APX_PYTHON_BRIDGE_URL" in start_mjs
    assert "appPort === '8000' ? '8001' : '8000'" in start_mjs
    assert "APX_PYTHON_BRIDGE_CWD" in start_mjs
    assert "--preserve-symlinks" in start_mjs
    assert "server/server.ts" in start_mjs
    assert "'127.0.0.1'" in start_mjs
    assert "createWorkspaceClient" in start_mjs
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in start_mjs
    assert "/api/2.0/otel" in start_mjs
    assert "OTEL_EXPORTER_OTLP_TRACES_HEADERS" in start_mjs
    assert "OTEL_EXPORTER_OTLP_METRICS_HEADERS" in start_mjs
    assert "OTEL_EXPORTER_OTLP_LOGS_HEADERS" in start_mjs
    assert "mlflow_experiment_trace_otel_spans" in start_mjs
    assert "mlflow_experiment_trace_otel_metrics" in start_mjs
    assert "mlflow_experiment_trace_otel_logs" in start_mjs
    server_ts = (host_dir / "server" / "server.ts").read_text()
    assert "APX_APPKIT_STATIC_PATH" in server_ts
    assert "APX_PYTHON_BRIDGE_PROXY_PATHS" not in server_ts
    assert "app.use('/_apx', proxyToPython)" in server_ts
    assert "app.use('/mcp', proxyToPython)" in server_ts
    assert "app.get('/.well-known/agent.json', proxyToPython)" in server_ts
    assert "app.post('/', proxyToPython)" in server_ts
    assert "app.get('/readyz', proxyToPython)" in server_ts
    for path in ("/health", "/chat", "/responses", "/invocations"):
        assert f"'{path}', proxyToPython" not in server_ts
    for header in ("host", "connection"):
        assert server_ts.count(f"delete headers.{header}") == 2
    for header in ("transfer-encoding", "content-length"):
        assert server_ts.count(f"delete headers['{header}']") == 2
    assert "if (body !== undefined) upstream.end(body);" in server_ts
    assert "else req.pipe(upstream);" in server_ts
    assert '{ detail: \'APX Python bridge unavailable\' }' in server_ts
    assert "const devEnabled = process.env.APX_DEV_UI !== '0';" in server_ts
    assert "app.get('/api/dev-ui'" in server_ts
    assert "createInternalApxAppKitDevRuntime" in server_ts
    assert "app.get('/api/dev/config'" in server_ts
    assert "app.patch('/api/dev/config'" in server_ts
    assert "app.get('/api/dev/instructions'" in server_ts
    assert "app.patch('/api/dev/instructions'" in server_ts
    assert "app.delete('/api/dev/instructions'" in server_ts
    assert "app.get('/api/dev/tools'" in server_ts
    assert "app.patch('/api/dev/tools/:name'" in server_ts
    assert "app.put('/api/dev/skills/:name'" in server_ts
    assert "app.delete('/api/dev/skills/:name'" in server_ts
    assert "app.get('/api/dev/prompt'" in server_ts
    assert "const agentId = 'pricing-agent';" in server_ts
    assert "await appkit.agents.register(agentId, dev.definition())" in server_ts
    assert server_ts.index("app.get('/api/dev/config'") < server_ts.index(
        "app.use('/_apx', proxyToPython)"
    )
    assert "appkit.server.extend" in server_ts
    assert "http.request" in server_ts
    assert server_ts.startswith(
        "import { createApp, server, type IAppRouter } from '@databricks/appkit';\n"
        "import http from 'node:http';\n"
        "import { agents } from '@databricks/appkit/beta';\n"
        "import type { Request, Response } from 'express';\n"
        "import { z } from 'zod';\n"
    )
    assert (
        "server({ staticPath: process.env.APX_APPKIT_STATIC_PATH || undefined })"
        in server_ts
    )
    assert "  plugins: [\n" in server_ts
    assert (
        "    server({ staticPath: process.env.APX_APPKIT_STATIC_PATH || undefined }),\n"
        "    internalApxAppKitGovernance({\n"
        "      manifest: apxManifest,\n"
        "      pythonBridge: { baseUrl: pythonBridgeUrl },\n"
        "    }),\n"
        "    agents(internalApxAppKitAgentsOptionsFromManifest(apxManifest)),\n"
        "  ],\n"
    ) in server_ts
    agent_ts = host_dir / "server" / "agents" / "pricing-agent" / "agent.ts"
    assert "createInternalApxAppKitAgentDefinitionFromManifest" in agent_ts.read_text()
    assert not stale_agent.exists()

    package_json = read_json(host_dir / "package.json")
    assert package_json["private"] is True
    assert (
        package_json["dependencies"]["apx-internal-runtime"] == "file:/repo/typescript"
    )
    assert package_json["dependencies"]["tsx"] == "^4.20.0"
    assert package_json["dependencies"]["zod"] == "^4.0.0"
    assert package_json["dependencies"]["zod-to-json-schema"] == "^3.25.0"
    assert package_json["devDependencies"]["@types/express"] == "^4.17.25"
    assert package_json["scripts"]["build"] == "tsc --noEmit"
    assert package_json["scripts"]["start"] == "node scripts/start.mjs"

    manifest_json = read_json(host_dir / "apx-host-manifest.json")
    assert manifest_json["agent"]["name"] == "Pricing Agent"
    assert manifest_json["tools"][0]["name"] == "lookup_policy"


def test_generated_appkit_host_skeleton_slug_fallback(tmp_path: Path) -> None:
    manifest = compile_apps_host_manifest(LlmAgent(name="!!!", tools=[]))

    host_dir = write_appkit_host_skeleton(tmp_path, manifest)

    assert (host_dir / "server" / "agents" / "agent" / "agent.ts").exists()
