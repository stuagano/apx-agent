# Design: E1 · Template Protocol

**Date:** 2026-05-29
**Status:** Approved (design); pending implementation plan
**Scope:** `apx-agent` engine only

---

## Context

The "Coworker" use-case (an HR-portal layer that lets users create agents as
data) and the existing `DataAgent` primitive share the same DNA: take a small
declarative spec → wire governed tools → produce grounded instructions →
return a configured agent. `DataAgent` (`python/src/apx_agent/data_agent.py`)
already does this, hardcoded in Python for the Unity Catalog data domain.

This spec generalizes that pattern into a first-class **Template** abstraction.
It is the foundation (E1) of a larger, decomposed program:

| Piece | Layer | Status |
|-------|-------|--------|
| **E1 · Template protocol** | engine | **this spec** |
| E2 · Declarative tool loader (`[tool.apx.tools]`) | engine | scoped — `docs/engine-scope/02` |
| E3 · Template-as-config (`[tool.apx.agent].template`) | engine | future spec |
| C1 · CoworkerSpec + translation | coworker repo | future spec |
| C2 · Catalog, lifecycle, HR-portal UI | coworker repo | future spec |

Dependencies: `C2 → C1 → E3 → {E1, E2}`. E1 is the unscoped foundation; this
spec closes that gap. E1 is independently valuable: it makes `DataAgent` a
clean instance of a general pattern rather than a one-off, and it establishes
the registry/extension point the config and Coworker layers build on.

## Goals

- A typed, registrable `Template` abstraction: `spec → tools + grounded
  instructions → LlmAgent`.
- `DataAgent` retrofitted as the reference implementation, **fully backward
  compatible**.
- A registry supporting **enumeration** (for catalogs) and **cross-repo
  registration** (for Coworker's templates) via Python entry points.
- A clean **role vs persona** split: templates own role/skills; the existing
  `[tool.apx.agent]` envelope owns persona, layered via the `finalize_agent`
  seam established by the generation-knobs fix (commit `c158f0a3`).

## Non-Goals (deferred to later specs)

- Config-by-name template expansion `[tool.apx.agent].template = { name = … }`
  (E3).
- The `[tool.apx.tools]` declarative tool loader (E2 — already scoped).
- Any code in the Coworker repo (C1/C2).
- Memory/session and guards declarative config (#3/#4 — separately scoped).

---

## Design

### 1. Boundary

A **Template** turns a small typed spec into a configured **leaf agent**: it
wires governed tools and produces *grounded* instructions for a role. It does
**not** own persona. Persona — which model, instruction tone, generation knobs
— stays in the `[tool.apx.agent]` envelope (`AgentConfig`) and is layered onto
the built agent afterward via `finalize_agent` (`_wiring.py`). Templates are
used imperatively in `agent.py` in E1 — exactly where `DataAgent(...)` is used
today. Config-driven, by-name expansion is E3.

The HR-metaphor mapping this enables: **template = role** ("Data Analyst");
**envelope = personality/seniority** (warm tone, which model).

### 2. The `Template` protocol

```python
from typing import Any, ClassVar, Protocol
from pydantic import BaseModel

class Template(Protocol):
    name: ClassVar[str]                 # registry key, e.g. "data"
    title: ClassVar[str]                # human label for catalogs
    description: ClassVar[str]          # human description for catalogs
    Spec: ClassVar[type[BaseModel]]     # typed, serializable, validatable spec

    def build(self, spec: BaseModel, *, ws: Any | None = None) -> "LlmAgent":
        """Wire tools + grounded instructions from `spec`; return a leaf agent."""
        ...
```

- `Spec` is a Pydantic model → free validation, JSON-schema export. Both the
  Coworker catalog and E3 config consume that schema.
- `build` returns a real `LlmAgent` (or subclass), so composition
  (`sub_agents`, `SequentialAgent`, `RouterAgent`) works unchanged.

The protocol lives in a new module, e.g. `python/src/apx_agent/_template.py`,
exported from `apx_agent/__init__.py`.

### 3. Registry + discovery

```python
@template                       # decorator: registers by cls.name
class DataTemplate:
    name = "data"
    ...

# public surface
registry.list() -> list[TemplateInfo]        # name/title/description/Spec JSON schema
registry.get(name: str) -> Template
registry.build(name: str, spec: dict | BaseModel, *, ws=None) -> LlmAgent
```

- **Built-ins** register via the `@template` decorator at import time.
- **Cross-repo templates** (Coworker's) auto-register via the
  `apx_agent.templates` Python entry-point group — no explicit imports needed,
  and they appear in `registry.list()` for the catalog:

  ```toml
  # a consumer repo's pyproject.toml
  [project.entry-points."apx_agent.templates"]
  meeting_manager = "coworker.templates:MeetingTemplate"
  ```

- `registry.build` accepts a **raw dict** (validates against the template's
  `Spec`) so config/UI callers need not import the Python class. Passing a
  `Spec` instance skips re-validation.
- `TemplateInfo` carries `name`, `title`, `description`, and the `Spec` JSON
  schema — everything a catalog UI needs without importing template code.
- Entry points are loaded lazily on first registry access and memoized.

### 4. `build()` contract — grounding + graceful degradation

Carries `DataAgent`'s proven behavior into the protocol as the contract:

- `build` is **best-effort**. With `ws`, introspect live (e.g. schema → governed
  resources + grounded instructions). Without `ws`, return a working agent with
  generic instructions and defer introspection. **Never hard-fail for lack of
  `ws`.**
- Grounding output is the template's **default instructions** — the base layer
  for persona composition (§5).
- Any governed resources the template discovers (e.g. `uc_table`) are attached
  to the relevant tool via `attach_resources`, exactly as `DataAgent` does today
  (`data_agent.py:84`), so they flow through existing resource collection.

### 5. Persona layering (the one behavior change)

`build()` sets grounded instructions as the **base**. The envelope overlays
persona via a **compose** step: persona preamble **+** template grounding,
rather than replace.

```
[persona overlay]                     <- from [tool.apx.agent].instructions
You are warm and professional. Be concise.

[template grounding]                  <- from build()
You answer questions about main.sales.
Tables: orders(id, ts, amount...), customers(...)
Always cite the table you queried.
```

Changes required:

- **`_prompt_assembly.py`** gains a compose helper that concatenates a persona
  overlay above the grounded base with a clear separator.
- **`finalize_agent` / `apply_config_knobs`** (`_wiring.py`, from commit
  `c158f0a3`) learns to **overlay** the envelope's `instructions` on top of the
  agent's existing grounded instructions.

⚠️ **Deliberate semantics change to the shipped seam.** Today the envelope's
`instructions` only applies when the instance left it empty (fill/replace). The
new rule:

- instance has grounded instructions **and** envelope has instructions →
  **compose** (overlay).
- exactly one side present → current fill behavior (the non-empty side wins).
- This is guarded so a **plain, non-template `LlmAgent`** with envelope
  instructions keeps the behavior shipped in `c158f0a3` (no surprise compose
  when there's nothing to compose with). The compose path triggers only when
  both sides are non-empty.

### 6. `DataAgent` refactor (reference implementation)

- The wiring + grounding logic moves into `DataTemplate` implementing
  `Template`, with a `Spec`:

  ```python
  class DataTemplate:
      name = "data"
      title = "Data Analyst"
      description = "Talks to a governed Unity Catalog schema."

      class Spec(BaseModel):
          catalog: str
          schema: str
          warehouse_id: str | None = None
          genie_space: str | None = None
          vector_index: str | None = None
          include_functions: bool = True

      def build(self, spec: "DataTemplate.Spec", *, ws=None) -> LlmAgent: ...
  ```

- `DataAgent(...)` stays as a **thin ergonomic alias** preserving today's
  signature and the `from apx_agent import DataAgent` export:

  ```python
  DataAgent("main", "sales", ws=w)
  # ≡ DataTemplate().build(DataTemplate.Spec(catalog="main", schema="sales"), ws=w)
  ```

  The `extra_tools`, `instructions`, `name`, and `**kwargs` (forwarded to
  `LlmAgent`) parameters of today's `DataAgent` are preserved by the alias.
  Existing call sites are untouched.

---

## Error handling

| Failure | Behavior |
|---------|----------|
| Unknown template name | Raise `ValueError` listing available names — fail loud, never silent default. |
| Spec validation fails (dict → `Spec`) | Surface Pydantic `ValidationError` as-is. |
| Duplicate `name` registration | Raise at registration time (decorator or entry-point load), naming the colliding source, so a third-party template can't silently shadow a built-in. |
| Broken entry point (import error / not a `Template`) | Skip-with-warning (log the bad entry point); don't crash `registry.list()`. One bad plugin must not take down discovery. |
| `build` with no `ws` | Graceful degradation (§4): generic instructions, deferred introspection. Not an error. |
| Live introspection fails *with* `ws` (perms, warehouse down) | Best-effort: log, fall back to ungrounded instructions, still return a working agent. Mirrors `DataAgent` today. |
| Persona compose when only one side present | Falls back to current fill semantics; compose only when both grounded + envelope instructions exist (§5 guard). |

## Testing

Run from `python/` via `uv run pytest` (project convention; the root `.venv`
shadows `src/`).

- **Protocol/registry unit tests:** `@template` registration; entry-point
  discovery (fake `apx_agent.templates` entry point); `list()` returns Spec
  schemas; `get`/`build` happy path + unknown-name + duplicate-name +
  broken-entry-point; dict→`Spec` validation (valid + invalid).
- **DataTemplate parity (backward-compat safety net for §6):** assert
  `DataAgent(...)` and `DataTemplate().build(Spec(...))` produce equivalent
  agents — same tools, attached resources, and instructions. Cover both the
  `ws` and no-`ws` branches, reusing existing `DataAgent` fixtures/mocks.
- **Persona compose (the §5 behavior change):** grounded + envelope → composed,
  correct order; grounded only → unchanged; envelope only → fill (current
  behavior); **regression guard** that a plain non-template `LlmAgent` with
  envelope instructions still behaves as `c158f0a3` shipped.
- **Composition smoke:** a `DataTemplate`-built agent works as a `sub_agent`
  and as a `SequentialAgent` leaf.

**Explicitly not tested in E1:** config-by-name expansion and Coworker
integration — those belong to E3/C specs.

---

## Files touched

- **New:** `python/src/apx_agent/_template.py` — `Template` protocol, `@template`
  decorator, registry, `TemplateInfo`, entry-point discovery.
- **Refactor:** `python/src/apx_agent/data_agent.py` — `DataTemplate` reference
  impl + `DataAgent` alias.
- **Change:** `python/src/apx_agent/_prompt_assembly.py` — persona compose
  helper.
- **Change:** `python/src/apx_agent/_wiring.py` — `finalize_agent` instruction
  overlay (extends `c158f0a3`).
- **Export:** `python/src/apx_agent/__init__.py` — `Template`, `template`,
  `registry` (or `template_registry`), `DataTemplate`.
- **Packaging:** declare the `apx_agent.templates` entry-point group convention
  (docs); built-ins registered in-process via decorator.
- **Tests:** new `python/tests/test_template.py`; extend DataAgent/wiring tests.

## Open questions / risks

- **Naming of the public registry symbol** (`registry` vs `template_registry`)
  and decorator (`@template`) — finalize during implementation; avoid collision
  with existing exports.
- **`finalize_agent` overlay semantics** are the only behavioral risk; the
  regression guard test for plain `LlmAgent` is mandatory.
- **`schema` is a reserved-ish word** in some Pydantic/BaseModel contexts; the
  `DataTemplate.Spec.schema` field name may need care (Pydantic v2 reserves
  `.schema()` as a method on the model, not a field — verify no conflict, or
  alias the field).
