"""Flag ``foo: str = ""`` and ``foo: str = ''`` — empty-string defaults.

Empty string is almost always a bad default because it overloads the
type: ``""`` now means both "the empty string" and "the user didn't
pass anything." Downstream code has to re-check truthiness everywhere
to distinguish the two, and bugs slip in when one path treats ``""``
as a legitimate value and another treats it as "missing."

Use one of these instead:

- ``foo: str | None = None``  — when "not set" is a real state
- ``foo: str = <named constant>`` — when there's a real default whose
  provenance you can cite (e.g. the empty URL path for a REST root)
- required positional / keyword argument — when the caller must decide

Mechanical rule:
- Flag any function / method default where the annotation is a plain
  ``str`` (or stringified ``"str"``) and the default is an inline
  empty string literal.
- Flag ``@dataclass`` fields with the same shape.
- Do NOT flag ``str | None = ""`` or ``Optional[str] = ""`` (the
  intent there is odd but at least explicit — a separate lint could
  catch that later).
- Do NOT flag ``Literal[""]`` defaults — the empty string *is* the
  type.

Exit code 1 if any hits are found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _annotation_is_plain_str(annotation: ast.expr | None) -> bool:
    """Return True if the annotation is ``str`` or ``"str"`` — plain str only."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name) and annotation.id == "str":
        return True
    if isinstance(annotation, ast.Constant) and annotation.value == "str":
        return True
    return False


def _is_empty_string_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == ""


def _scan_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """Flag ``arg: str = ""`` defaults in a function signature."""
    hits: list[tuple[int, str]] = []
    args = func.args
    pos_args = args.args
    pos_defaults = args.defaults
    if pos_defaults:
        tail = pos_args[-len(pos_defaults):]
        for arg, default in zip(tail, pos_defaults, strict=True):
            if _annotation_is_plain_str(arg.annotation) and _is_empty_string_literal(default):
                hits.append((default.lineno, arg.arg))
    for arg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if not isinstance(kw_default, ast.expr):
            continue
        if _annotation_is_plain_str(arg.annotation) and _is_empty_string_literal(kw_default):
            hits.append((kw_default.lineno, arg.arg))
    return hits


def _is_dataclass_decorated(node: ast.ClassDef) -> bool:
    for deco in node.decorator_list:
        if isinstance(deco, ast.Name) and deco.id == "dataclass":
            return True
        if (
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Name)
            and deco.func.id == "dataclass"
        ):
            return True
        if isinstance(deco, ast.Attribute) and deco.attr == "dataclass":
            return True
        if (
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Attribute)
            and deco.func.attr == "dataclass"
        ):
            return True
    return False


def _scan_dataclass(node: ast.ClassDef) -> list[tuple[int, str]]:
    """Flag ``x: str = ""`` fields on a @dataclass."""
    hits: list[tuple[int, str]] = []
    for item in node.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        if _annotation_is_plain_str(item.annotation) and _is_empty_string_literal(item.value):
            hits.append((item.lineno, item.target.id))
    return hits


def scan(path: Path) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []
    source_lines = path.read_text().splitlines()
    hits: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line, name in _scan_function(node):
                snippet = source_lines[line - 1].strip() if 0 < line <= len(source_lines) else ""
                hits.append((line, name, snippet))
        elif isinstance(node, ast.ClassDef) and _is_dataclass_decorated(node):
            for line, name in _scan_dataclass(node):
                snippet = source_lines[line - 1].strip() if 0 < line <= len(source_lines) else ""
                hits.append((line, name, snippet))
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
            for line, name, snippet in hits:
                sys.stdout.write(
                    f'{path}:{line}: `{name}: str = ""` default overloads '
                    f"'not set' and 'empty string': {snippet}\n"
                )
    if failed:
        sys.stdout.write(
            "\nEmpty-string defaults on `str` parameters and @dataclass fields "
            "make 'unset' and 'empty value' indistinguishable. Use "
            "`str | None = None` when unset is a real state, a named "
            "module-level constant when there's a real default, or make the "
            "argument required.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
