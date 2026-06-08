"""Flag `getattr(obj, "literal_name", ...)` calls.

`getattr(x, "foo", default)` silently returns `Any` and bypasses mypy's
attribute checks — if the attribute exists on ``x``'s type, use direct
``x.foo`` access instead so mypy can verify the shape. If it doesn't
exist on ``x`` because the type is too broad, fix the type.

Exceptions (still allowed):
- Dynamic attribute names: `getattr(x, name, ...)` where `name` is not
  a string literal.
- `getattr(x, "__dict__", ...)` / `getattr(x, "__class__", ...)` and
  other dunder names — those are the idiomatic way to read protocol
  attributes across heterogeneous objects.

Exit code 1 if any literal-name calls are found in argv.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def scan(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    source_lines = path.read_text().splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and not _is_dunder(node.args[1].value)
        ):
            line = node.lineno
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
                    f'{path}:{line}: `getattr(..., "literal", ...)` hides the '
                    f"attribute from type checking: {snippet}\n"
                )
    if failed:
        sys.stdout.write(
            '\n`getattr(x, "<literal>", default)` returns Any and bypasses '
            "mypy's attr-defined check. Use direct attribute access and fix "
            "the type of `x` so the attribute is visible to the checker. If "
            "`x` has an open shape (Protocol / union of disjoint types), add "
            "an isinstance narrowing before the access. Dynamic attribute "
            "names (non-literal) and dunder names are still allowed.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
