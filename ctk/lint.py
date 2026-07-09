"""
Static scan for swallowed exceptions.

The "swallowed exception" failure mode is hard to catch at runtime because the
whole point is that the error never surfaces. So we catch it statically: parse
the source with `ast` and flag `except` blocks that hide errors.

Flagged patterns:
  * `except: pass`               (bare except that does nothing)
  * `except Exception: pass`     (broad except that does nothing)
  * an except body that only logs/prints and never re-raises

A test can assert your codebase has none:

    from ctk import find_swallowed_exceptions
    assert find_swallowed_exceptions("my_pkg/") == []
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass


@dataclass
class SwallowedExcept:
    file: str
    line: int
    kind: str  # "bare-pass" | "broad-pass" | "log-only"
    snippet: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}  [{self.kind}]  {self.snippet}"


_LOGGING_CALLS = {
    "print",
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "exception",
    "critical",
    "log",
}


def _body_is_only_pass(body: list[ast.stmt]) -> bool:
    return len(body) == 1 and isinstance(body[0], ast.Pass)


def _stmt_is_logging_only(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    fn = call.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    return name in _LOGGING_CALLS


def _body_is_log_only(body: list[ast.stmt]) -> bool:
    """Body only logs/prints (and maybe pass) and never raises/returns/continues."""
    if not body:
        return False
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if _stmt_is_logging_only(stmt):
            continue
        return False  # something more substantial happens — not "swallowed"
    # at least one statement, all logging/pass, and no raise anywhere
    return any(not isinstance(s, ast.Pass) for s in body)


def _has_reraise(body: list[ast.stmt]) -> bool:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
    return False


def _scan_source(source: str, filename: str) -> list[SwallowedExcept]:
    found: list[SwallowedExcept] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found
    lines = source.splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        is_bare = node.type is None
        is_broad = isinstance(node.type, ast.Name) and node.type.id in {
            "Exception",
            "BaseException",
        }
        snippet = lines[node.lineno - 1].strip() if 0 <= node.lineno - 1 < len(lines) else ""

        if _body_is_only_pass(node.body):
            kind = "bare-pass" if is_bare else ("broad-pass" if is_broad else "narrow-pass")
            # narrow `except KeyError: pass` is often legitimate; only flag bare/broad.
            if is_bare or is_broad:
                found.append(SwallowedExcept(filename, node.lineno, kind, snippet))
        elif (is_bare or is_broad) and not _has_reraise(node.body) and _body_is_log_only(node.body):
            found.append(SwallowedExcept(filename, node.lineno, "log-only", snippet))

    return found


def find_swallowed_exceptions(path: str) -> list[SwallowedExcept]:
    """
    Scan a file or directory tree of .py files for swallowed exceptions.
    Returns a list (empty == clean). Skips common noise dirs.
    """
    targets: list[str] = []
    if os.path.isfile(path):
        targets = [path]
    else:
        skip = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in skip]
            for fn in files:
                if fn.endswith(".py"):
                    targets.append(os.path.join(root, fn))

    results: list[SwallowedExcept] = []
    for fp in targets:
        try:
            with open(fp, errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        results.extend(_scan_source(src, fp))
    return results
