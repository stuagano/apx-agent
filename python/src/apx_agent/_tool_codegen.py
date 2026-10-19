"""Generation-time translation of tool specs into ordinary Python declarations."""

from __future__ import annotations

import inspect
import keyword
import re
from dataclasses import fields
from typing import Any, get_origin

from ._tool_config import _registry
from ._tool_scope import parse_tool_scope


def _python_value(value: Any, imports: set[str]) -> str:
    """Render values without resolving environment references on the build machine."""
    if isinstance(value, str):
        match = re.fullmatch(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", value)
        if match:
            imports.add("import os")
            return f"os.environ[{(match[1] or match[2])!r}]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k!r}: {_python_value(v, imports)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_python_value(v, imports) for v in value) + "]"
    if isinstance(value, tuple):
        return "(" + "".join(f"{_python_value(v, imports)}, " for v in value) + ")"
    if value is None or isinstance(value, (str, int, float, bool)):
        return repr(value)
    raise ValueError(f"Tool configuration cannot be rendered as Python: {type(value).__name__}")


def render_tool_group(
    raw_tools: list[dict[str, Any]],
    module: str,
    blocks: list[str],
    modules: dict[str, str] | None,
) -> str:
    """Append direct declarations (or package imports) and return a flat list expression."""
    if not isinstance(raw_tools, list):
        raise ValueError(f"{module} tools must be a list")
    imports: set[str] = set()
    statements: list[str] = []
    refs: list[str] = []
    registry = _registry()
    for index, raw in enumerate(raw_tools):
        if not isinstance(raw, dict):
            raise ValueError(f"{module} tool #{index} must be a mapping")
        kwargs = dict(raw)
        kind = kwargs.pop("type", None)
        scope = parse_tool_scope(**{
            key: kwargs.pop(key, None)
            for key in ("identity", "scope", "secret_scopes", "callers")
        })
        var = f"_apx_{module}_{index}"
        toolkit = False
        if kind == "python":
            ref = kwargs.pop("module", None)
            parts = ref.split(":") if isinstance(ref, str) else []
            if (len(parts) != 2 or not all(
                piece.isidentifier() and not keyword.iskeyword(piece)
                for part in parts for piece in part.split(".")
            ) or "." in parts[-1]):
                raise ValueError(f"{module} python tool #{index} requires module: 'pkg.mod:fn'")
            if kwargs:
                raise ValueError(f"{module} python tool #{index} has unknown keys: {sorted(kwargs)}")
            imports.add(f"from {parts[0]} import {parts[1]} as {var}")
        else:
            factory = registry.get(kind) if isinstance(kind, str) else None
            if factory is None:
                raise ValueError(f"{module} tool #{index}: unknown type {kind!r}")
            if kind == "uc_function" and "function" in kwargs and "function_name" not in kwargs:
                kwargs["function_name"] = kwargs.pop("function")
            if kind == "vector_search" and "index" in kwargs and "index_name" not in kwargs:
                kwargs["index_name"] = kwargs.pop("index")
            signature = inspect.signature(factory)
            try:
                signature.bind(**kwargs)
            except TypeError as exc:
                raise ValueError(f"{module} tool #{index} ({kind}): {exc}") from exc
            annotation = signature.return_annotation
            toolkit = annotation is list or get_origin(annotation) is list or str(annotation).startswith("list")
            # Generated tools are user-owned source: import the public export.
            imports.add(f"from apx_agent import {factory.__name__}")
            args = ", ".join(f"{key}={_python_value(value, imports)}" for key, value in kwargs.items())
            statements.append(f"{var} = {factory.__name__}({args})")
        if scope is not None:
            imports.add("from apx_agent import ToolScope, build_tool")
            # build_tool leaves existing resources attached when resources is omitted.
            scope_args = ", ".join(
                f"{field.name}={_python_value(getattr(scope, field.name), imports)}"
                for field in fields(scope) if getattr(scope, field.name)
            )
            fn = "_tool" if toolkit else var
            scoped = (
                f"build_tool({fn}, name={fn}.__name__, description={fn}.__doc__, "
                f"scope=ToolScope({scope_args}))"
            )
            statements.append(f"{var} = [{scoped} for _tool in {var}]" if toolkit else f"{var} = {scoped}")
        refs.append(("*" if toolkit else "") + var)
    expression = "[" + ", ".join(refs) + "]"
    if refs:
        statements.extend([
            f"if len({expression}) != len({{_tool.__name__ for _tool in {expression}}}):",
            f"    raise ValueError('duplicate tool name in {module}')",
        ])
        source = "\n".join(sorted(imports)) + "\n\n" + "\n".join(statements) + "\n"
        if modules is None:
            blocks.append(source)
        else:
            modules[module] = source
            blocks.append(f"from tools.{module} import " + ", ".join(ref.lstrip("*") for ref in refs))
    return expression
