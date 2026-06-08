"""Flag `-> tuple[...]` return types on functions.

Tuple returns are positionally fragile: callers depend on the ordering
of the elements and the relationship between them has to be restated
at every call site (`host, token = _read_cfg(...)`). When the function
needs a third value or a new optional field, every caller breaks.

Prefer a lightweight `@dataclass` with named fields. For the 2-element
case a `typing.NamedTuple` is acceptable when the pair is conceptually
one opaque value (e.g. ``(x, y)`` coordinates) and has a stable
semantic name — annotate it and note that choice.

Exceptions (still allowed, mechanically):
- Single-element tuples (very rare, usually genuine sum types).
- Callable parameter lists (`Callable[[int, str], X]` — the tuple
  inside the first slot isn't a return type).
- Plain ``tuple`` without subscript (already flagged by mypy's
  `disallow_any_generics`).

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_tuple_subscript(node: ast.expr | None) -> bool:
    """Return True if `node` spells ``tuple[...]`` with 2+ element types."""
    if not isinstance(node, ast.Subscript):
        return False
    value = node.value
    if isinstance(value, ast.Name) and value.id == "tuple":
        pass
    elif isinstance(value, ast.Attribute) and value.attr == "tuple":
        pass
    else:
        return False
    slice_expr = node.slice
    if isinstance(slice_expr, ast.Tuple):
        return len(slice_expr.elts) >= 2
    return False


def scan(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    source_lines = path.read_text().splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_tuple_subscript(
            node.returns
        ):
            line = node.returns.lineno if node.returns is not None else node.lineno
            snippet = source_lines[line - 1].strip() if 0 < line <= len(source_lines) else ""
            hits.append((line, snippet))
    return hits


def main(argv: list[str]) -> int:
    failed = False
    for arg in argv[1:]:
        path = Path(arg)
        if not path.is_file():
            continue
        hits = scan(path)
        if hits:
            failed = True
            for line, snippet in hits:
                sys.stdout.write(
                    f"{path}:{line}: `-> tuple[...]` return is positionally fragile: {snippet}\n"
                )
    if failed:
        sys.stdout.write(
            "\nTuple returns are positionally fragile: callers depend on "
            "element order and every new field is a breaking change. "
            "Replace with a `@dataclass` that has named fields. `NamedTuple` "
            "is acceptable when the pair is conceptually one opaque value "
            "(e.g. 2D coordinates) — annotate that choice.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
