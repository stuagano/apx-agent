"""Coworker — a DataAgent with a named persona.

``CoworkerAgent`` is a ``DataAgent`` subclass.  All it adds is:

- ``persona`` — woven into the grounded instructions.
- ``memory`` — defaults to ``"off"`` (stateless); opt-in to ``"persistent"``
  or ``"lakebase"`` when you need cross-session recall.

Memory wiring lives in ``LlmAgent`` (base class).  Construction needs no ``ws``.
"""

from __future__ import annotations

from typing import Any

from ._models import normalize_memory_knob as normalize_memory_knob  # re-export for tests
from .data_agent import DataAgent
from ._template import template
from pydantic import BaseModel, ConfigDict, Field


class CoworkerAgent(DataAgent):
    """A ``DataAgent`` with an optional persona (system instructions).

    Coworker = DataAgent + persona. Memory is opt-in via the ``memory`` knob;
    the default is ``"off"`` (stateless). Upgrade to ``"persistent"`` (UC Delta)
    or ``"lakebase"`` when cross-session recall is needed.

    Args:
        persona: Optional role phrase woven into grounded instructions.
        memory: Memory tier — ``"off"`` (default), ``"inmemory"``,
            ``"persistent"`` (alias ``"delta"``). For ``lakebase``, use
            explicit ``[tool.apx.agent.memory]`` / ``.session`` TOML blocks.
        (All other args are ``DataAgent``'s.)
    """

    def __init__(
        self,
        catalog: str,
        schema: str,
        *,
        persona: str | None = None,
        memory: str = "off",
        **kwargs: Any,
    ) -> None:
        super().__init__(catalog, schema, persona=persona, memory=memory, **kwargs)


@template
class CoworkerTemplate:
    """A DataAgent with a named persona; memory off by default, upgradeable."""

    name = "coworker"
    title = "Coworker"
    description = (
        "A DataAgent with a named persona (system instructions). "
        "Memory is opt-in: off → inmemory → persistent → lakebase."
    )

    class Spec(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        schema_name: str = Field(alias="schema")  # 'schema' in config dicts
        warehouse_id: str | None = None
        persona: str | None = None
        memory: str = "off"
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
