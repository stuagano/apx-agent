"""Consistency guard: every inline-SQL escaper routes through one canonical impl.

#351/#352 were caused by multiple divergent copies of the SQL-literal escaper,
some of which forgot to escape backslashes. The correct copies have now been
consolidated onto the dependency-free `_sql_escape.{sql_escape,sql_str_literal}`.
This guards against them drifting apart again: each site's helper must produce
exactly what the canonical produces (respecting its wrapped/unwrapped/None
contract), and the canonical must still escape backslashes.
"""

from __future__ import annotations

import pytest

from apx_agent._sql_escape import sql_escape, sql_str_literal


@pytest.mark.unit
@pytest.mark.parametrize("value", ["x\\", "a'b", "a'b\\", "\\'", "plain", "', DROP--"])
def test_all_site_helpers_match_the_canonical(value: str) -> None:
    from apx_agent._trace_export import _escape_sql
    from apx_agent.uc_comment import _esc_literal
    from apx_agent.workflow.engine_delta import _esc
    from apx_agent.workflow.population_store import _esc_sql_str

    # Wrapped (single-quoted literal) helpers.
    assert _escape_sql(value) == sql_str_literal(value)
    # Unwrapped (escape-only) helpers.
    assert _esc_literal(value) == sql_escape(value)
    assert _esc(value) == sql_escape(value)
    assert _esc_sql_str(value) == sql_escape(value)


@pytest.mark.unit
def test_none_and_backslash_contracts() -> None:
    from apx_agent._trace_export import _escape_sql

    assert _escape_sql(None) == "NULL"  # None → NULL preserved
    assert sql_escape("a\\") == "a\\\\"  # backslash actually doubled
    assert sql_str_literal("a\\") == "'a\\\\'"
