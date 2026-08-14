"""
Structured per-step outputs for the discovery workflow.

Every step returns a typed dataclass, never free prose — so a downstream
consumer (render, handoff step, audit) reads a shape, not a paragraph. Each
dataclass parses itself from the raw completion dict via ``from_dict`` and
raises ``MalformedStepOutput`` when the model returned something unusable, so a
bad completion fails loudly instead of flowing forward as garbage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class MalformedStepOutput(Exception):
    """A step's completion output did not match the step's schema."""

    def __init__(self, step_key: str, detail: str):
        super().__init__(f"malformed output for step '{step_key}': {detail}")
        self.step_key = step_key


def _obj(step_key: str, data: Any, keys: list[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise MalformedStepOutput(step_key, f"expected object, got {type(data).__name__}")
    missing = [k for k in keys if k not in data]
    if missing:
        raise MalformedStepOutput(step_key, f"missing keys: {missing}")
    return data


def _str_list(step_key: str, value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise MalformedStepOutput(step_key, f"'{label}' must be a list")
    return [str(v) for v in value]


@dataclass
class ResearchBundle:
    """Customer research feeding step 1. Domain-neutral by construction."""

    customer: str
    persona: str | None
    findings: list[str] = field(default_factory=list)


@dataclass
class Priorities:
    industry: str
    priorities: list[str]

    @classmethod
    def from_dict(cls, data: Any) -> Priorities:
        d = _obj("priorities", data, ["industry", "priorities"])
        return cls(industry=str(d["industry"]), priorities=_str_list("priorities", d["priorities"], "priorities"))


@dataclass
class MatrixRow:
    outcome: str
    capability: str
    enabler: str


@dataclass
class ValueMatrices:
    rows: list[MatrixRow]

    @classmethod
    def from_dict(cls, data: Any) -> ValueMatrices:
        d = _obj("value_matrices", data, ["rows"])
        raw = d["rows"]
        if not isinstance(raw, list):
            raise MalformedStepOutput("value_matrices", "'rows' must be a list")
        rows = []
        for r in raw:
            rd = _obj("value_matrices", r, ["outcome", "capability", "enabler"])
            rows.append(MatrixRow(str(rd["outcome"]), str(rd["capability"]), str(rd["enabler"])))
        return cls(rows=rows)


@dataclass
class HeatCell:
    outcome: str
    value_score: float
    effort_score: float


@dataclass
class HeatMap:
    cells: list[HeatCell]

    @classmethod
    def from_dict(cls, data: Any) -> HeatMap:
        d = _obj("heat_map", data, ["cells"])
        raw = d["cells"]
        if not isinstance(raw, list):
            raise MalformedStepOutput("heat_map", "'cells' must be a list")
        cells = []
        for c in raw:
            cd = _obj("heat_map", c, ["outcome", "value_score", "effort_score"])
            try:
                cells.append(HeatCell(str(cd["outcome"]), float(cd["value_score"]), float(cd["effort_score"])))
            except (TypeError, ValueError) as err:
                raise MalformedStepOutput("heat_map", f"scores must be numeric: {err}") from err
        return cls(cells=cells)


@dataclass
class WowSelection:
    chosen: list[str]
    rationale: str

    @classmethod
    def from_dict(cls, data: Any) -> WowSelection:
        d = _obj("wow_selection", data, ["chosen", "rationale"])
        return cls(chosen=_str_list("wow_selection", d["chosen"], "chosen"), rationale=str(d["rationale"]))


@dataclass
class DiscoveryGuide:
    questions: list[str]
    value_equation: str

    @classmethod
    def from_dict(cls, data: Any) -> DiscoveryGuide:
        d = _obj("discovery_guide", data, ["questions", "value_equation"])
        return cls(
            questions=_str_list("discovery_guide", d["questions"], "questions"),
            value_equation=str(d["value_equation"]),
        )


@dataclass
class ThreeWs:
    why_change: str
    why_now: str
    why_us: str
    gaps: list[str]

    @classmethod
    def from_dict(cls, data: Any) -> ThreeWs:
        d = _obj("three_ws", data, ["why_change", "why_now", "why_us", "gaps"])
        return cls(
            why_change=str(d["why_change"]),
            why_now=str(d["why_now"]),
            why_us=str(d["why_us"]),
            gaps=_str_list("three_ws", d["gaps"], "gaps"),
        )
