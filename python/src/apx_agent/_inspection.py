"""Function inspection helpers — introspect tool functions, build schemas, load config."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Annotated, Any, NamedTuple, get_args, get_origin, get_type_hints

from fastapi import params
from pydantic import BaseModel, ConfigDict, create_model

from ._models import AgentConfig, _ToolFn
from ._state_marker import _STATE_DEP


class ToolSignature(NamedTuple):
    plain_params: dict[str, tuple[Any, Any]]
    dep_param_names: list[str]


def _is_fastapi_dependency(annotation: Any) -> bool:
    """Return True if the annotation is a FastAPI Depends (Dependencies.*)."""
    if get_origin(annotation) is not Annotated:
        return False
    return any(isinstance(arg, params.Depends) for arg in get_args(annotation))


def _is_state_dependency(annotation: Any) -> bool:
    """Return True if the annotation is ``Dependencies.State``."""
    if get_origin(annotation) is not Annotated:
        return False
    return any(arg is _STATE_DEP for arg in get_args(annotation))


def _state_param_name(fn: _ToolFn) -> str | None:
    """Return the name of the fn's Dependencies.State parameter, or None."""
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}
    for name in inspect.signature(fn).parameters:
        if _is_state_dependency(hints.get(name, Any)):
            return name
    return None


def _inspect_tool_fn(fn: _ToolFn) -> ToolSignature:
    """Inspect a tool function's signature.

    Returns:
        plain_params: {name: (type, default)} for tool input parameters
        dep_param_names: list of parameter names that are FastAPI dependencies
    """
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}

    sig = inspect.signature(fn)
    plain_params: dict[str, tuple[Any, Any]] = {}
    dep_param_names: list[str] = []

    for name, param in sig.parameters.items():
        annotation = hints.get(name, Any)
        if _is_state_dependency(annotation):
            continue                            # injected per-call, not in schema/deps
        if _is_fastapi_dependency(annotation):
            dep_param_names.append(name)
        else:
            default = param.default if param.default is not inspect.Parameter.empty else ...
            plain_params[name] = (annotation, default)

    return ToolSignature(plain_params=plain_params, dep_param_names=dep_param_names)


class _EmptyToolInput(BaseModel):
    """Input model for a zero-argument tool.

    Binding a zero-arg tool with ``args_schema=None`` makes langchain infer a
    schema from the ``**kwargs`` wrapper — ``{"kwargs": {"additionalProperties":
    true, ...}}`` — which FMAPI rejects with 400 'the "additionalProperties"
    keyword must be False or not specified', 500-ing every conversation at the
    first LLM call (#439). ``extra="forbid"`` keeps the emitted schema a plain
    empty object.
    """

    model_config = ConfigDict(extra="forbid")


#: The empty-object input schema FMAPI accepts for zero-argument tools (#439).
_EMPTY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def _make_input_model(fn: _ToolFn, plain_params: dict[str, tuple[Any, Any]]) -> type[BaseModel] | None:
    """Dynamically create a Pydantic input model from the plain parameters."""
    if not plain_params:
        return None
    fields = {name: (annotation, default) for name, (annotation, default) in plain_params.items()}
    return create_model(f"{fn.__name__}_input", **fields)  # type: ignore


def _make_route_handler(
    fn: _ToolFn,
    input_model: type[BaseModel] | None,
    dep_param_names: list[str],
) -> Any:
    """Create a FastAPI route handler that calls fn with injected dependencies."""
    hints = get_type_hints(fn, include_extras=True)
    dep_annotations = {name: hints[name] for name in dep_param_names if name in hints}

    _is_async = inspect.iscoroutinefunction(fn)

    if input_model is not None:
        # Build a handler: (body: InputModel, dep1: Dep1, ...) -> return_type
        if _is_async:
            async def handler_with_body(body: Any, **kwargs: Any) -> Any:
                return await fn(**body.model_dump(), **kwargs)
        else:
            async def handler_with_body(body: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
                return fn(**body.model_dump(), **kwargs)

        handler_with_body.__annotations__ = {
            "body": input_model,
            **dep_annotations,
            "return": hints.get("return", Any),
        }
        _patch_handler_signature(handler_with_body, input_model, dep_annotations)
        handler_with_body.__name__ = fn.__name__  # type: ignore[method-assign]
        handler_with_body.__doc__ = fn.__doc__  # type: ignore[method-assign]
        return handler_with_body
    else:
        if _is_async:
            async def handler_no_body(**kwargs: Any) -> Any:
                return await fn(**kwargs)
        else:
            async def handler_no_body(**kwargs: Any) -> Any:  # type: ignore[misc]
                return fn(**kwargs)

        handler_no_body.__annotations__ = {**dep_annotations, "return": hints.get("return", Any)}
        _patch_handler_signature(handler_no_body, None, dep_annotations)
        handler_no_body.__name__ = fn.__name__  # type: ignore[method-assign]
        handler_no_body.__doc__ = fn.__doc__  # type: ignore[method-assign]
        return handler_no_body


def _patch_handler_signature(
    handler: Any,
    input_model: type[BaseModel] | None,
    dep_annotations: dict[str, Any],
) -> None:
    """Replace handler's inspect.Signature so FastAPI sees the right parameters."""
    parameters: list[inspect.Parameter] = []

    if input_model is not None:
        parameters.append(
            inspect.Parameter("body", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=input_model)
        )

    for dep_name, dep_annotation in dep_annotations.items():
        parameters.append(
            inspect.Parameter(dep_name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=dep_annotation)
        )

    handler.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_agent_config(
    section_path: tuple[str, ...] = ("tool", "apx", "agent"),
    pyproject_path: Path | str | None = None,
) -> AgentConfig | None:
    """Read agent config from pyproject.toml. Returns None if absent.

    ``section_path`` defaults to ``("tool", "apx", "agent")`` for APX compatibility.
    Override to e.g. ``("tool", "agent")`` for standalone projects.

    ``pyproject_path`` can be an explicit path to the pyproject.toml file.
    When omitted, the search order is:

    1. Walk up from ``Path.cwd()`` — but only if the found pyproject declares
       agent config in ``section_path``. The operator ran the command from the
       project, so that IS the project (#437: the ``apx-agent`` console script
       lives in the framework venv's ``bin/``, so the ``__main__`` walk-up used
       to find the framework repo's pyproject and shadow the project's).
    2. Walk up from ``__main__.__file__`` — the entry-point module (e.g. the
       consumer's ``app.py``). Covers serving a project from elsewhere, such
       as deployed Databricks Apps launching ``app.py`` directly.
    3. Walk up from ``Path.cwd()`` unconditionally — final fallback for
       interactive / test use.
    """
    import logging
    import os
    import sys
    import tomllib

    def _find_pyproject(start: Path) -> Path | None:
        for directory in [start, *start.parents]:
            candidate = directory / "pyproject.toml"
            if candidate.exists():
                return candidate
        return None

    def _config_fields(path: Path) -> dict[str, Any] | None:
        """AgentConfig fields declared at ``section_path`` in *path*, or None."""
        with open(path, "rb") as f:
            data = tomllib.load(f)
        section = data
        for key in section_path:
            section = section.get(key, {})
            if not section:
                return None
        fields = {k: v for k, v in section.items() if k in AgentConfig.model_fields}
        # A section holding only deploy-envelope keys (registered_model,
        # experiment, ...) declares no agent config — same as no section.
        return fields if fields else None

    env_pyproject = os.environ.get("APX_PYPROJECT")
    if pyproject_path is not None:
        resolved = Path(pyproject_path)
    elif env_pyproject:
        # Explicit override — `apx-agent run <spec>.yaml` sets this to the
        # generated project's pyproject so config resolution can't fall through
        # to a nearer pyproject (e.g. the consumer's top-level project) via
        # the cwd/__main__ heuristics.
        resolved = Path(env_pyproject)
    else:
        resolved = None
        # 1. cwd, when its pyproject actually declares agent config (#437).
        cwd_pyproject = _find_pyproject(Path.cwd())
        if cwd_pyproject is not None and _config_fields(cwd_pyproject) is not None:
            resolved = cwd_pyproject
        # 2. __main__'s location — the consumer's entry point when serving
        #    from outside the project directory.
        if resolved is None:
            main_mod = sys.modules.get("__main__")
            main_file = getattr(main_mod, "__file__", None) if main_mod else None
            if main_file:
                resolved = _find_pyproject(Path(main_file).parent)
        # 3. Fallback to cwd even without agent config (returns None below).
        if resolved is None:
            resolved = cwd_pyproject

    if resolved is None or not resolved.exists():
        return None

    fields = _config_fields(resolved)
    if fields is None:
        return None
    config = AgentConfig(**fields)
    logging.getLogger(__name__).info(
        "[%s] loaded from %s (name=%s)", ".".join(section_path), resolved, config.name
    )
    return config


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _schema_for_model(model: type[BaseModel] | None) -> dict[str, Any]:
    """JSON schema for a tool input model.

    A zero-argument tool (``model is None``) advertises the empty-object
    schema — never ``null`` — so the A2A card's ``inputSchema`` matches what
    the LLM bind sends to FMAPI (#439).
    """
    if model is None:
        return dict(_EMPTY_INPUT_SCHEMA)
    return model.model_json_schema()


def _schema_for_return(fn: _ToolFn) -> dict[str, Any] | None:
    hints = get_type_hints(fn)
    return_type = hints.get("return")
    if return_type is None or return_type is type(None):
        return None
    if isinstance(return_type, type) and issubclass(return_type, BaseModel):
        return return_type.model_json_schema()
    return {"type": "string"}
