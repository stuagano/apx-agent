"""Render completed discovery run state into a single markdown brief."""
from __future__ import annotations

from typing import Any

from .schemas import DiscoveryGuide, HeatMap, Priorities, ThreeWs, ValueMatrices, WowSelection


def render_markdown_brief(state: dict[str, Any], customer: str) -> str:
    lines: list[str] = [f"# Discovery Brief: {customer}", ""]

    priorities = state.get("priorities")
    if isinstance(priorities, Priorities):
        lines += ["## Strategic Priorities", "", f"**Industry:** {priorities.industry}", ""]
        lines += [f"- {p}" for p in priorities.priorities] + [""]

    matrices = state.get("value_matrices")
    if isinstance(matrices, ValueMatrices):
        lines += ["## Strategic Value Matrices", "", "| Outcome | Capability | Enabler |", "| --- | --- | --- |"]
        lines += [f"| {r.outcome} | {r.capability} | {r.enabler} |" for r in matrices.rows] + [""]

    heat = state.get("heat_map")
    if isinstance(heat, HeatMap):
        lines += ["## Value/Effort Heat Map", "", "| Outcome | Value | Effort |", "| --- | --- | --- |"]
        lines += [f"| {c.outcome} | {c.value_score} | {c.effort_score} |" for c in heat.cells] + [""]

    wow = state.get("wow_selection")
    if isinstance(wow, WowSelection):
        lines += ["## Wow Selection", ""]
        lines += [f"- {c}" for c in wow.chosen]
        lines += ["", f"_Rationale:_ {wow.rationale}", ""]

    guide = state.get("discovery_guide")
    if isinstance(guide, DiscoveryGuide):
        lines += ["## Discovery Guide", ""]
        lines += [f"- {q}" for q in guide.questions]
        lines += ["", f"**Value equation:** {guide.value_equation}", ""]

    three = state.get("three_ws")
    if isinstance(three, ThreeWs):
        lines += ["## The 3 Ws", ""]
        lines += [f"**Why Change:** {three.why_change}", "", f"**Why Now:** {three.why_now}", ""]
        lines += [f"**Why Us:** {three.why_us}", "", "**Open gaps:**"]
        lines += [f"- {g}" for g in three.gaps] + [""]

    return "\n".join(lines).rstrip() + "\n"
