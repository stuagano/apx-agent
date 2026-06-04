"""Coworker — a pre-grounded DataAgent that remembers (facts + session).

``CoworkerAgent`` is a ``DataAgent`` subclass that adds an optional persona and a
single ``memory`` knob; ``CoworkerTemplate`` wraps it for template-as-config.
Memory is carried as *declared config* (``memory_config`` / ``session_config``)
and wired by the framework's finalize/serve path with the app workspace client —
so construction needs no ``ws`` (same property as DataAgent grounding).
"""

from __future__ import annotations

from ._models import MemoryBackendConfig, SessionBackendConfig

# Bare-knob rungs → backend StoreType. ``lakebase`` is intentionally absent: it
# needs connection details the one-word knob can't express (see normalize).
_KNOB_TO_TYPE: dict[str, str] = {
    "off": "",            # sentinel: disabled
    "inmemory": "inmemory",
    "local": "inmemory",
    "persistent": "delta",
    "delta": "delta",
}


def normalize_memory_knob(
    value: str,
) -> "tuple[MemoryBackendConfig | None, SessionBackendConfig | None]":
    """Map the coworker ``memory`` knob to ``(MemoryBackendConfig,
    SessionBackendConfig)`` for the facts + session subsystems (same tier).

    Returns ``(None, None)`` for ``"off"``. Raises ``ValueError`` for
    ``"lakebase"`` (needs an explicit ``[tool.apx.agent.memory]`` block) and for
    any unknown value.
    """
    v = (value or "").strip().lower()
    if v == "lakebase":
        raise ValueError(
            "memory='lakebase' needs connection details the one-word knob can't "
            "carry — add explicit [tool.apx.agent.memory] and "
            "[tool.apx.agent.session] blocks with type='lakebase' "
            "(host, database, embedding_model, embedding_dim)."
        )
    if v not in _KNOB_TO_TYPE:
        raise ValueError(
            f"memory={value!r} is not a valid tier; use one of: off, inmemory "
            "(alias local), persistent (alias delta), or an explicit "
            "[tool.apx.agent.memory] block for lakebase."
        )
    tier = _KNOB_TO_TYPE[v]
    if not tier:  # "off"
        return (None, None)
    return (MemoryBackendConfig(type=tier), SessionBackendConfig(type=tier))


from typing import Any

from .data_agent import DataAgent
from ._template import template
from pydantic import BaseModel, ConfigDict, Field


class CoworkerAgent(DataAgent):
    """A pre-grounded ``DataAgent`` that remembers — persona + memory.

    Adds an optional ``persona`` (woven into the grounded instructions) and a
    single ``memory`` knob covering facts + session. Memory is declared as
    ``memory_config`` / ``session_config`` and wired by the framework's
    finalize/serve path with the app workspace client (so no ``ws`` is needed at
    construction). Composes like any agent: directly, as a ``sub_agent``, or as a
    leaf in a ``SequentialAgent`` / ``RouterAgent``.

    Args:
        memory: Memory tier knob — ``"off"``, ``"inmemory"`` (alias ``"local"``),
            ``"persistent"`` (alias ``"delta"``, the default). For ``lakebase``,
            use explicit ``[tool.apx.agent.memory]`` / ``.session`` blocks.
        persona: Optional role phrase (see ``DataAgent``).
        (All other args are ``DataAgent``'s.)
    """

    def __init__(
        self,
        catalog: str,
        schema: str,
        *,
        persona: str | None = None,
        memory: str = "persistent",
        **kwargs: Any,
    ) -> None:
        super().__init__(catalog, schema, persona=persona, **kwargs)
        self.memory_config, self.session_config = normalize_memory_knob(memory)


@template
class CoworkerTemplate:
    """A pre-grounded data agent that remembers (facts + session); memory
    upgradeable off → inmemory → persistent → lakebase. Wraps ``CoworkerAgent``."""

    name = "coworker"
    title = "Coworker"
    description = (
        "A pre-grounded data agent that remembers (facts + session); "
        "memory upgradeable off → inmemory → persistent → lakebase."
    )

    class Spec(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        schema_name: str = Field(alias="schema")  # 'schema' in config dicts
        warehouse_id: str | None = None
        persona: str | None = None
        memory: str = "persistent"
        genie_space: str | None = None
        vector_index: str | None = None
        include_functions: bool = True

    def build(self, spec: "CoworkerTemplate.Spec", *, ws: Any | None = None) -> CoworkerAgent:
        return CoworkerAgent(
            spec.catalog,
            spec.schema_name,
            persona=spec.persona,
            memory=spec.memory,
            warehouse_id=spec.warehouse_id,
            ws=ws,
            include_functions=spec.include_functions,
            genie_space=spec.genie_space,
            vector_index=spec.vector_index,
        )
