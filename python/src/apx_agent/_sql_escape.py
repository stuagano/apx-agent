"""Inline SQL string-literal escaping — the one canonical implementation.

Kept dependency-free (no ``databricks.sdk`` import) so every module that builds
inline SQL can share it without pulling the SDK into its import path. ``_sql``
re-exports these for back-compat.

Spark/Databricks SQL treats backslash as a live escape character inside a
single-quoted literal, so a value ending in ``\\`` would otherwise escape the
closing quote and break out of the literal (SQL injection). Escape backslash
FIRST, then single quotes — quote-only doubling is not sufficient. (#351/#352)
"""

from __future__ import annotations


def sql_escape(value: str) -> str:
    """Escape a string for inline SQL (no wrapping quotes)."""
    return value.replace("\\", "\\\\").replace("'", "''")


def sql_str_literal(value: str) -> str:
    """Render a Python string as a safe single-quoted SQL literal.

    Prefer bind parameters where the driver supports them; use this only when a
    value must be interpolated directly (e.g. the SQL Statements API where binds
    aren't available).
    """
    return "'" + sql_escape(value) + "'"
