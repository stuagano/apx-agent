"""Guard: the dev-UI information_schema queries bind the schema, not interpolate it.

The tool-schema and tool-suggest dev-UI routes filtered `information_schema.columns`
with `WHERE table_schema = '{schema}'` — raw interpolation of a value (a
config-sourced CATALOG/SCHEMA in a dev-only route, so not an external injection
vector, but a latent robustness/quoting hazard). They now bind the schema as a
statement parameter (`:schema`) instead.

This guards against the raw interpolation returning.
"""

from __future__ import annotations

import pathlib

import apx_agent._dev as _dev


def _src() -> str:
    return pathlib.Path(_dev.__file__).read_text()


def test_no_raw_schema_interpolation_into_information_schema_query() -> None:
    src = _src()
    assert "table_schema = '{" not in src, (
        "dev-UI still interpolates the schema raw into an information_schema query"
    )


def test_schema_is_bound_as_a_statement_parameter() -> None:
    src = _src()
    assert "table_schema = :schema" in src
    assert "StatementParameterListItem(name=\"schema\"" in src
