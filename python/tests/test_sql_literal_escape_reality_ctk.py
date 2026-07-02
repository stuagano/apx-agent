"""Claim-vs-reality checks on SQL single-quoted literal escaping.

The escapers render Python strings as inline Spark/Databricks SQL literals
(``sql_str_literal`` and its callers in ``catalog._to_sql_literal``,
``_publish._q``, ``_conversation_delta._sql_str``). Spark treats backslash as a
live escape character *inside* a single-quoted literal, so an escaper that only
doubles the quote (``'`` -> ``''``) but leaves ``\\`` untouched lets a value
ending in a backslash escape the closing quote and break out of the literal —
SQL injection under the caller's UC grants (issues #351, #352).

A string-only "did it return a quoted value" assertion passes on the buggy
escaper. This reconciles the *claim* (this is a safe literal) against *reality*:
each adversarial value is escaped, then decoded with a Spark-literal decoder;
the decoded value must equal the original, and the literal must not terminate
early. The pre-fix escapers fail here; the backslash-aware ones pass.
"""

from __future__ import annotations

import pytest

from apx_agent._conversation_delta import _sql_str
from apx_agent._publish import _q
from apx_agent._sql import sql_str_literal
from apx_agent.catalog import _to_sql_literal

# Adversarial values: trailing backslash (the breakout vector), embedded quote,
# backslash+quote combos, newline/tab (which json.dumps emits), and a payload
# that would inject a subquery if the closing quote were escaped away.
_ADVERSARIAL = [
    "x\\",
    "a'b",
    "a'b\\",
    "ends with backslash\\",
    "\\",
    "\\'",
    "line1\nline2",
    "tab\there",
    "', (SELECT secret FROM other.schema.tbl)) --",
    'json {"k": "v\\n", "q": "a\\""}',
    "plain",
]


def _spark_decode(literal: str) -> str:
    """Decode a single-quoted Spark SQL literal back to its Python string.

    Models the two escape mechanisms Spark honours inside ``'...'``: a
    backslash escape (``\\\\`` -> ``\\``, ``\\'`` -> ``'``, ``\\n`` -> newline,
    etc.) and a doubled quote (``''`` -> ``'``). A bare ``'`` that is neither
    doubled nor backslash-escaped means the literal terminated early — i.e. the
    escaper let the value break out — which we surface as an assertion failure.
    """
    assert literal[0] == "'" and literal[-1] == "'", f"not a quoted literal: {literal!r}"
    body = literal[1:-1]
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            assert i + 1 < n, f"dangling backslash escapes closing quote: {literal!r}"
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}.get(nxt, nxt))
            i += 2
        elif c == "'":
            assert i + 1 < n and body[i + 1] == "'", f"unescaped quote = breakout: {literal!r}"
            out.append("'")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


@pytest.mark.unit
@pytest.mark.parametrize("value", _ADVERSARIAL)
@pytest.mark.parametrize(
    "escaper",
    [
        pytest.param(sql_str_literal, id="sql_str_literal"),
        pytest.param(lambda v: _to_sql_literal(v, "STRING"), id="catalog._to_sql_literal"),
        pytest.param(_q, id="_publish._q"),
        pytest.param(_sql_str, id="_conversation_delta._sql_str"),
    ],
)
def test_escaped_literal_round_trips_without_breakout(escaper, value: str) -> None:
    literal = escaper(value)
    decoded = _spark_decode(literal)
    assert decoded == value, (
        f"escaper {escaper} corrupted {value!r} -> literal {literal!r} -> decoded {decoded!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1_000", "0x10"])
def test_numeric_branch_does_not_leak_non_numeric_strings_unquoted(bad: str) -> None:
    """catalog._to_sql_literal's numeric branch must not emit float()-accepted
    junk (``nan``/``inf``) or Python underscore/hex forms as raw, unquoted SQL —
    those are invalid or dangerous tokens. They must fall back to a quoted
    literal (a clean type error at UC beats an unquoted token in the statement).
    """
    literal = _to_sql_literal(bad, "INT")
    assert literal.startswith("'") and literal.endswith("'"), f"leaked unquoted: {literal!r}"
