"""Per-family /_apx route modules composed by build_dev_ui_router."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext
from . import chat
from . import tools
from . import probe
from . import conversations
from . import approvals
from . import traces
from . import topology
from . import replay
from . import edit
from . import deploy
from . import setup
from . import discover
from . import hub
from . import grounding
from . import eval_ui

# First-seen order from the original build_dev_ui_router body.
_ORDER = (
    chat,
    tools,
    probe,
    conversations,
    approvals,
    traces,
    topology,
    replay,
    edit,
    deploy,
    setup,
    discover,
    hub,
    grounding,
    eval_ui,
)


def mount(router: APIRouter, ctx: DevRouteContext) -> None:
    """Register each family against ``apx_agent._dev`` globals.

    These routes used to be nested inside ``build_dev_ui_router``, so call-time
    name lookup and postponed annotations resolved on that module. Rebinding
    ``register`` keeps that, including monkeypatches applied after the router
    is built. Each family's source still owns its route closures.
    """
    import types

    import apx_agent._dev as dev

    for family in _ORDER:
        register = family.register
        bound = types.FunctionType(
            register.__code__,
            dev.__dict__,
            register.__name__,
            register.__defaults__,
            register.__closure__,
        )
        bound(router, ctx)
