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

import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

logger = logging.getLogger(__name__)


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
    def from_template(cls, tmpl: Template) -> TemplateInfo:
        return cls(
            name=tmpl.name,
            title=tmpl.title,
            description=tmpl.description,
            spec_schema=tmpl.Spec.model_json_schema(),
        )


class TemplateRegistry:
    """Name → Template registry. Built-ins via @template; third-party via entry points."""

    ENTRY_POINT_GROUP: ClassVar[str] = "apx_agent.templates"

    def __init__(self) -> None:
        self._templates: dict[str, Template] = {}
        self._discovered = False

    def register(self, tmpl_cls: type) -> type:
        # Typed as ``type`` (not ``type[Template]``) because a Protocol with a
        # mutable ``ClassVar`` (``Spec``) is invariant — concrete templates like
        # ``DataTemplate`` would fail a static ``type[Template]`` check. The
        # runtime ``isinstance`` below is the real conformance gate.
        inst = tmpl_cls()
        if not isinstance(inst, Template):
            raise ValueError(f"{tmpl_cls!r} does not implement the Template protocol.")
        name = inst.name
        if name in self._templates:
            raise ValueError(
                f"Template {name!r} already registered "
                f"(by {type(self._templates[name]).__module__})."
            )
        self._templates[name] = inst
        return tmpl_cls

    def get(self, name: str) -> Template:
        self._ensure_discovered()
        if name not in self._templates:
            available = ", ".join(sorted(self._templates)) or "(none)"
            raise ValueError(f"Unknown template {name!r}. Available: {available}.")
        return self._templates[name]

    def list(self) -> list[TemplateInfo]:
        self._ensure_discovered()
        return [TemplateInfo.from_template(t) for t in self._templates.values()]

    def build(self, name: str, spec: dict | BaseModel, *, ws: Any | None = None) -> Any:
        tmpl = self.get(name)
        validated = spec if isinstance(spec, BaseModel) else tmpl.Spec.model_validate(spec)
        return tmpl.build(validated, ws=ws)

    def _ensure_discovered(self) -> None:
        if self._discovered:
            return
        self._discovered = True  # set first so a failure doesn't retry-loop
        self._load_entry_points()

    def _load_entry_points(self) -> None:
        from importlib.metadata import entry_points

        try:
            eps = entry_points(group=self.ENTRY_POINT_GROUP)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Template entry-point discovery failed: %s", e)
            return
        for ep in eps:
            try:
                self.register(ep.load())
            except Exception as e:
                logger.warning("Skipping bad template entry point %r: %s", ep.name, e)


template_registry = TemplateRegistry()


_T = TypeVar("_T")


def template(tmpl_cls: type[_T]) -> type[_T]:
    """Decorator: register a Template class on the module-level registry.

    Generic passthrough so the decorated class keeps its concrete type; runtime
    conformance is enforced by ``register`` (see its note).
    """
    template_registry.register(tmpl_cls)
    return tmpl_cls
