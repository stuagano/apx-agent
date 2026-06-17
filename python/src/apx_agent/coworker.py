"""Coworker — a DataAgent that joins two disparate source systems.

A coworker is a ``DataAgent`` configured with three coworker-specific knobs:

- **persona** — who it is ("a payroll operations analyst")
- **join_key** — the business entity that links the two source systems
  ("employee ID", "opportunity ID")
- **objective** — what it is designed to do ("surface mismatches between
  hours worked and paychecks issued")

The ``join_key`` is folded into the objective before the ``DataAgent`` builds
its schema-grounded instructions, so the agent always knows which field
connects the two landed systems.

``CoworkerTemplate`` is kept for template-as-config use cases.
"""

from __future__ import annotations

from typing import Any

from .data_agent import DataAgent
from ._template import template
from ._models import normalize_memory_knob as normalize_memory_knob  # re-export for tests
from pydantic import BaseModel, ConfigDict, Field


class CoworkerAgent(DataAgent):
    """A DataAgent that joins two disparate source systems.

    Adds three coworker-specific knobs on top of ``DataAgent``:

    Args:
        persona: Who this agent is ("a payroll operations analyst").
        join_key: The business entity linking the two source systems
            ("employee ID", "opportunity ID", "asset serial number").
            Appended to the objective so the agent knows how to JOIN the
            landed tables.
        objective: What this agent is designed to do ("surface mismatches
            between hours worked and paychecks issued"). Combined with
            ``join_key`` when both are given.
        **kwargs: All other ``DataAgent`` / ``LlmAgent`` kwargs pass through
            (``memory``, ``warehouse_id``, ``ws``, ``genie_space``, etc.).
    """

    def __init__(
        self,
        catalog: str,
        schema: str,
        *,
        persona: str | None = None,
        join_key: str | None = None,
        objective: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.join_key = join_key
        resolved_objective = objective
        if join_key:
            join_context = f"The two source systems are linked on {join_key}"
            resolved_objective = (
                f"{objective}. {join_context}" if objective else join_context
            )
        super().__init__(
            catalog, schema,
            persona=persona,
            objective=resolved_objective,
            **kwargs,
        )


@template
class CoworkerTemplate:
    """Joins two disparate UC source systems; memory off by default."""

    name = "coworker"
    title = "Coworker"
    description = (
        "Joins two source systems landed in a UC schema. "
        "Configured with persona, join key, and objective. Memory is opt-in."
    )

    class Spec(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        catalog: str
        schema_name: str = Field(alias="schema")
        warehouse_id: str | None = None
        persona: str | None = None
        join_key: str | None = None
        objective: str | None = None
        memory: str = "off"
        genie_space: str | None = None
        vector_index: str | None = None
        include_functions: bool = True
        knowledge: str | None = None

    def build(self, spec: "CoworkerTemplate.Spec", *, ws: Any | None = None) -> CoworkerAgent:
        return CoworkerAgent(
            spec.catalog,
            spec.schema_name,
            persona=spec.persona,
            join_key=spec.join_key,
            objective=spec.objective,
            memory=spec.memory,
            warehouse_id=spec.warehouse_id,
            ws=ws,
            include_functions=spec.include_functions,
            genie_space=spec.genie_space,
            vector_index=spec.vector_index,
            knowledge=spec.knowledge,
        )
