"""Flag ``<dict>.get(<key>, "")`` — empty-string defaults on ``dict.get``.

The empty-string default on ``dict.get`` overloads the return value:
``""`` now means both "the key was missing" and "the stored value was
the empty string." Callers then write truthiness checks that silently
collapse those cases, hiding bugs where a legitimate empty value is
treated as absence (or vice versa).

Prefer one of:

- ``d.get(key)`` + an ``is None`` check (fail loud or branch explicitly).
- ``d.get(key, SENTINEL)`` using a named module-level constant whose
  intent is documented at the definition site.
- ``d[key]`` when the key is required (raises KeyError on missing).

Mechanical rule:
- Flag ``<expr>.get(<key>, "")`` and ``<expr>.get(<key>, '')`` on any
  call to an attribute named ``get`` with exactly two positional args
  where the second is an empty-string literal.
- This will occasionally false-positive on non-dict ``.get`` calls
  (e.g. a custom class whose ``get`` accepts a default) — those are
  rare in this codebase, and when they come up the fix is the same
  shape (hoist the default to a named constant).

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_dict_get_with_empty_string_default(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return False
    if len(node.args) != 2:
        return False
    default = node.args[1]
    return isinstance(default, ast.Constant) and default.value == ""


def scan(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    source_lines = path.read_text().splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_dict_get_with_empty_string_default(node):
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
                    f"{path}:{line}: `.get(key, \"\")` conflates 'missing' "
                    f"with 'empty value': {snippet}\n"
                )
    if failed:
        sys.stdout.write(
            "\nEmpty-string defaults on `.get()` make absence and the empty "
            "string indistinguishable. Prefer `.get(key)` + explicit None "
            "handling, `d[key]` when required, or a named sentinel constant.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
