"""Template protocol + registry — the agent-as-config foundation (E1).

A Template turns a small typed spec into a configured leaf agent: it wires
governed tools and produces *grounded* instructions for a role. ``DataAgent``
is the reference implementation. Persona (model, instruction tone, generation
knobs) is NOT a template's job — it stays in the ``[tool.apx.agent]`` envelope
and is layered on afterward via ``apply_config_knobs`` (``_wiring.py``).

Built-in templates register via the ``@template`` decorator at import time.
Third-party / cross-repo templates auto-register via the ``apx_agent.templates``
entry-point group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class Template(Protocol):
    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    Spec: ClassVar[type[BaseModel]]

    def build(self, spec: BaseModel, *, ws: Any | None = None) -> Any: ...


@dataclass(frozen=True)
class TemplateInfo:
    """Catalog-facing metadata for a template — no template code needed to render."""

    name: str
    title: str
    description: str
    spec_schema: dict[str, Any]

    @classmethod
    def from_template(cls, tmpl: Template) -> "TemplateInfo":
        return cls(
            name=tmpl.name,
            title=tmpl.title,
            description=tmpl.description,
            spec_schema=tmpl.Spec.model_json_schema(),
        )
