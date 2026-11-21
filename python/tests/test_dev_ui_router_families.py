"""build_dev_ui_router composes route families; it defines no route closures."""

import ast
from pathlib import Path

from apx_agent._dev import build_dev_ui_router

_SRC = Path(__file__).resolve().parents[1] / "src/apx_agent"
_FAMILIES = [
    "approvals",
    "chat",
    "conversations",
    "deploy",
    "discover",
    "edit",
    "eval_ui",
    "grounding",
    "hub",
    "probe",
    "replay",
    "setup",
    "tools",
    "topology",
    "traces",
]
# Method + path, registration order, HEAD omitted. Specific routes stay
# ahead of the parameterized sibling in the same family.
_ROUTES = [
    "GET /_apx/agent",
    "GET /_apx/chat",
    "GET /_apx/tools",
    "GET /_apx/tools/list",
    "GET /_apx/tools/file",
    "POST /_apx/tools/save",
    "GET /_apx/openapi.json",
    "GET /_apx/tools/schema",
    "POST /_apx/tools/suggest",
    "POST /_apx/tools/new",
    "DELETE /_apx/tools/{fn_name}",
    "GET /_apx/probe",
    "GET /_apx/probe/checks",
    "GET /_apx/conversations",
    "GET /_apx/conversations/{conv_id}/items",
    "GET /_apx/approvals",
    "POST /_apx/approvals/{approval_id}/approve",
    "POST /_apx/approvals/{approval_id}/deny",
    "GET /_apx/traces",
    "GET /_apx/traces/last-route",
    "GET /_apx/traces/latency",
    "GET /_apx/traces/tool-fails",
    "GET /_apx/traces/diff",
    "GET /_apx/traces/{trace_id:path}",
    "GET /_apx/topology",
    "GET /_apx/topology.json",
    "GET /_apx/topology/digest",
    "GET /_apx/topology/inspect/{node_id:path}",
    "GET /_apx/topology/tracing",
    "POST /_apx/topology/tracing",
    "GET /_apx/topology/assets/{path:path}",
    "GET /_apx/vendor/{filename}",
    "POST /_apx/replay/tool",
    "POST /_apx/replay/llm",
    "GET /_apx/edit",
    "POST /_apx/edit",
    "POST /_apx/edit/preview",
    "POST /_apx/edit/optimize-instructions",
    "GET /_apx/deploy/stream",
    "GET /_apx/setup",
    "GET /_apx/workspace-context",
    "GET /_apx/setup/catalogs",
    "GET /_apx/setup/schemas",
    "GET /_apx/setup/tables",
    "GET /_apx/setup/warehouses",
    "GET /_apx/setup/agents",
    "POST /_apx/setup",
    "POST /_apx/setup/generate-instructions",
    "POST /_apx/setup/apply-instructions",
    "GET /_apx/setup/tools",
    "POST /_apx/setup/create-tool",
    "POST /_apx/wizard/generate-tools",
    "POST /_apx/setup/wire-agent",
    "POST /_apx/setup/agents",
    "GET /_apx/setup/probe-json",
    "GET /_apx/setup/vs-indexes",
    "GET /_apx/setup/agent-pattern",
    "POST /_apx/setup/agent-pattern",
    "POST /_apx/setup/compose",
    "GET /_apx/wizard",
    "GET /_apx/wizard/tables",
    "GET /_apx/discover",
    "GET /_apx/discover/graph.json",
    "GET /_apx/discover/impact/{node_id:path}",
    "GET /_apx/hub",
    "POST /_apx/hub/agents",
    "GET /_apx/hub/agents",
    "GET /_apx/hub/agents/{agent_id}",
    "DELETE /_apx/hub/agents/{agent_id}",
    "POST /_apx/hub/agents/{agent_id}/refresh",
    "POST /_apx/hub/agents/{agent_id}/invoke",
    "GET /_apx/hub/fleet",
    "GET /_apx/grounding",
    "GET /_apx/knowledge",
    "GET /_apx/grounding/columns",
    "POST /_apx/grounding/columns",
    "POST /_apx/grounding/generate",
    "POST /_apx/grounding/suggest",
    "GET /_apx/memories",
    "GET /_apx/eval/data",
    "POST /_apx/eval/data",
    "POST /_apx/eval/judge",
    "GET /_apx/eval",
    "POST /_apx/eval/label-start",
    "POST /_apx/eval/label-align",
    "GET /_apx/eval/label-history",
    "POST /_apx/eval/cache/bust",
]


def _functions(node: ast.AST) -> list[ast.AST]:
    return [
        child for child in ast.walk(node)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_build_dev_ui_router_defines_no_route_closures():
    tree = ast.parse((_SRC / "_dev.py").read_text(encoding="utf-8"))
    builder = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_dev_ui_router"
    )
    assert _functions(builder) == [builder]


def test_each_route_family_owns_its_closures():
    names = []
    for path in sorted((_SRC / "_dev_routes").glob("*.py")):
        if path.name in {"__init__.py", "context.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        register = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "register"
        )
        assert len(_functions(register)) > 1
        names.append(path.stem)
    assert names == _FAMILIES


def test_optimize_instructions_still_refuses_a_changed_source():
    text = (_SRC / "_dev_routes" / "edit.py").read_text(encoding="utf-8")
    assert "instruction_source_path = ctx.instruction_source_path" in text
    assert "instruction_source = ctx.instruction_source" in text
    assert "if path != instruction_source_path or source != instruction_source:" in text


def test_dev_ui_routes_keep_family_order():
    live = []
    for route in build_dev_ui_router().routes:
        methods = sorted(method for method in route.methods if method != "HEAD")
        live.append(f"{','.join(methods)} {route.path}")
    assert live == _ROUTES
