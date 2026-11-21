"""Per-agent Python client generated from declared Databricks SDK operations.

Declare the operations on the agent, then generate a client that exposes only
those methods. The generated methods call ``WorkspaceClient`` and return its
result unchanged. They do not open a second HTTP client and they do not read
credentials::

    from pathlib import Path

    from apx_agent import Agent, PlatformOperation, declare_platform_operations, generate_platform_client

    agent = Agent(name="sales", tools=[])
    declare_platform_operations(agent, [
        PlatformOperation("list_catalogs", "catalogs.list", fields=("include_browse",)),
    ])
    path = generate_platform_client(agent, Path("generated"))
    # generated/sales_platform_client.py — regenerate; do not edit.
"""

from __future__ import annotations

import inspect
import keyword
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

_OPERATION = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\Z")
_PASSABLE = frozenset({
    inspect.Parameter.KEYWORD_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
})


@dataclass(frozen=True)
class PlatformOperation:
    """One Databricks SDK operation an agent may call.

    ``name`` is the generated method. ``operation`` is ``service.method`` on
    ``WorkspaceClient`` (no wildcards). ``fields`` are the only request
    parameters that method accepts; other SDK parameters stay at their defaults.
    """

    name: str
    operation: str
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.fields, tuple) or any(not isinstance(field, str) for field in self.fields):
            raise TypeError("fields must be a tuple of strings")
        _check_name(self.name, what="operation name")
        if "*" in self.operation:
            raise ValueError(
                "A wildcard or all-APIs declaration is not a platform operation: "
                f"{self.operation!r}"
            )
        if _OPERATION.fullmatch(self.operation) is None:
            raise ValueError(
                "operation must be a WorkspaceClient service.method path, "
                f"got {self.operation!r}"
            )
        if len(set(self.fields)) != len(self.fields):
            raise ValueError(f"{self.name} repeats a request field: {self.fields!r}")
        for field in self.fields:
            _check_name(field, what=f"{self.name} request field")


@dataclass(frozen=True)
class _Param:
    annotation: Any
    required: bool
    default: Any


@dataclass(frozen=True)
class _Resolved:
    service: str
    method: str
    params: dict[str, _Param]
    return_type: Any


def declare_platform_operations(agent: Any, operations: Sequence[PlatformOperation]) -> None:
    """Validate ``operations`` and store them on ``agent`` for generation.

    ``agent`` is any agent instance. The declaration is ``_platform_operations``.
    Replaces a previous declaration. Does not call Databricks.
    """

    agent._platform_operations = tuple(_validate(operations))


def generate_platform_client(agent: Any, dest: Path) -> Path:
    """Write ``<dest>/<agent.name>_platform_client.py`` and return that path.

    The agent name must be a Python identifier (``Agent(name=...)``). An
    unchanged declaration does not rewrite the file. Does not call Databricks.
    """

    name = _agent_name(agent)
    source = _render_source(name, _validate(_declared(agent)))
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise ValueError(f"dest must be a directory, got {dest}") from exc
    if not dest.is_dir():
        raise ValueError(f"dest must be a directory, got {dest}")
    path = dest / f"{name}_platform_client.py"
    if path.is_file() and path.read_text(encoding="utf-8") == source:
        return path
    path.write_text(source, encoding="utf-8")
    return path


def _declared(agent: Any) -> list[Any]:
    try:
        raw = agent._platform_operations
    except AttributeError:
        return []
    if not isinstance(raw, tuple):
        raise TypeError("agent._platform_operations must be a tuple of PlatformOperation")
    return list(raw)


def _agent_name(agent: Any) -> str:
    try:
        name = agent._name
    except AttributeError as exc:
        raise ValueError("generate_platform_client requires the agent to declare a name") from exc
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError("generate_platform_client requires the agent's name to be a Python identifier")
    return name


def _check_name(name: str, *, what: str) -> None:
    if not name.isidentifier() or keyword.iskeyword(name) or name.startswith("_"):
        raise ValueError(f"{what} must be a public Python identifier, got {name!r}")


def _validate(operations: Sequence[PlatformOperation]) -> list[PlatformOperation]:
    if isinstance(operations, PlatformOperation):
        raise TypeError("operations must be a sequence of PlatformOperation, not one operation")
    seen: set[str] = set()
    validated: list[PlatformOperation] = []
    for operation in operations:
        if not isinstance(operation, PlatformOperation):
            raise TypeError(
                f"platform operation must be PlatformOperation, got {type(operation).__name__}"
            )
        if operation.name in seen:
            raise ValueError(f"duplicate platform operation name {operation.name!r}")
        seen.add(operation.name)
        _resolve(operation)
        validated.append(operation)
    return validated


def _workspace_client() -> type[Any]:
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient


def _resolve(operation: PlatformOperation) -> _Resolved:
    service, method = operation.operation.split(".", 1)
    workspace = _workspace_client()
    try:
        descriptor = inspect.getattr_static(workspace, service)
    except AttributeError:
        raise ValueError(f"{operation.operation} is not a WorkspaceClient operation") from None
    if not isinstance(descriptor, property) or descriptor.fget is None:
        raise ValueError(f"{operation.operation} is not a WorkspaceClient operation")
    api = inspect.get_annotations(descriptor.fget, eval_str=True).get("return")
    if not isinstance(api, type):
        raise ValueError(f"{operation.operation} is not a WorkspaceClient operation")
    try:
        func = inspect.getattr_static(api, method)
    except AttributeError:
        raise ValueError(f"{operation.operation} is not a WorkspaceClient operation") from None
    if isinstance(func, (staticmethod, classmethod)):
        func = func.__func__
    if not inspect.isfunction(func):
        raise ValueError(f"{operation.operation} is not a WorkspaceClient operation")
    signature = inspect.signature(func)
    evaluated = inspect.get_annotations(func, eval_str=True)
    params: dict[str, _Param] = {}
    required: list[str] = []
    for param_name, param in signature.parameters.items():
        if param_name == "self" or param.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if param.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise ValueError(
                f"{operation.operation} parameter {param_name} is positional-only and cannot be generated"
            )
        if param.kind not in _PASSABLE:
            raise ValueError(f"{operation.operation} parameter {param_name} cannot be generated")
        is_required = param.default is inspect.Parameter.empty
        params[param_name] = _Param(
            evaluated.get(param_name, Any),
            is_required,
            None if is_required else param.default,
        )
        if is_required:
            required.append(param_name)
    unknown = [field for field in operation.fields if field not in params]
    if unknown:
        raise ValueError(f"{operation.operation} has no request field {unknown[0]!r}")
    missing = [field for field in required if field not in operation.fields]
    if missing:
        raise ValueError(f"{operation.operation} requires request field {missing[0]!r}")
    return _Resolved(service, method, params, evaluated.get("return", Any))


def _class_name(agent_name: str) -> str:
    pascal = "".join(part[:1].upper() + part[1:] for part in agent_name.split("_") if part)
    return f"{pascal}PlatformClient"


def _render_source(agent_name: str, operations: list[PlatformOperation]) -> str:
    imports: set[tuple[str, str]] = {("databricks.sdk", "WorkspaceClient")}
    methods = [_render_method(operation, _resolve(operation), imports) for operation in operations]
    header = [
        "# Generated by apx_agent.generate_platform_client. Do not edit.",
        f'"""Platform client for agent {agent_name}. Regenerated from declared operations; do not edit."""',
        "",
        "from __future__ import annotations",
        "",
        *_import_lines(imports),
        "",
        f"class {_class_name(agent_name)}:",
        f'    """Generated Databricks client for agent {agent_name!r}. Only declared operations."""',
        "",
        "    def __init__(self, workspace: WorkspaceClient) -> None:",
        "        self._workspace = workspace",
    ]
    body = "\n".join(header)
    if methods:
        body = body + "\n\n" + "\n\n".join(methods)
    return body + "\n"


def _render_method(operation: PlatformOperation, resolved: _Resolved, imports: set[tuple[str, str]]) -> str:
    rendered: list[str] = []
    for field in operation.fields:
        param = resolved.params[field]
        annotation = _render_type(param.annotation, imports)
        if param.required:
            rendered.append(f"{field}: {annotation}")
        else:
            rendered.append(f"{field}: {annotation} = {_render_default(param.default, operation)}")
    args = f"self, *, {', '.join(rendered)}" if rendered else "self"
    call = ", ".join(f"{field}={field}" for field in operation.fields)
    returns = _render_type(resolved.return_type, imports)
    return (
        f"    def {operation.name}({args}) -> {returns}:\n"
        f"        return self._workspace.{resolved.service}.{resolved.method}({call})"
    )


def _render_default(value: Any, operation: PlatformOperation) -> str:
    if isinstance(value, bool) or value is None or isinstance(value, (int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{operation.operation} default for a declared field is not a Python literal")
        return repr(value)
    raise ValueError(f"{operation.operation} default for a declared field is not a Python literal")


def _render_type(tp: Any, imports: set[tuple[str, str]]) -> str:
    if tp is Any:
        imports.add(("typing", "Any"))
        return "Any"
    if tp is type(None):
        return "None"
    origin = get_origin(tp)
    if origin is Union or origin is UnionType:
        args = get_args(tp)
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            return f"{_render_type(non_none[0], imports)} | None"
        return " | ".join(_render_type(arg, imports) for arg in args)
    if origin is not None:
        if not isinstance(origin, type):
            raise ValueError(f"cannot render annotation {tp!r}")
        args = ", ".join(_render_type(arg, imports) for arg in get_args(tp))
        name = _import_name(origin.__module__, origin.__name__, imports)
        return f"{name}[{args}]" if args else name
    if isinstance(tp, type):
        if tp.__module__ == "builtins":
            return tp.__qualname__
        return _import_name(tp.__module__, tp.__name__, imports)
    raise ValueError(f"cannot render annotation {tp!r}")


def _import_name(module: str, name: str, imports: set[tuple[str, str]]) -> str:
    if module != "builtins":
        imports.add((module, name))
    return name


def _import_lines(imports: set[tuple[str, str]]) -> list[str]:
    owners: dict[str, str] = {}
    for module, name in imports:
        previous = owners.get(name)
        if previous is not None and previous != module:
            raise ValueError(f"generated client cannot import {name!r} from both {previous} and {module}")
        owners[name] = module
    grouped: dict[str, list[str]] = {}
    for module, name in imports:
        grouped.setdefault(module, []).append(name)
    stdlib: list[str] = []
    third: list[str] = []
    for module, names in sorted(grouped.items()):
        line = f"from {module} import {', '.join(sorted(set(names)))}"
        if module.split(".", 1)[0] == "databricks":
            third.append(line)
        else:
            stdlib.append(line)
    lines = list(stdlib)
    if stdlib and third:
        lines.append("")
    lines.extend(third)
    return lines
