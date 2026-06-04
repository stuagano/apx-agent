"""Coworker — a pre-grounded DataAgent that remembers (facts + session).

``CoworkerAgent`` is a ``DataAgent`` subclass.  All it adds is:

- ``persona`` — woven into the grounded instructions.
- ``memory`` — defaulting to ``"persistent"`` instead of ``"off"``.

Memory wiring lives in ``LlmAgent`` (base class); ``normalize_memory_knob``
lives in ``_models``.  Construction needs no ``ws``.
"""

from __future__ import annotations

from typing import Any

from ._models import normalize_memory_knob as normalize_memory_knob  # re-export for tests
from .data_agent import DataAgent
from ._template import template
from pydantic import BaseModel, ConfigDict, Field


class CoworkerAgent(DataAgent):
    """A pre-grounded ``DataAgent`` that remembers — persona + memory.

    Adds an optional ``persona`` (woven into the grounded instructions) and
    defaults ``memory`` to ``"persistent"`` (UC Delta facts + session).
    Memory wiring is handled by the base ``LlmAgent``; all tier values and
    escalation paths are documented on ``LlmAgent.memory``.

    Args:
        memory: Memory tier — ``"off"``, ``"inmemory"``, ``"persistent"``
            (default, alias ``"delta"``). For ``lakebase``, use explicit
            ``[tool.apx.agent.memory]`` / ``.session`` TOML blocks.
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
        super().__init__(catalog, schema, persona=persona, memory=memory, **kwargs)


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
