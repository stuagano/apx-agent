"""Static checks for apx-agent agents.

Catches common footguns at definition time rather than in production:
agents with no system prompt, tools missing docstrings (the LLM has no
description to choose from), sub-agent URLs that reference unset env
vars, model endpoint names that don't look like Databricks Model
Serving endpoints.

Linting is best-effort and static — it can only see what's wired
into the agent tree at import time. Behavior bugs (does the agent
actually do the right thing?) belong in ``apx eval``. Smoke tests
(does the agent crash on a basic prompt?) belong in ``apx test``.

The lint catalog (rule codes are stable; severities can tighten over time):

| Code | Severity | What |
|------|----------|------|
| L001 | error    | LlmAgent has no ``instructions`` set. |
| L002 | warning  | Tool function has no docstring — LLM can't pick it reliably. |
| L003 | error    | Sub-agent URL references ``$ENV_VAR`` that isn't set. |
| L004 | warning  | Model endpoint name doesn't look like a Databricks serving endpoint. |
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from ._agents import BaseAgent


class Severity(str, Enum):
    """Lint finding severity. Strings so JSON output stays trivial."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class LintFinding:
    """A single lint finding."""

    code: str
    severity: Severity
    location: str
    message: str

    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


# ---------------------------------------------------------------------------
# Tree walking — yield every LlmAgent in the tree (recurses composites)
# ---------------------------------------------------------------------------


def _iter_llm_agents(agent: "BaseAgent") -> Iterable[Any]:
    """Yield every leaf ``LlmAgent`` reachable from ``agent``.

    Recurses through composite agents (``SequentialAgent``,
    ``ParallelAgent``, ``LoopAgent``, ``RouterAgent``, ``HandoffAgent``).
    Remote agents are skipped — their instructions live on the other side.
    """
    from ._agents import LlmAgent
    from ._remote import RemoteDatabricksAgent

    if isinstance(agent, RemoteDatabricksAgent):
        return
    if isinstance(agent, LlmAgent):
        yield agent
        return
    # Composite agents: descend into ``_agents``
    children = getattr(agent, "_agents", None)
    if not children:
        return
    if isinstance(children, dict):
        children = children.values()
    for child in children:
        yield from _iter_llm_agents(child)


# ---------------------------------------------------------------------------
# L001 — missing instructions
# ---------------------------------------------------------------------------


def _check_instructions(agent: "BaseAgent") -> Iterable[LintFinding]:
    for sub in _iter_llm_agents(agent):
        instructions = getattr(sub, "_instructions", None) or ""
        if not instructions.strip():
            name = getattr(sub, "_name", None) or type(sub).__name__
            yield LintFinding(
                code="L001",
                severity=Severity.ERROR,
                location=f"agent:{name}",
                message=(
                    "LlmAgent has no instructions set. The LLM will receive "
                    "no system prompt and its behavior is undefined. Pass "
                    "instructions= to the constructor or set "
                    "[tool.apx.agent].instructions in pyproject.toml."
                ),
            )


# ---------------------------------------------------------------------------
# L002 — tools missing docstrings
# ---------------------------------------------------------------------------


def _check_tool_docstrings(agent: "BaseAgent") -> Iterable[LintFinding]:
    from ._resources import _iter_tool_fns

    for fn in _iter_tool_fns(agent):
        doc = (getattr(fn, "__doc__", None) or "").strip()
        if not doc:
            yield LintFinding(
                code="L002",
                severity=Severity.WARNING,
                location=f"tool:{fn.__name__}",
                message=(
                    f"Tool {fn.__name__!r} has no docstring. The LLM uses "
                    "the docstring as the tool description; without one the "
                    "LLM cannot reliably decide when to call this tool."
                ),
            )


# ---------------------------------------------------------------------------
# L003 — sub-agent URLs with unresolved $ENV_VAR refs
# ---------------------------------------------------------------------------


_ENV_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _check_subagent_urls(agent: "BaseAgent") -> Iterable[LintFinding]:
    from ._resources import _iter_sub_agents

    for url in _iter_sub_agents(agent):
        for match in _ENV_VAR_RE.finditer(url):
            var_name = match.group(1)
            if var_name not in os.environ:
                yield LintFinding(
                    code="L003",
                    severity=Severity.ERROR,
                    location=f"sub_agent:{url}",
                    message=(
                        f"Sub-agent URL references ${var_name} but the env "
                        "var is not set. The agent will fail at compile time "
                        "with an unresolved variable error."
                    ),
                )


# ---------------------------------------------------------------------------
# L004 — model name shape check
# ---------------------------------------------------------------------------


_DATABRICKS_MODEL_PREFIXES = (
    "databricks-",       # Foundation Model API endpoints
    "endpoints/",        # Apps mode reference
)


def _check_model_name(agent: "BaseAgent", model: str | None) -> Iterable[LintFinding]:
    """Both the explicit ``model`` kwarg and the agent's pyproject config."""
    candidates: list[tuple[str, str]] = []

    if model:
        candidates.append(("explicit-model-kwarg", model))

    # If the agent has been compiled with a model attribute, check that too.
    for sub in _iter_llm_agents(agent):
        m = getattr(sub, "_model", None)
        if m and isinstance(m, str):
            name = getattr(sub, "_name", None) or type(sub).__name__
            candidates.append((f"agent:{name}", m))

    seen: set[str] = set()
    for location, value in candidates:
        if value in seen:
            continue
        seen.add(value)
        if not any(value.startswith(p) for p in _DATABRICKS_MODEL_PREFIXES):
            yield LintFinding(
                code="L004",
                severity=Severity.WARNING,
                location=location,
                message=(
                    f"Model name {value!r} doesn't start with a known "
                    "Databricks endpoint prefix "
                    f"({', '.join(_DATABRICKS_MODEL_PREFIXES)}). This may be a "
                    "typo, or a custom endpoint name — verify before deploy."
                ),
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def lint_agent(agent: "BaseAgent", *, model: str | None = None) -> list[LintFinding]:
    """Run all static checks against an agent. Returns sorted findings.

    Findings are sorted by ``(code, location)`` so the output is stable
    across runs — useful for diffing or asserting in CI.

    Args:
        agent: Any ``BaseAgent``.
        model: The model endpoint that will be used at deploy time. When
            provided, lints the name; otherwise only checks ``_model``
            attributes already present on tree nodes.

    Returns:
        Sorted list of ``LintFinding``. Empty list means clean.
    """
    findings: list[LintFinding] = []
    findings.extend(_check_instructions(agent))
    findings.extend(_check_tool_docstrings(agent))
    findings.extend(_check_subagent_urls(agent))
    findings.extend(_check_model_name(agent, model))
    findings.sort(key=lambda f: (f.code, f.location))
    return findings
