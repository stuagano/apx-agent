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
    from ._models import AgentConfig, MemoryBackendConfig

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


def _declared_memory(agent: BaseAgent | None, config: AgentConfig | None) -> MemoryBackendConfig | None:
    """Memory declared on the Python agent (``memory=``), #954."""
    if config is not None and config.memory is not None:
        return config.memory
    return getattr(agent, "memory_config", None)


def needs_request_user(plan: AuthorizationPlan, config: AgentConfig | None, *,
                       agent: BaseAgent | None = None, project: AgentProject | None = None) -> bool:
    """Whether ``[auth.user] required`` must be true. build_native_manifest,
    migration and check_agent_toml share this one rule; scopes matter only then."""
    from ._platform_tools import platform_tool_leaves

    if any(op.requires_request_context for op in plan.operations) or plan.app_dependencies:
        return True
    if _declared_memory(agent, config) is not None:
        return True
    if agent is None or project is None:
        return False
    user_ids = {spec.id for spec in project.tools if spec.auth == "user"}
    return any(user_ids & (ref.ids or user_ids) for _, ref in platform_tool_leaves(agent))


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
    if needs_request_user(plan, config, agent=agent, project=project):
        if not project.user_auth.required:
            findings.append(Check(_NAME, Status.FAIL, "tools need a request user but [auth.user] required is false",
                                  "Set [auth.user] required = true in agent.toml."))
        missing = sorted(set(plan.user_api_scopes) - set(project.user_auth.additional_api_scopes))
        if missing:
            findings.append(Check(_NAME, Status.FAIL, f"user API scopes not declared: {', '.join(missing)}",
                                  f"Add {missing} to [auth.user] additional_api_scopes in agent.toml."))
    findings.extend(_store_findings(project, agent, config))
    return findings or [Check(_NAME, Status.OK, "agent.toml matches the Python agent")]


def require_agent_toml_ok(project_root: Path, agent: BaseAgent, plan: AuthorizationPlan,
                          config: AgentConfig | None) -> None:
    """Deploy preflight for every backend: any agent.toml FAIL stops the deploy."""
    import click

    fails = [c for c in check_agent_toml(project_root, agent, plan, config) if c.status is Status.FAIL]
    if fails:
        raise click.ClickException("agent.toml: " + "; ".join(f"{c.detail} — {c.fix}" for c in fails))


def _bound_session_store(agent: BaseAgent, config: AgentConfig) -> str | None:
    """The session store the agent binds: the one rule build_native_manifest and migration share."""
    from ._runtime_targets import inspect_target

    return inspect_target(agent, config=config, target=config.target).session_store


def _store_findings(project: AgentProject, agent: BaseAgent, config: AgentConfig | None) -> list[Check]:
    declared_memory = _declared_memory(agent, config)
    memory = declared_memory.store_name if declared_memory is not None and declared_memory.type == "managed" else None
    # A Python-only agent derives no session store (inspect_target binds none without config).
    session = _bound_session_store(agent, config) if config is not None else None
    pairs = (("memory", memory, project.memory_store, "agentbricks memory bind"),
             ("session", session, project.session_store, "agentbricks sessions bind"))
    out: list[Check] = []
    for label, declared, bound, command in pairs:
        if declared is None:
            continue
        if bound is None:
            out.append(Check(_NAME, Status.FAIL, f"managed {label} declared but no {label}_store bound",
                             f"Run `{command} <name>`."))
        elif declared is not None and declared != bound:
            out.append(Check(_NAME, Status.FAIL,
                             f"{label} store mismatch: apx {declared!r} vs agent.toml {bound!r}",
                             "Make the apx declaration (the agent's memory=) and "
                             "agent.toml name the same store."))
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
    if target.exists() and not target.read_text().startswith("# Generated by APX"):
        raise ValueError("Refusing to overwrite an authored agent.toml in the staged source directory.")
    target.write_text(generate())
    log("  no agent.toml: generated one (deprecated) — run `apx-agent agents migrate-agent-toml`")
    return target


def write_agent_toml(project_root: Path, config: AgentConfig, plan: AuthorizationPlan,
                     agent: BaseAgent) -> Path:
    """One-shot migration: the SDK writes the file; apx adds [auth.user]."""
    from databricks_agentbricks.projects.agent_project import AgentProject

    from ._project_gen import _write_toml_section

    path = project_root / _NAME
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"{path} already exists; agent.toml is not overwritten")

    memory = _declared_memory(agent, config)
    memory_name = memory.store_name if memory is not None and memory.type == "managed" else None
    AgentProject.create(project_root, framework="langgraph", server="agentbricks",
                        memory_store=memory_name,
                        session_store=_bound_session_store(agent, config)).write()
    # AgentProject has no [auth.user] setter. No [[tools]] exist yet, so no project.
    required = needs_request_user(plan, config, agent=agent)
    lines = [path.read_text().rstrip("\n"), ""]
    _write_toml_section(lines, "auth.user", {
        "required": required,
        "additional_api_scopes": list(plan.user_api_scopes) if required else [],
    })
    path.write_text("\n".join(lines) + "\n")
    load_agent_toml(project_root)  # read-after-write: the SDK must accept what we wrote
    return path
