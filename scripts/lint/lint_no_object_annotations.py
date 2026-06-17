"""Flag uses of `object` as a type annotation.

`object` conveys nothing more than `Any` to a reader; it tells them
the value is arbitrary but nudges them to isinstance-narrow at every
use. For heterogeneous data prefer a real type, union, TypedDict, or a
named `dict[str, Any]` TypeAlias with a justified
`# type: ignore[explicit-any]` comment explaining the boundary.

Exit code 1 if any uses are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _uses_object(node: ast.expr | None) -> bool:
    """Return True if `node` references the builtin `object` as a type."""
    if node is None:
        return False
    if isinstance(node, ast.Name) and node.id == "object":
        return True
    if isinstance(node, ast.Subscript):
        if _uses_object(node.value):
            return True
        return _uses_object(node.slice)
    if isinstance(node, ast.Tuple):
        return any(_uses_object(elt) for elt in node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _uses_object(node.left) or _uses_object(node.right)
    return False


def scan(path: Path) -> list[tuple[int, str]]:
    """Return (line, snippet) pairs for every `object`-bearing annotation."""
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    source_lines = path.read_text().splitlines()
    hits: list[tuple[int, str]] = []

    def record(node: ast.AST) -> None:
        assert isinstance(node, (ast.stmt, ast.expr, ast.arg))
        line = node.lineno
        snippet = source_lines[line - 1].rstrip() if 0 < line <= len(source_lines) else ""
        hits.append((line, snippet))

    for node in ast.walk(tree):
        if isinstance(node, (ast.AnnAssign, ast.arg)) and _uses_object(node.annotation):
            record(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _uses_object(node.returns):
                record(node)
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
                sys.stdout.write(f"{path}:{line}: `object` used as type annotation: {snippet}\n")
    if failed:
        sys.stdout.write(
            "\n`object` as a type annotation hides shape information from "
            "readers. Replace with a real type, a union, a TypedDict/Protocol, "
            "or a named `dict[str, Any]` TypeAlias (with a justified "
            "`# type: ignore[explicit-any]` comment for JSON boundaries).\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
