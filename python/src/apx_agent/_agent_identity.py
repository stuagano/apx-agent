"""Agent principal — the identity an agent declares once (#816).

A Databricks App authenticates as exactly two principals: the app service
principal the platform injects (``DATABRICKS_CLIENT_ID`` /
``DATABRICKS_CLIENT_SECRET``) and the calling user, carried per request as an
OBO token. An agent declares which of those its tools default to, in one place:

    Agent(..., identity="named-sp:reader")   # or "sp" (app SP), or "obo" (the user)

Tools that declare no identity of their own inherit it. A tool's own
``identity`` still wins, which is how one agent mixes machine steps and
on-behalf-of steps.

``named-sp:<name>`` does not provision a second principal — the platform
injects one service principal per App, and apx has no credential source for
another. The name is the principal those calls run as and are audited as, and
it is only honest when it is the App's own service principal. Deploy checks
that against the App record rather than accepting the declaration on faith.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from ._tool import ExecutionIdentity
from ._tool_scope import ToolScope, attach_scope, get_scope

logger = logging.getLogger(__name__)

__all__ = [
    "apply_agent_identity",
    "principal_label",
]


def principal_label(identity: ExecutionIdentity, name: str | None) -> str:
    """The principal a call runs as, named the way audit attributes carry it.

    ``service`` with a name is that service principal; without one it is the
    App's own. ``user`` is the caller, whose identity rides OBO rather than
    this label.
    """
    if identity == "user":
        return "user"
    if name:
        return f"service-principal:{name}"
    return "app-service-principal"


def apply_agent_identity(agent: Any, identity: str) -> None:
    """Stamp the agent's default identity onto tools that declare none.

    A tool that already carries a scope ``identity`` keeps it — the per-tool
    declaration is the explicit choice. Scope ceilings are left untouched, so
    a tool that declared only catalogs still keeps exactly that ceiling.
    """
    tool_fns = getattr(agent, "_tool_fns", None)
    for fn in tool_fns if tool_fns else ():
        scope = get_scope(fn)
        if scope is not None and scope.identity is not None:
            continue
        inherited = replace(scope, identity=identity) if scope else ToolScope(identity=identity)
        attach_scope(fn, inherited)
        logger.debug(
            "tool %s inherited agent identity %r",
            getattr(fn, "__name__", "<tool>"),
            identity,
        )


