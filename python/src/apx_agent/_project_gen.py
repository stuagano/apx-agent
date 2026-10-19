"""Project generator — writes a deployable Databricks Apps project from an AgentConfig.

Given an ``AgentConfig`` and a target directory, ``generate_project`` writes:

  target_dir/pyproject.toml             — TOML envelope derived from AgentConfig
  target_dir/agent.py                   — Python agent declaration
  target_dir/agent_server/              — Bundle compatibility hosts only
  target_dir/databricks.yml             — Bundle compatibility targets only
  target_dir/app.yml                    — Bundle compatibility targets only

Native projects compile deployment manifests during ``apx-agent agents deploy``;
they use APX's packaged launcher and require no generated server package or Bundle.
The agent always has a single Python definition: when ``config.template`` is set,
``render_agent_py`` codegens a top-level ``agent.py`` that constructs the template
agent in code (ADK-style), and ``module = "agent:agent"`` is always written.
"""

from __future__ import annotations

import inspect
import keyword
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._project_yml import _build_app_yml, _build_databricks_yml

if TYPE_CHECKING:
    from ._models import AgentConfig


# ---------------------------------------------------------------------------
# TOML helpers (string-building, no third-party library required)
# ---------------------------------------------------------------------------


def _toml_value(v: Any) -> str:
    """Render a Python value as a TOML inline value."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        escaped = (
            v.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\b", "\\b")
            .replace("\t", "\\t")
            .replace("\n", "\\n")
            .replace("\f", "\\f")
            .replace("\r", "\\r")
        )
        escaped = re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
            lambda match: f"\\u{ord(match.group()):04x}",
            escaped,
        )
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(i) for i in v) + "]"
    if v is None:
        return '""'
    return str(v)


def _write_toml_section(
    lines: list[str],
    header: str,
    fields: dict[str, Any],
    *,
    skip_none: bool = True,
) -> None:
    """Append a ``[header]`` TOML section to *lines*.

    Fields with ``None`` values are skipped when *skip_none* is True.
    """
    lines.append(f"[{header}]")
    for key, value in fields.items():
        if skip_none and value is None:
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")


# ---------------------------------------------------------------------------
# File content builders
# ---------------------------------------------------------------------------


def _toml_value_nested(v: Any) -> str:
    """Render a Python value as a TOML inline value, including nested dicts.

    Extends ``_toml_value`` with dict support so that tool tables with nested
    structures (e.g. MCP tool args lists) serialise correctly.

    :param v: Python value to render.
    :returns: A TOML inline representation of *v*.
    """
    if isinstance(v, dict):
        pairs = ", ".join(f"{k} = {_toml_value_nested(val)}" for k, val in v.items())
        return "{" + pairs + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value_nested(i) for i in v) + "]"
    return _toml_value(v)


def _build_pyproject(config: "AgentConfig") -> str:
    """Build pyproject.toml content for *config*.

    Serializes the runtime envelope. Tool declarations belong in the generated
    Python package, never in ``[[tool.apx.tools]]``.

    :param config: Validated ``AgentConfig`` to serialize.
    :returns: Complete pyproject.toml content as a string.
    """
    lines: list[str] = []

    # [project]
    lines.append("[project]")
    lines.append(f'name = "{config.name}"')
    lines.append('version = "0.1.0"')
    lines.append('requires-python = ">=3.11"')
    lines.append("dependencies = [")
    needs_agentkit = config.target == "durable_agent_server" or (
        config.memory is not None and config.memory.type == "managed"
    )
    extra = "eval,agentbricks" if needs_agentkit else "eval"
    if any(
        backend is not None and backend.type == "lakebase"
        for backend in (config.session, config.memory, config.example)
    ):
        extra += ",lakebase"
    lines.append(f'    "apx-agent[{extra}]",')
    lines.append("]")
    lines.append("")

    # [tool.apx.agent]
    lines.append("[tool.apx.agent]")
    lines.append(f'name = "{config.name}"')
    # Always written. An omitted key now loads as durable_agent_server, so a
    # compatibility project that hid responses_agent would change contract.
    lines.append(f"target = {_toml_value(config.target)}")
    if config.recovery:
        lines.append("recovery = true")
    if config.description:
        lines.append(f"description = {_toml_value(config.description)}")
    lines.append(f"model = {_toml_value(config.model)}")
    if config.instructions:
        lines.append(f"instructions = {_toml_value(config.instructions)}")
    # sub_agents — without this the runtime envelope merge never sees the
    # spec's sub-agent URLs and the deployed app silently has no A2A tools.
    if config.sub_agents and not config.agents:
        lines.append(f"sub_agents = {_toml_value(config.sub_agents)}")

    # knowledge — emit only when the caller explicitly set it in the AgentConfig.
    # The auto-default "./.apx/okf" was removed: generate_project writes no .apx/
    # artifact, so auto-emitting a knob pointing at a non-existent bundle would be
    # incoherent. The bundle-writing path (apx-agent scaffold) emits the knob there
    # instead, gated on manifest-is-not-None, so knob ⟺ bundle is always satisfied.
    if config.knowledge is not None:
        lines.append(f"knowledge = {_toml_value(config.knowledge)}")

    # Python-canonical: the agent always has a top-level agent.py (generated from
    # the template by render_agent_py), so the module spec is always written and
    # the [tool.apx.agent.template] section is no longer emitted.
    lines.append('module = "agent:agent"')

    if config.examples:
        lines.append(f"examples = {_toml_value(config.examples)}")
    if config.workflows:
        workflows = [workflow.model_dump() for workflow in config.workflows]
        lines.append(f"workflows = {_toml_value_nested(workflows)}")
    lines.append("")

    if config.bindings:
        _write_toml_section(
            lines,
            "tool.apx.agent.bindings",
            config.bindings,
        )

    # [tool.apx.agent.service_policies] — omit the default empty declaration
    # so existing generated projects remain byte-for-byte compatible.
    service_policies = config.service_policies
    if (
        service_policies.attachments
        or service_policies.abac is not None
        or service_policies.native_mode.value != "off"
        or service_policies.local_mode.value != "mirror"
    ):
        lines.append("[tool.apx.agent.service_policies]")
        lines.append(f"local_mode = {_toml_value(service_policies.local_mode.value)}")
        lines.append(f"native_mode = {_toml_value(service_policies.native_mode.value)}")
        lines.append("")
        if service_policies.abac is not None:
            lines.append("[tool.apx.agent.service_policies.abac]")
            lines.append(
                f"tags = {_toml_value_nested(dict(service_policies.abac.tags))}"
            )
            lines.append("")
        for attachment in service_policies.attachments:
            lines.append("[[tool.apx.agent.service_policies.attachments]]")
            lines.append(f"name = {_toml_value(attachment.name)}")
            lines.append(f"target_type = {_toml_value(attachment.target_type.value)}")
            lines.append(f"target = {_toml_value(attachment.target)}")
            lines.append(f"mode = {_toml_value(attachment.mode.value)}")
            lines.append("")
            for policy in attachment.policies:
                lines.append("[[tool.apx.agent.service_policies.attachments.policies]]")
                lines.append(f"name = {_toml_value(policy.name)}")
                lines.append(f"kind = {_toml_value(policy.kind.value)}")
                if policy.builtin is not None:
                    lines.append(f"builtin = {_toml_value(policy.builtin.value)}")
                if policy.classifier is not None:
                    lines.append(f"classifier = {_toml_value(policy.classifier)}")
                if policy.prompt is not None:
                    lines.append(f"prompt = {_toml_value(policy.prompt)}")
                if policy.function is not None:
                    lines.append(f"function = {_toml_value(policy.function)}")
                lines.append(f"phase = {_toml_value(policy.phase.value)}")
                lines.append(f"rank = {policy.rank}")
                lines.append("")

    # [tool.apx.agent.memory]
    if config.memory is not None:
        _write_toml_section(
            lines, "tool.apx.agent.memory", config.memory.model_dump(exclude_none=True)
        )

    # [tool.apx.agent.session]
    if config.session is not None:
        _write_toml_section(
            lines,
            "tool.apx.agent.session",
            config.session.model_dump(exclude_none=True),
        )

    if config.deploy is not None:
        _write_toml_section(
            lines,
            "tool.apx.agent.deploy",
            config.deploy.model_dump(exclude_none=True, exclude={"autoscale"}),
        )
        if config.deploy.autoscale is not None:
            _write_toml_section(
                lines,
                "tool.apx.agent.deploy.autoscale",
                config.deploy.autoscale.model_dump(),
            )

    return "\n".join(lines)


_START_SERVER_CONTENT = '''\
"""Native APX DurableAgentServer target. Install databricks-agentbricks to use it."""
import os

os.environ.setdefault("APX_RUNTIME", "apps")

from agent import agent
from apx_agent import compile_agent
from apx_agent._defaults import _make_workspace_client
from apx_agent._inspection import _load_agent_config

config = _load_agent_config()
if config is None:
    raise ValueError("The durable target requires [tool.apx.agent] configuration")
ws = _make_workspace_client()
app = compile_agent(
    agent, config=config, target="durable_agent_server",
    model=os.environ.get("APX_MODEL", config.model), service_ws=ws,
    session_store=os.environ.get("AGENT_SESSION_STORE"),
)
'''


_START_HOST_CONTENT = '''\
"""Databricks Apps host selector — framework boilerplate, do not edit.

The only Apps host is DurableAgentServer.
"""
from __future__ import annotations

import os


def main() -> None:
    os.environ.setdefault("APX_RUNTIME", "apps")
    os.execvp(
        "uvicorn",
        ["uvicorn", "agent_server.start_server:app", "--host", "0.0.0.0", "--port", "8000"],
    )
'''


_TEMPLATE_TARGETS = {
    "coworker": ("apx_agent.coworker", "CoworkerAgent"),
    "data": ("apx_agent.data_agent", "DataAgent"),
}


_AGENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _PyExpr(str):
    """String that should be emitted as Python source, not repr() quoted."""


def _agent_var(name: str) -> str:
    """Return a Python variable name for a graph node name."""
    candidate = name.replace("-", "_")
    if not _AGENT_NAME_RE.match(candidate) or keyword.iskeyword(candidate):
        raise ValueError(f"Invalid graph agent name: {name!r}")
    return candidate


def _ctor_kwargs(spec: dict[str, Any]) -> str:
    """Render constructor kwargs, skipping ``None`` values."""
    return ", ".join(
        f"{k}={v if isinstance(v, _PyExpr) else repr(v)}"
        for k, v in spec.items()
        if v is not None
    )


def _render_leaf_tools(
    name: str,
    spec: dict[str, Any],
    blocks: list[str],
    modules: dict[str, str] | None,
) -> str | None:
    """Render leaf tools with explicit factories and flattened toolkit results."""
    from ._tool_codegen import render_tool_group

    raw_tools = spec.pop("tools", None)
    extra_tools = spec.pop("extra_tools", None)
    if raw_tools is None and extra_tools is None:
        return None
    for tools in (raw_tools, extra_tools):
        if tools is not None and not isinstance(tools, list):
            raise ValueError(f"Graph agent {name!r} tools must be a list")
    return _PyExpr(render_tool_group(
        (raw_tools or []) + (extra_tools or []),
        f"{_agent_var(name)}_tools", blocks, modules,
    ))


# Resolved leaf kind -> the apx_agent class the render path forwards kwargs to.
# Aliases collapse to the same class so the allowed-key set is derived once from
# the constructor the generated code actually calls (R-1: never drift from render).
_LEAF_CLASSES = {
    "agent": "Agent",
    "llm": "Agent",
    "llm_agent": "Agent",
    "data": "DataAgent",
    "data_agent": "DataAgent",
    "coworker": "CoworkerAgent",
    "coworker_agent": "CoworkerAgent",
}


@lru_cache(maxsize=None)
def _leaf_ctor_params(class_name: str) -> frozenset[str]:
    """Every named ``__init__`` param across the class and its bases.

    These are exactly the keys the render path may forward to the constructor,
    so validating declared leaf keys against them rejects an unknown key at
    codegen instead of letting it become an import-time ``TypeError`` (FR-3).
    Imported lazily to avoid an import cycle at module load.
    """
    import apx_agent

    cls = getattr(apx_agent, class_name)
    names: set[str] = set()
    for base in cls.__mro__:
        init = base.__dict__.get("__init__")
        if init is None:
            continue
        for pname, param in inspect.signature(init).parameters.items():
            if pname != "self" and param.kind in (
                param.POSITIONAL_OR_KEYWORD,
                param.KEYWORD_ONLY,
            ):
                names.add(pname)
    return frozenset(names)


# Remote leaf keys: RemoteDatabricksAgent params + the YAML ``url`` alias for the
# positional ``card_url``. Hardcoded (not introspected) because ``url`` is a
# render-path alias with no matching constructor param.
_REMOTE_LEAF_KEYS = frozenset(
    {
        "type",
        "url",
        "card_url",
        "app_name",
        "headers",
        "timeout",
        "long_task",
        "max_continuations",
    }
)


def _render_remote_leaf(
    name: str, var: str, spec: dict[str, Any], imports: set[str]
) -> str:
    """Render a ``RemoteDatabricksAgent`` leaf (the A2A / sub_agents story)."""
    imports.add("RemoteDatabricksAgent")
    unknown = set(spec) - _REMOTE_LEAF_KEYS
    if unknown:
        raise ValueError(
            f"Remote graph agent {name!r} has unknown key(s): {sorted(unknown)}"
        )
    url = spec.get("url")
    if url is None:
        url = spec.get("card_url")
    if not isinstance(url, str) or not url:
        raise ValueError(f"Remote graph agent {name!r} requires a non-empty url")
    kwargs = {k: v for k, v in spec.items() if k not in {"type", "url", "card_url"}}
    rendered = _ctor_kwargs(kwargs)
    args = ", ".join([repr(url)] + ([rendered] if rendered else []))
    return f"{var} = RemoteDatabricksAgent({args})"


def _render_leaf(
    name: str, spec: dict[str, Any], imports: set[str],
    blocks: list[str], modules: dict[str, str] | None,
) -> str:
    """Render one YAML graph leaf as a Python assignment."""
    kind = str(spec.get("type", "agent")).replace("-", "_").lower()
    var = _agent_var(name)

    if kind in {"remote", "remote_databricks"}:
        return _render_remote_leaf(name, var, spec, imports)

    class_name = _LEAF_CLASSES.get(kind)
    if class_name is None:
        raise ValueError(f"Unsupported graph agent type {kind!r} for {name!r}")
    # FR-3: reject a leaf key that is not valid for the resolved type, before it
    # becomes an opaque constructor TypeError at import. ``tools`` is a render-path
    # alias (mapped to tools/extra_tools below), so it is always allowed.
    allowed = _leaf_ctor_params(class_name) | {"type", "tools"}
    unknown = set(spec) - allowed
    if unknown:
        raise ValueError(
            f"Graph agent {name!r} ({kind}) has unknown key(s): {sorted(unknown)}"
        )

    kwargs = {k: v for k, v in spec.items() if k != "type"}
    kwargs.setdefault("name", name)
    tools_expr = _render_leaf_tools(name, kwargs, blocks, modules)
    if tools_expr is not None:
        kwargs["tools"] = tools_expr if kind in {"agent", "llm", "llm_agent"} else None
        if kind not in {"agent", "llm", "llm_agent"}:
            kwargs["extra_tools"] = tools_expr

    if kind in {"agent", "llm", "llm_agent"}:
        imports.add("Agent")
        return f"{var} = Agent({_ctor_kwargs(kwargs)})"

    if kind in {"data", "data_agent"}:
        imports.add("DataAgent")
        catalog = kwargs.pop("catalog", None)
        schema = kwargs.pop("schema", None)
        if catalog is None or schema is None:
            raise ValueError(f"Data graph agent {name!r} requires catalog and schema")
        args = ", ".join([repr(catalog), repr(schema), _ctor_kwargs(kwargs)]).rstrip(
            ", "
        )
        return f"{var} = DataAgent({args})"

    if kind in {"coworker", "coworker_agent"}:
        imports.add("CoworkerAgent")
        catalog = kwargs.pop("catalog", None)
        schema = kwargs.pop("schema", None)
        if catalog is None or schema is None:
            raise ValueError(
                f"Coworker graph agent {name!r} requires catalog and schema"
            )
        args = ", ".join([repr(catalog), repr(schema), _ctor_kwargs(kwargs)]).rstrip(
            ", "
        )
        return f"{var} = CoworkerAgent({args})"

    raise ValueError(
        f"Unsupported graph agent type {kind!r} for {name!r}"
    )  # pragma: no cover


def _render_root(
    root: dict[str, Any],
    agent_names: set[str],
    imports: set[str],
    remote_names: set[str],
) -> str:
    """Render the YAML graph root assignment."""
    kind = str(root.get("type", "router")).replace("-", "_").lower()

    # Keys each root kind consumes below. Anything else is rejected (FR-2) rather
    # than silently dropped by the previous ``root.get`` reads.
    allowed_keys = {
        "router": {"type", "agents", "instructions"},
        "sequential": {"type", "agents", "instructions"},
        "parallel": {"type", "agents", "instructions"},
        "handoff": {"type", "agents", "start"},
        "loop": {"type", "agent", "max_iterations"},
    }.get(kind)
    if allowed_keys is None:
        raise ValueError(f"Unsupported graph root type {kind!r}")
    if kind in {"handoff", "loop"} and "instructions" in root:
        raise ValueError(
            f"instructions is only supported on router/sequential/parallel roots, not {kind!r}"
        )
    unknown = set(root) - allowed_keys
    if unknown:
        raise ValueError(f"{kind} root has unknown key(s): {sorted(unknown)}")

    def _refs(key: str = "agents") -> list[str]:
        names = root.get(key)
        if not isinstance(names, list) or not names:
            raise ValueError(f"{kind} root requires a non-empty {key} list")
        missing = [n for n in names if n not in agent_names]
        if missing:
            raise ValueError(f"{kind} root references unknown agents: {missing}")
        return [_agent_var(n) for n in names]

    def _ref(key: str) -> str:
        name = root.get(key)
        if not isinstance(name, str) or not name:
            raise ValueError(f"{kind} root requires {key}")
        if name not in agent_names:
            raise ValueError(f"{kind} root references unknown agent: {name!r}")
        return _agent_var(name)

    def _reject_remote_members() -> None:
        # router/handoff build routing tools that read each member's name at
        # construction; RemoteDatabricksAgent only learns its name from an async
        # card fetch, so a remote member would fail at import. Reject at codegen
        # (consistent with FR-2/FR-3) instead of emitting a graph that dies later.
        remotes = [n for n in root["agents"] if n in remote_names]
        if remotes:
            raise ValueError(
                f"{kind} root cannot reference remote leaf/leaves {sorted(remotes)}: "
                f"{kind} needs a synchronous agent name and RemoteDatabricksAgent has none "
                f"until its card is fetched. Use a sequential/parallel/loop root."
            )

    list_roots = {
        "router": "RouterAgent",
        "sequential": "SequentialAgent",
        "parallel": "ParallelAgent",
    }
    if kind in list_roots:
        cls = list_roots[kind]
        imports.add(cls)
        refs = ", ".join(_refs())
        if kind == "router":
            _reject_remote_members()
        instructions = root.get("instructions")
        extra = f", instructions={instructions!r}" if instructions else ""
        return f"agent = {cls}(agents=[{refs}]{extra})"

    if kind == "handoff":
        imports.add("HandoffAgent")
        refs = ", ".join(_refs())
        _reject_remote_members()
        start = root.get("start")
        if start is not None and start not in agent_names:
            raise ValueError(f"handoff root references unknown start agent: {start!r}")
        extra = f", start={start!r}" if start else ""
        return f"agent = HandoffAgent(agents=[{refs}]{extra})"

    if kind == "loop":
        imports.add("LoopAgent")
        max_iterations = root.get("max_iterations", 5)
        # bool is an int subclass — reject it too (FR-5).
        if (
            not isinstance(max_iterations, int)
            or isinstance(max_iterations, bool)
            or max_iterations <= 0
        ):
            raise ValueError(
                f"loop root max_iterations must be a positive int, got {max_iterations!r}"
            )
        return f"agent = LoopAgent({_ref('agent')}, max_iterations={max_iterations})"

    raise ValueError(f"Unsupported graph root type {kind!r}")  # pragma: no cover


def _render_graph_agent_py(
    config: "AgentConfig", modules: dict[str, str] | None = None,
) -> str:
    """Codegen a top-level ``agent.py`` from YAML graph declarations."""
    if not config.agents or config.root is None:
        raise ValueError("YAML graph generation requires both agents and root")
    if config.template is not None:
        raise ValueError("YAML graph generation cannot also set template")
    if config.tools or config.skills:
        raise ValueError("Graph tools and skills must be declared on a leaf, not at the top level")

    # Distinct leaf names must not collapse to the same Python variable (e.g.
    # "my-agent" and "my_agent" both sanitize to `my_agent`), or one leaf would
    # silently shadow the other and root refs would point at the wrong agent.
    by_var: dict[str, str] = {}
    for name in config.agents:
        var = _agent_var(name)
        if var in by_var:
            raise ValueError(
                f"Graph agent names {by_var[var]!r} and {name!r} both map to variable {var!r}"
            )
        by_var[var] = name

    imports: set[str] = set()
    tool_blocks: list[str] = []
    leaves = [
        _render_leaf(name, spec, imports, tool_blocks, modules)
        for name, spec in config.agents.items()
    ]
    remote_names = {
        name
        for name, spec in config.agents.items()
        if str(spec.get("type", "agent")).replace("-", "_").lower()
        in {"remote", "remote_databricks"}
    }
    root = _render_root(config.root, set(config.agents), imports, remote_names)
    blocks = ["from apx_agent import " + ", ".join(sorted(imports)), *tool_blocks]
    blocks.extend(leaves)
    blocks.append(root)
    return "\n\n".join(blocks) + "\n"


def render_agent_py(
    config: "AgentConfig", *, tool_modules: dict[str, str] | None = None,
) -> str:
    """Codegen a top-level ``agent.py`` that builds the template agent in code.

    The YAML/config deploy path used to ship a config-only project (the runtime
    rebuilt the agent from ``[tool.apx.agent.template]`` via the registry). This
    emits an ADK-style ``agent = CoworkerAgent("cat", "sch", persona=...)`` instead
    so the agent has a single Python definition — editable, debuggable, and visible
    in the dev-UI Edit page. The template's ``build()`` is a pure spec->kwargs
    forward (see coworker.py / data_agent.py), so the constructed agent is identical.

    ``ws`` is intentionally omitted: at import time there is no workspace client,
    and DataAgent/CoworkerAgent fall back to the baked ``.apx/schema.json`` for
    grounding (ws is resolved by the serving runtime later).

    :param config: Validated ``AgentConfig`` whose ``template`` selects the agent.
    :returns: Complete ``agent.py`` source.
    """
    from ._tool_codegen import render_tool_group

    if config.agents or config.root is not None:
        return _render_graph_agent_py(config, tool_modules)

    spec = {k: v for k, v in (config.template or {}).items() if k != "name"}
    raw_tools = list(config.tools)
    for key in ("tools", "extra_tools"):
        extra = spec.pop(key, [])
        if not isinstance(extra, list):
            raise ValueError(f"Template {key} must be a list")
        raw_tools.extend(extra)
    raw_tools.extend({
        "type": "skill", "name": skill.name, "description": skill.description,
        "path": f"skills/{skill.name}.md",
    } for skill in config.skills)
    blocks: list[str] = []
    tools = render_tool_group(raw_tools, "declared", blocks, tool_modules)
    if config.template is None:
        blocks.insert(0, "from apx_agent import LlmAgent")
        blocks.append(f"agent = LlmAgent(name={config.name!r}, tools={tools})")
        return "\n\n".join(blocks) + "\n"

    name = config.template.get("name")
    if raw_tools:
        spec["extra_tools"] = _PyExpr(tools)
    if config.knowledge is not None and "knowledge" not in spec:
        spec["knowledge"] = config.knowledge
    target = _TEMPLATE_TARGETS.get(name or "")
    if target is None:
        # Preserve third-party template construction; tools are already callables.
        blocks.insert(0, "from apx_agent._template import template_registry")
        values = ", ".join(
            f"{key!r}: {value if isinstance(value, _PyExpr) else repr(value)}"
            for key, value in spec.items()
        )
        blocks.append(f"agent = template_registry.build({name!r}, {{{values}}})")
    else:
        module, cls = target
        catalog = spec.pop("catalog", None)
        schema = spec.pop("schema", None)
        spec.setdefault("name", config.name)
        pos = [repr(v) for v in (catalog, schema) if v is not None]
        args = ", ".join([*pos, _ctor_kwargs(spec)]).rstrip(", ")
        blocks.insert(0, f"from {module} import {cls}")
        blocks.append(f"agent = {cls}({args})")
    return "\n\n".join(blocks) + "\n"


_KEEPALIVE_CONTENT = '''\
"""Scheduled keepalive — pings /readyz to prevent cold starts. Do not edit."""
import sys
import urllib.request

url = sys.argv[1]
try:
    urllib.request.urlopen(url, timeout=10)
    print(f"OK: {url}")
except Exception as exc:
    print(f"WARN: {url} — {exc}")
'''


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_project(
    config: "AgentConfig",
    target_dir: Path,
    *,
    source_dir: Path | None = None,
) -> None:
    """Write a deployable Databricks Apps project to *target_dir*.

    *target_dir* is created if it does not exist. Any files that already exist
    in the directory are overwritten.

    When ``config.skills`` is non-empty, each skill's markdown file is copied
    from *source_dir* (or ``target_dir.parent`` when *source_dir* is ``None``)
    into ``target_dir/skills/<skill.name>.md``.

    :param config: Validated ``AgentConfig`` describing the agent.
    :param target_dir: Destination directory for the generated project.
    :param source_dir: Base directory for resolving relative skill paths.
        Defaults to ``target_dir.parent`` when ``None``.
    """
    # Validate declarations and collisions before writing any project files.
    tool_modules: dict[str, str] = {}
    agent_source = render_agent_py(config, tool_modules=tool_modules)
    if tool_modules:
        for name in ("tools.py", "tools"):
            path = target_dir / name
            if path.exists() or path.is_symlink():
                raise ValueError(f"Cannot generate tools package: {path} already exists")
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "pyproject.toml").write_text(_build_pyproject(config))
    (target_dir / "agent.py").write_text(agent_source)
    if tool_modules:
        tools_dir = target_dir / "tools"
        tools_dir.mkdir()
        (tools_dir / "__init__.py").write_text('"""Project-authored tool declarations."""\n')
        for name, source in tool_modules.items():
            (tools_dir / f"{name}.py").write_text(source)

    native = config.target == "durable_agent_server" and (
        config.deploy is None or config.deploy.space is None
    )
    if native:
        # Validate scaling without generating a Bundle just to discard it.
        from ._agentbricks_deploy import compile_native_app

        compile_native_app(config)
    else:
        # Existing Bundle/App Space hosts retain their generated entrypoints.
        agent_server_dir = target_dir / "agent_server"
        agent_server_dir.mkdir(exist_ok=True)
        (agent_server_dir / "__init__.py").write_text("")
        (agent_server_dir / "start_server.py").write_text(_START_SERVER_CONTENT)
        (agent_server_dir / "start_host.py").write_text(_START_HOST_CONTENT)
        (agent_server_dir / "keepalive.py").write_text(_KEEPALIVE_CONTENT)
        (target_dir / "databricks.yml").write_text(_build_databricks_yml(config))
        (target_dir / "app.yml").write_text(_build_app_yml(config))

    # skills/ — copy each declared skill file into the project
    if config.skills:
        _base = source_dir if source_dir is not None else target_dir.parent
        skills_dir = target_dir / "skills"
        skills_dir.mkdir(exist_ok=True)
        for skill in config.skills:
            src = Path(skill.path)
            if not src.is_absolute():
                src = _base / skill.path
            (skills_dir / f"{skill.name}.md").write_text(src.read_text())
