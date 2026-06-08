"""Flag ``<expr> or ""`` — empty-string coercion of ``str | None``.

This is the same empty-string antipattern as ``.get(key, "")`` and
``foo: str = ""`` in disguise. When a caller has a ``str | None`` value
and coerces it to ``str`` with ``or ""`` before handing it off, the
fix is NOT to swallow the ``None`` at the call site — it's to make the
receiving function/parameter accept ``str | None`` so the absence
propagates through the type system.

The antipattern:

    result = some_func(agent.name or "", ...)

The fix:

    # ``some_func`` accepts ``str | None``:
    result = some_func(agent.name, ...)

    # or, less common — the callee makes its own decision:
    result = some_func(name=agent.name, name_fallback="<unnamed>")

Mechanical rule:
- Flag any ``BoolOp(op=Or, values=[<anything>, Constant(value="")])``
  where the final operand is an inline empty-string literal.
- This catches ``x or ""``, ``f() or ""``, ``d.get("k") or ""``,
  ``x if cond else ""`` is a separate pattern not covered here.

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_empty_string_or(node: ast.expr) -> bool:
    if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
        return False
    # An ``or`` chain short-circuits left-to-right; the antipattern sits
    # at the *final* operand position (the fallback). Only flag when the
    # final operand is a literal empty string.
    if not node.values:
        return False
    last = node.values[-1]
    return isinstance(last, ast.Constant) and last.value == ""


def scan(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    source_lines = path.read_text().splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr):
            continue
        if _is_empty_string_or(node):
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
                    f'{path}:{line}: `<expr> or ""` coerces `str | None` '
                    f"to `str` at the call site: {snippet}\n"
                )
    if failed:
        sys.stdout.write(
            '\n``<expr> or ""`` swallows the ``None`` instead of letting it '
            "propagate. The fix is to widen the receiving parameter to "
            "`str | None` (or the stored field to `str | None`), not to "
            "silently substitute an empty string. Empty string sentinels "
            "re-introduce the exact ambiguity the typing discipline is "
            "trying to eliminate.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
