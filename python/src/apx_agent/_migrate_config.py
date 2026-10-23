"""apx-agent agents migrate-config: move a project off [tool.apx.agent] (sub-project 3a).

Deploy fields move to [tool.apx.deploy]; runtime fields must already be declared
on the Python agent before the table is removed. See
docs/design/agent-toml-source-of-truth.md, "Sub-project 3a"."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._models import AgentConfig

if TYPE_CHECKING:
    from ._agents import BaseAgent

_YAML_ONLY = ("data", "executor", "workflows", "example")
# Agent constructor arguments that aren't AgentSettings: compared to the agent's attribute.
_CONSTRUCTOR = {
    "instructions": "_instructions", "description": "_description", "temperature": "_temperature",
    "max_tokens": "_max_tokens", "max_iterations": "_max_iterations", "sub_agents": "_sub_agent_urls",
}
_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


@dataclass(frozen=True)
class Migration:
    text: str
    missing: dict[str, Any]
    removed_agent_table: bool
    manual: list[str] = field(default_factory=list)
    """[tool.apx.agent] keys that aren't settings; the user removes them by hand."""


def cut_sections(text: str, prefix: str, *, children: bool = True) -> str:
    """Remove ``[prefix]`` (and, with *children*, every ``[prefix.*]``) section."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        match = _HEADER.match(line)
        if match:
            name = match.group(1)
            skipping = name == prefix or (children and name.startswith(prefix + "."))
        if not skipping:
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def _deploy_block(table: dict[str, Any]) -> str:
    from ._project_gen import _write_toml_section

    lines: list[str] = []
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    _write_toml_section(lines, "tool.apx.deploy", scalars)
    for key, value in table.items():
        if isinstance(value, dict):
            _write_toml_section(lines, f"tool.apx.deploy.{key}", value)
    return "\n".join(lines)


def _covered(field: str, value: Any, agent: BaseAgent, after: AgentConfig) -> bool:
    from ._settings import resolve_settings

    declared = AgentConfig.model_validate({"name": "x", field: value})
    if field in _CONSTRUCTOR:
        return getattr(agent, _CONSTRUCTOR[field], None) == getattr(declared, field)
    return getattr(resolve_settings(agent, after), field) == getattr(declared, field)


def plan_migration(pyproject: Path, agent: BaseAgent) -> Migration:
    """The new pyproject text and what Python still has to declare."""
    text = pyproject.read_text()
    data = tomllib.loads(text)
    apx = data.get("tool", {}).get("apx", {})
    raw = apx.get("agent")
    if raw is None:
        return Migration(text, {}, False)
    module = raw.get("module")
    if module is not None and module != "agent:agent":
        raise ValueError(f"module = {module!r} is not supported; rename it so the agent is agent:agent.")
    if "template" in raw:
        raise ValueError("template = {...} is not supported; generate an agent.py from the template first.")
    for field in _YAML_ONLY:
        if field in raw:
            raise ValueError(f"{field} belongs in the YAML spec, not [tool.apx.agent].")

    deploy = dict(apx.get("deploy", {}))
    for key in ("name", "host"):
        if key in raw:
            deploy[key] = raw[key]
    deploy.update(raw.get("deploy", {}))
    from ._inspection import DEV_KEYS, REGISTRATION_KEYS
    from ._project_gen import _write_toml_section

    deploy.update({k: raw[k] for k in REGISTRATION_KEYS if k in raw})
    top = {k: v for k, v in apx.items() if not isinstance(v, dict)}
    top.update({k: raw[k] for k in DEV_KEYS if k in raw})
    deploy_only = {"tool": {"apx": {"deploy": deploy}}, "project": data.get("project", {})}
    from ._inspection import _fold_deploy_table

    after = AgentConfig.model_validate(_fold_deploy_table(deploy_only, {}))
    runtime = {k: v for k, v in raw.items()
               if k in AgentConfig.model_fields and k not in ("name", "host", "deploy")}
    missing = {k: v for k, v in runtime.items() if not _covered(k, v, agent, after)}

    moved = ("name", "host", "deploy", "module", *DEV_KEYS, *REGISTRATION_KEYS)
    remaining = {k: v for k, v in raw.items() if k not in moved}
    new = cut_sections(cut_sections(text, "tool.apx.deploy"), "tool.apx.agent")
    if missing or set(remaining) - set(runtime):
        lines: list[str] = []
        _write_toml_section(lines, "tool.apx.agent", {k: v for k, v in remaining.items() if not isinstance(v, dict)})
        for key, value in remaining.items():
            if isinstance(value, dict):
                _write_toml_section(lines, f"tool.apx.agent.{key}", value)
        new = new.rstrip("\n") + "\n\n" + "\n".join(lines)
        removed = False
    else:
        removed = True
    new = cut_sections(new, "tool.apx", children=False)
    if top:
        top_lines: list[str] = []
        _write_toml_section(top_lines, "tool.apx", top)
        new = new.rstrip("\n") + "\n\n" + "\n".join(top_lines)
    new = new.rstrip("\n") + "\n\n" + _deploy_block(deploy)
    manual = sorted(set(remaining) - set(runtime))
    return Migration(new.rstrip("\n") + "\n", missing, removed, manual)


def migrate_local(text: str) -> str | None:
    """``.apx.local``: move dev keys from its old ``[tool.apx.agent]`` to ``[tool.apx]``; None if nothing to do."""
    from ._inspection import DEV_KEYS
    from ._project_gen import _write_toml_section

    data = tomllib.loads(text)
    apx = data.get("tool", {}).get("apx", {})
    legacy = apx.get("agent", {})
    move = {k: legacy[k] for k in DEV_KEYS if k in legacy}
    if not move:
        return None
    rest = {k: v for k, v in legacy.items() if k not in move}
    top = {**{k: v for k, v in apx.items() if not isinstance(v, dict)}, **move}
    out = cut_sections(cut_sections(text, "tool.apx.agent"), "tool.apx", children=False).rstrip("\n")
    lines: list[str] = []
    _write_toml_section(lines, "tool.apx", top)
    if rest:
        _write_toml_section(lines, "tool.apx.agent", rest)
    return ((out + "\n\n") if out else "") + "\n".join(lines).rstrip("\n") + "\n"


def snippet(missing: dict[str, Any]) -> str:
    """Keyword arguments to paste into the agent's constructor."""
    return "Agent(..., " + ", ".join(f"{k}={v!r}" for k, v in missing.items()) + ")"
