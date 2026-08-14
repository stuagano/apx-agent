"""
Step definitions: the stable step keys, their JSON schemas, their parsers, and
prompt rendering from overridable template files.

Each techpov step is one ``engine.step(run_id, step_key, handler)``. A handler
renders ``prompts/{step_key}.md`` against accumulated run state, calls the
injected completion with the step's schema, and parses the result into the
step's dataclass. Prompt templates are files loaded by step_key so a domain
agent can override any of them by pointing at its own prompts dir.
"""
from __future__ import annotations

import json
import string
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from .schemas import (
    DiscoveryGuide,
    HeatMap,
    Priorities,
    ThreeWs,
    ValueMatrices,
    WowSelection,
)

STEP_KEYS: list[str] = [
    "priorities",
    "value_matrices",
    "heat_map",
    "wow_selection",
    "discovery_guide",
    "three_ws",
]

PARSERS: dict[str, Callable[[Any], Any]] = {
    "priorities": Priorities.from_dict,
    "value_matrices": ValueMatrices.from_dict,
    "heat_map": HeatMap.from_dict,
    "wow_selection": WowSelection.from_dict,
    "discovery_guide": DiscoveryGuide.from_dict,
    "three_ws": ThreeWs.from_dict,
}

# Per-step response schemas passed to the completion (real providers use them
# to constrain output; the shape mirrors the dataclass each parser expects).
SCHEMAS: dict[str, dict[str, Any]] = {
    "priorities": {"required": ["industry", "priorities"]},
    "value_matrices": {"required": ["rows"]},
    "heat_map": {"required": ["cells"]},
    "wow_selection": {"required": ["chosen", "rationale"]},
    "discovery_guide": {"required": ["questions", "value_equation"]},
    "three_ws": {"required": ["why_change", "why_now", "why_us", "gaps"]},
}

DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _stringify(value: Any) -> str:
    if is_dataclass(value) and not isinstance(value, type):
        return json.dumps(asdict(value), indent=2)
    return json.dumps(value, indent=2, default=str)


def render_prompt(
    step_key: str,
    context: dict[str, Any],
    prompts_dir: Path,
) -> str:
    """Render prompts/{step_key}.md against context (missing keys left blank)."""
    template = (prompts_dir / f"{step_key}.md").read_text(encoding="utf-8")
    rendered = {k: (v if isinstance(v, str) else _stringify(v)) for k, v in context.items()}
    return string.Template(template).safe_substitute(rendered)
