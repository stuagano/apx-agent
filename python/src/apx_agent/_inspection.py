"""Function inspection helpers — introspect tool functions, build schemas, load config."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from fastapi import params
from pydantic import BaseModel, create_model

from ._models import AgentConfig, _ToolFn


def _is_fastapi_dependency(annotation: Any) -> bool:
    """Return True if the annotation is a FastAPI Depends (Dependencies.*)."""
    if get_origin(annotation) is not Annotated:
        return False
    return any(isinstance(arg, params.Depends) for arg in get_args(annotation))


def _inspect_tool_fn(fn: _ToolFn) -> tuple[dict[str, tuple[Any, Any]], list[str]]:
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
        if _is_fastapi_dependency(annotation):
            dep_param_names.append(name)
        else:
            default = param.default if param.default is not inspect.Parameter.empty else ...
            plain_params[name] = (annotation, default)

    return plain_params, dep_param_names


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

    1. Walk up from ``__main__.__file__`` — the entry-point module (e.g. the
       consumer's ``app.py``). This is the most reliable heuristic in both
       local dev and deployed Databricks Apps.
    2. Walk up from ``Path.cwd()`` — fallback for interactive / test use.
    """
    import sys
    import tomllib

    def _find_pyproject(start: Path) -> Path | None:
        for directory in [start, *start.parents]:
            candidate = directory / "pyproject.toml"
            if candidate.exists():
                return candidate
        return None

    if pyproject_path is not None:
        resolved = Path(pyproject_path)
    else:
        resolved = None
        # Try __main__'s location first — this is the consumer's entry point
        main_mod = sys.modules.get("__main__")
        main_file = getattr(main_mod, "__file__", None) if main_mod else None
        if main_file:
            resolved = _find_pyproject(Path(main_file).parent)
        # Fallback to cwd
        if resolved is None:
            resolved = _find_pyproject(Path.cwd())

    if resolved is None or not resolved.exists():
        return None

    with open(resolved, "rb") as f:
        data = tomllib.load(f)

    section = data
    for key in section_path:
        section = section.get(key, {})
        if not section:
            return None

    return AgentConfig(**{k: v for k, v in section.items() if k in AgentConfig.model_fields})


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _schema_for_model(model: type[BaseModel] | None) -> dict[str, Any] | None:
    if model is None:
        return None
    return model.model_json_schema()


def _schema_for_return(fn: _ToolFn) -> dict[str, Any] | None:
    hints = get_type_hints(fn)
    return_type = hints.get("return")
    if return_type is None or return_type is type(None):
        return None
    if isinstance(return_type, type) and issubclass(return_type, BaseModel):
        return return_type.model_json_schema()
    return {"type": "string"}
