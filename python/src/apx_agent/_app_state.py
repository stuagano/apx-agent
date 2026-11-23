"""Deploy-time ``app:`` constants (#1233).

Declared on the root agent as ``app_state`` (or ``app_state:`` in a coworker
YAML spec, which ``generate_project`` writes onto that agent). Seeded into the
graph ``state`` channel on every invoke as ``app:<key>``. Agents do not write
these keys at runtime. There is no store.

``agent.toml`` is the Agent Bricks manifest and ``[tool.apx]`` is a fixed CLI
key list, so neither holds this table. See
docs/design/keyed-state-scopes.md.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from ._session_state import note_unpersisted_scope

_APP_PREFIX = "app:"
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")


def normalize_app_state(value: Any) -> dict[str, Any]:
    """Copy *value* into ``{unprefixed key: JSON value}``.

    A key may be ``flag`` or ``app:flag``. Both become ``flag``. The same name
    given both ways is an error. Values are JSON scalars or nested arrays and
    objects.
    """
    if not isinstance(value, Mapping):
        raise ValueError("app_state must be a mapping of names to JSON values")
    normalized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or raw_key == "":
            raise ValueError(f"app_state key must be a non-empty string, got {raw_key!r}")
        key = raw_key[len(_APP_PREFIX):] if raw_key.startswith(_APP_PREFIX) else raw_key
        if _KEY.fullmatch(key) is None:
            raise ValueError(
                f"app_state key {raw_key!r} must be an identifier, with or without an app: prefix"
            )
        if key in normalized:
            raise ValueError(f"app_state key {key!r} is declared more than once")
        normalized[key] = _json_value(raw_value, key, set())
    return normalized


def _json_value(value: Any, where: str, seen: set[int]) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"app_state[{where}] must be a finite number")
        return value
    if isinstance(value, list):
        if id(value) in seen:
            raise ValueError(f"app_state[{where}] contains a cycle")
        seen.add(id(value))
        try:
            return [_json_value(item, where, seen) for item in value]
        finally:
            seen.discard(id(value))
    if isinstance(value, dict):
        if id(value) in seen:
            raise ValueError(f"app_state[{where}] contains a cycle")
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"app_state[{where}] object keys must be strings")
        seen.add(id(value))
        try:
            return {key: _json_value(item, f"{where}.{key}", seen) for key, item in value.items()}
        finally:
            seen.discard(id(value))
    raise ValueError(
        f"app_state[{where}] must be JSON (null, bool, number, string, array, or object), "
        f"got {type(value).__name__}"
    )


def reject_app_write(key: str) -> None:
    """Log and raise when a turn tries to write an ``app:`` key.

    The declared value stays in state. ``user:`` writes are unchanged.
    """
    if not key.startswith(_APP_PREFIX):
        return
    note_unpersisted_scope(key)
    raise ValueError(
        f"{key} is deploy-time app config and cannot be written at runtime. "
        "Set it in app_state and redeploy."
    )


def app_state_channel(agent: Any) -> dict[str, Any]:
    """``{"app:<key>": value}`` for the agent's declaration. Empty when unset.

    Values are copied so a later in-graph mutation cannot change the declaration.
    The next invoke seeds the declaration again.
    """
    from ._agents import BaseAgent

    if not isinstance(agent, BaseAgent):
        return {}
    raw = agent.app_state
    if not isinstance(raw, dict) or not raw:
        return {}
    return {f"{_APP_PREFIX}{key}": deepcopy(value) for key, value in raw.items()}


def declared_app_keys(source: Any) -> list[str]:
    """Sorted unprefixed key names. Values are not included.

    A loaded module attribute is not always a ``BaseAgent`` (``agents describe``
    still prints a plain object). Missing ``app_state`` means no keys.
    """
    if isinstance(source, dict):
        raw = source
    elif hasattr(source, "app_state"):
        raw = source.app_state
    else:
        raw = None
    if not isinstance(raw, dict) or not raw:
        return []
    return sorted(raw)


def _overlay_seed(state: dict[str, Any], seed: dict[str, Any]) -> dict[str, Any]:
    """Return *state* with declared ``app:`` keys winning over anything already there."""
    merged = dict(state)
    for key, value in seed.items():
        if key in merged and merged[key] != value:
            note_unpersisted_scope(key)
        merged[key] = value
    return merged


def graph_input_with_app_state(agent: Any, graph_input: Any) -> Any:
    """Merge declared ``app:`` constants into one graph invoke.

    A dict input gains a ``state`` channel (or the declaration replaces those
    keys). A ``Command`` resume carries the same keys in ``update`` so a paused
    turn still sees deploy config. ``None`` (a recovery continuation) is left
    alone: that checkpoint already applied this turn's input.
    """
    seed = app_state_channel(agent)
    if not seed or graph_input is None:
        return graph_input
    from langgraph.types import Command

    if isinstance(graph_input, Command):
        update = dict(graph_input.update) if isinstance(graph_input.update, dict) else {}
        prior = update.get("state")
        update["state"] = _overlay_seed(prior if isinstance(prior, dict) else {}, seed)
        return Command(
            graph=graph_input.graph,
            update=update,
            resume=graph_input.resume,
            goto=graph_input.goto,
        )
    if not isinstance(graph_input, dict):
        return graph_input
    prior = graph_input.get("state")
    return {**graph_input, "state": _overlay_seed(prior if isinstance(prior, dict) else {}, seed)}


def check_app_constants(agent: Any, config: Any, agent_py: Path | None = None) -> Any | None:
    """Doctor row naming declared app keys. Values are omitted.

    Prefers a loaded agent, then an ``AgentConfig`` that actually set the
    field, then a literal ``app_state=`` in ``agent.py`` (the static read used
    when doctor does not import the agent).
    """
    raw: dict[str, Any] | None = None
    if agent is not None:
        # A loaded agent is the runtime declaration. Do not also read agent.py.
        raw = agent.app_state if isinstance(agent.app_state, dict) and agent.app_state else None
    elif (
        config is not None
        and isinstance(config.app_state, dict)
        and config.app_state
        and "app_state" in config.model_fields_set
    ):
        raw = config.app_state
    elif agent_py is not None and agent_py.is_file():
        from ._inspection import static_agent_setting

        literal = static_agent_setting(agent_py, "app_state")
        if isinstance(literal, dict) and literal:
            raw = normalize_app_state(literal)
    if not raw:
        return None
    from ._doctor import Check, Status

    return Check(
        "App constants",
        Status.OK,
        "keys: " + ", ".join(sorted(raw)) + " (values are not shown)",
        None,
    )
