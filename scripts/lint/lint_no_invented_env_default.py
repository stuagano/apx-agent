"""Flag ``os.environ.get("VAR", <invented-default>)`` calls.

An invented default for an environment variable masks missing
configuration and produces silent, hard-to-debug failures in
production: the code quietly operates on an empty string or a
placeholder like ``"unknown"`` / ``"localhost"`` instead of failing
loud with a clear error message.

Required env vars should use::

    value = os.environ.get("VAR")
    if value is None:
        raise RuntimeError("VAR must be set")

Optional env vars are fine — but the fallback must be a documented,
semantically meaningful value (e.g. ``"INFO"`` for a log level
following the stdlib docs). If you have a genuine default, wrap
the fallback in a named constant declared at module top *with a
comment citing the source*:

    DEFAULT_LOG_LEVEL = "INFO"  # stdlib default per logging.getLevelName

Mechanical rule:
- Flag ``os.environ.get(name, default)`` where ``default`` is an
  inline string literal.
- Also flag ``os.getenv("VAR", default)`` with the same shape.
- Don't flag ``os.environ.get("VAR")`` (no default) — that's the
  fail-loud path.
- Don't flag calls whose ``default`` is a ``Name`` (named constant)
  — those have presumably been reviewed at the constant's site.

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_environ_get(node: ast.Call) -> bool:
    """Match `os.environ.get(...)` and `os.getenv(...)`."""
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "get"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "environ"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "os"
    ):
        return True
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "getenv"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        return True
    return False


def _default_arg(node: ast.Call) -> ast.expr | None:
    """Return the 2nd positional arg (the default), or None if absent."""
    if len(node.args) >= 2:
        return node.args[1]
    return None


def scan(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    source_lines = path.read_text().splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_environ_get(node):
            continue
        default = _default_arg(node)
        if default is None:
            continue
        if isinstance(default, ast.Constant) and isinstance(default.value, str):
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
                    f"{path}:{line}: `os.environ.get(..., <inline-string>)` "
                    f"masks missing config: {snippet}\n"
                )
    if failed:
        sys.stdout.write(
            "\nInline string defaults for env vars silently mask missing "
            "configuration. Either: (a) drop the default and fail loud "
            "when the value is required, or (b) move the default to a "
            "named module-level constant WITH a comment citing the "
            "documented source of that value.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
