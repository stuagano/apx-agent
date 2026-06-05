"""Coworker — a DataAgent with persona + objective.

A *coworker* is not a separate agent type; it is a ``DataAgent`` configured
with a ``persona`` (who it is) and an ``objective`` (what it is designed to
do).  ``CoworkerAgent`` is therefore just an alias for ``DataAgent``:

    agent = DataAgent(
        "main", "payroll",
        persona="a payroll specialist",
        objective="process payroll and answer compensation questions",
    )

``CoworkerTemplate`` is kept for template-as-config use cases and builds a
``DataAgent`` directly.
"""

from __future__ import annotations

from typing import Any

from .data_agent import DataAgent
from ._template import template
from ._models import normalize_memory_knob as normalize_memory_knob  # re-export for tests
from pydantic import BaseModel, ConfigDict, Field


# A coworker IS a DataAgent — no subclass needed.
CoworkerAgent = DataAgent


@template
class CoworkerTemplate:
    """A DataAgent with persona + objective; memory off by default."""

    name = "coworker"
    title = "Coworker"
    description = (
        "A DataAgent with a persona (who it is) and an objective "
        "(what it is designed to do). Memory is opt-in."
    )

    class Spec(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        schema_name: str = Field(alias="schema")
        warehouse_id: str | None = None
        persona: str | None = None
        objective: str | None = None
        memory: str = "off"
        genie_space: str | None = None
        vector_index: str | None = None
        include_functions: bool = True

    def build(self, spec: "CoworkerTemplate.Spec", *, ws: Any | None = None) -> DataAgent:
        return DataAgent(
            spec.catalog,
            spec.schema_name,
            persona=spec.persona,
            objective=spec.objective,
            memory=spec.memory,
            warehouse_id=spec.warehouse_id,
            ws=ws,
            include_functions=spec.include_functions,
            genie_space=spec.genie_space,
            vector_index=spec.vector_index,
        )
