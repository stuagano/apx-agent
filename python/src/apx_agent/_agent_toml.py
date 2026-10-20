"""agent.toml is the platform's source of truth (docs/design/agent-toml-source-of-truth.md).

apx reads it with the SDK's own AgentProject.load and checks it against the
Python agent. It never writes it (see `agents migrate-agent-toml` for the one
exception)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ._doctor import Check, Status

if TYPE_CHECKING:
    from databricks_agentbricks.projects.agent_project import AgentProject

    from ._agents import BaseAgent
    from ._apps_authorization import AuthorizationPlan
    from ._models import AgentConfig, MemoryBackendConfig, SessionBackendConfig

_NAME = "agent.toml"


def load_agent_toml(project_root: Path) -> AgentProject | None:
    """The AgentProject at exactly ``project_root``, or None. Never walks up."""
    if not (project_root / _NAME).is_file():
        return None
    from databricks_agentbricks.projects.agent_project import AgentProject

    return AgentProject.load(project_root)


def platform_tool_names(project: AgentProject) -> dict[str, list[str]]:
    """agent.toml id → the tool names the LLM sees for it."""
    names: dict[str, list[str]] = {}
    for spec in project.tools:
        if spec.source.kind == "genie_agent":
            names[spec.id] = [f"{spec.id}_{m}" for m in ("ask", "poll", "query_result")]
        else:
            names[spec.id] = [spec.id]
    return names


def check_agent_toml(
    project_root: Path,
    agent: BaseAgent,
    plan: AuthorizationPlan,
    config: AgentConfig | None,
    *,
    runtime_only: bool = False,
) -> list[Check]:
    """Findings for agent.toml vs the Python agent. One FAIL per mismatch."""
    from databricks_agentbricks.errors import AgentCliError

    from ._platform_tools import platform_tool_leaves

    try:
        project = load_agent_toml(project_root)
    except AgentCliError as exc:
        return [Check(_NAME, Status.FAIL, str(exc), "Fix agent.toml so `agentbricks` can load it.")]
    if project is None:
        return [Check(_NAME, Status.WARN, "no agent.toml; deploy generates one (deprecated)",
                      "Run `apx-agent agents migrate-agent-toml` to write an authored agent.toml.")]

    findings: list[Check] = []
    declared = platform_tool_names(project)
    leaves = platform_tool_leaves(agent)
    referenced: set[str] = set()
    for leaf, ref in leaves:
        unknown = sorted(ref.ids - declared.keys())
        if unknown:
            findings.append(Check(_NAME, Status.FAIL,
                                  f"{leaf._name}: platform_tools({', '.join(map(repr, unknown))}) not declared",
                                  "Declare the tool under [[tools]] in agent.toml, or remove the id."))
        ids = ref.ids or frozenset(declared)
        referenced |= ids
        python_names = {fn.__name__ for fn in leaf._tool_fns}
        clashes = sorted(python_names & {n for i in ids & declared.keys() for n in declared[i]})
        if clashes:
            findings.append(Check(_NAME, Status.FAIL,
                                  f"{leaf._name}: tool name collision {', '.join(clashes)}",
                                  "Rename the Python tool or the agent.toml tool id."))
    if runtime_only:
        return findings

    unreferenced = sorted(declared.keys() - referenced)
    if unreferenced:
        findings.append(Check(_NAME, Status.FAIL,
                              f"declared but unused: {', '.join(unreferenced)}",
                              "Reference them with platform_tools(...) on an agent, or remove them."))
    if str(project.framework) != "langgraph":
        findings.append(Check(_NAME, Status.FAIL, f"framework = {project.framework!s}",
                              'Set [agent] framework = "langgraph" (apx compiles to LangGraph).'))
    if str(project.server) != "agentbricks":
        findings.append(Check(_NAME, Status.FAIL, f"server = {project.server!s}",
                              'Set [agent] server = "agentbricks" for the durable target.'))
    missing = sorted(set(plan.user_api_scopes) - set(project.user_auth.additional_api_scopes))
    if missing:
        findings.append(Check(_NAME, Status.FAIL, f"user API scopes not declared: {', '.join(missing)}",
                              f"Add {missing} to [auth.user] additional_api_scopes in agent.toml."))
    needs_user = any(op.requires_request_context for op in plan.operations) or bool(plan.app_dependencies)
    if needs_user and not project.user_auth.required:
        findings.append(Check(_NAME, Status.FAIL, "tools need a request user but [auth.user] required is false",
                              "Set [auth.user] required = true in agent.toml."))
    findings.extend(_store_findings(project, config))
    return findings or [Check(_NAME, Status.OK, "agent.toml matches the Python agent")]


def _store_findings(project: AgentProject, config: AgentConfig | None) -> list[Check]:
    if config is None:
        return []
    out: list[Check] = []
    pairs = (("memory", config.memory, project.memory_store, "agentbricks memory bind"),
             ("session", config.session, project.session_store, "agentbricks sessions bind"))
    for label, declared, bound, command in pairs:
        if declared is None or declared.type != "managed":
            continue
        if bound is None:
            out.append(Check(_NAME, Status.FAIL, f"managed {label} declared but no {label}_store bound",
                             f"Run `{command} <name>`."))
        elif declared.store_name is not None and declared.store_name != bound:
            out.append(Check(_NAME, Status.FAIL,
                             f"{label} store mismatch: pyproject {declared.store_name!r} vs agent.toml {bound!r}",
                             "Make [tool.apx.agent] and agent.toml name the same store."))
    return out


def stage_agent_toml(project_root: Path, staged_source: Path,
                     generate: Callable[[], str], log: Callable[[str], None]) -> Path:
    """Stage the authored agent.toml unchanged; generate (deprecated) only when absent."""
    authored = project_root / _NAME
    target = staged_source / _NAME
    if authored.is_symlink() or target.is_symlink():
        raise ValueError("Refusing a symlinked agent.toml.")
    if authored.is_file():
        shutil.copyfile(authored, target)
        log(f"  staged authored agent.toml: {target}")
        return target
    target.write_text(generate())
    log("  no agent.toml: generated one (deprecated) — run `apx-agent agents migrate-agent-toml`")
    return target


def write_agent_toml(project_root: Path, config: AgentConfig, plan: AuthorizationPlan) -> Path:
    """One-shot migration: the SDK writes the file; apx adds [auth.user]."""
    from databricks_agentbricks.projects.agent_project import AgentProject

    from ._project_gen import _write_toml_section

    path = project_root / _NAME
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"{path} already exists; agent.toml is not overwritten")

    def managed(store: MemoryBackendConfig | SessionBackendConfig | None) -> str | None:
        return store.store_name if store is not None and store.type == "managed" else None

    AgentProject.create(project_root, framework="langgraph", server="agentbricks",
                        memory_store=managed(config.memory),
                        session_store=managed(config.session)).write()
    # AgentProject has no [auth.user] setter. Same rule as build_native_manifest.
    required = any(op.requires_request_context for op in plan.operations)
    required = required or bool(plan.app_dependencies) or config.memory is not None
    lines = [path.read_text().rstrip("\n"), ""]
    _write_toml_section(lines, "auth.user", {
        "required": required,
        "additional_api_scopes": list(plan.user_api_scopes) if required else [],
    })
    path.write_text("\n".join(lines) + "\n")
    load_agent_toml(project_root)  # read-after-write: the SDK must accept what we wrote
    return path
