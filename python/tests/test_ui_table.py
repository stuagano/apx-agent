"""The shared tool-output table renderer.

One implementation feeds three surfaces: the Tools console, the Chat page
(detail panel + inline steps + trace replay), and the server-rendered trace
detail page. These tests pin the shape rules the JS half also follows, and
prove the chat/trace pages actually ship (and use) the shared renderer.
"""

from __future__ import annotations

from apx_agent._ui_table import TABLE_CSS, TABLE_JS, render_tool_output_html


REVISIONS = {
    "tenant_filter": "(all tenants)",
    "entity_filter": "(all entities)",
    "count": 2,
    "source": "live",
    "revisions": [
        {"revision_id": "aps-agreement-r5", "tenant": "APS", "status": "approved"},
        {"revision_id": "honi-customer-r2", "tenant": "HONI", "status": "draft"},
    ],
}


class TestPythonTableRenderer:
    def test_list_of_objects_becomes_a_table(self):
        html = render_tool_output_html(REVISIONS["revisions"])
        assert html is not None
        assert 'table class="out"' in html
        assert "<th>revision_id</th>" in html
        assert "<td>aps-agreement-r5</td>" in html
        assert "2 rows" in html

    def test_wrapper_object_picks_the_largest_array_and_chips_scalars(self):
        html = render_tool_output_html(REVISIONS)
        assert html is not None
        assert "rows: revisions" in html
        assert "count = 2" in html and "source = live" in html
        assert "Raw JSON" in html
        assert 'details class="out-raw"' in html

    def test_largest_array_wins_when_several_are_tabular(self):
        payload = {
            "fields": [{"target_field": "a"}],
            "source_files": [{"name": "one.csv"}, {"name": "two.csv"}],
        }
        html = render_tool_output_html(payload)
        assert html is not None
        assert "rows: source_files" in html
        assert "other arrays: fields" in html

    def test_non_tabular_returns_none(self):
        # A string array (e.g. known_revisions) is not tabular: the caller
        # keeps its pretty-JSON fallback.
        assert render_tool_output_html({"error": "x", "known_revisions": ["a", "b"]}) is None
        assert render_tool_output_html("not json") is None
        assert render_tool_output_html({}) is None
        assert render_tool_output_html([]) is None
        assert render_tool_output_html({"a": 1, "b": {"c": 2}}) is None

    def test_nested_values_and_nulls_render_safely(self):
        html = render_tool_output_html(
            {"fields": [{"target_field": "x", "source_columns": ["a", "b"], "nullable": True, "verdict": None}]}
        )
        assert html is not None
        assert "a, b" in html
        assert "<td>true</td>" in html
        assert '<span class="muted">—</span>' in html

    def test_values_are_html_escaped(self):
        html = render_tool_output_html({"fields": [{"target_field": "<script>alert(1)</script>"}]})
        assert html is not None
        assert "&lt;script&gt;" in html
        assert "<script>alert" not in html

    def test_json_string_wrapper_is_unwrapped(self):
        import json as _json

        for key in ("output", "result"):
            html = render_tool_output_html({key: _json.dumps(REVISIONS)})
            assert html is not None, key
            assert "rows: revisions" in html

    def test_raw_json_string_input_is_accepted(self):
        import json as _json

        html = render_tool_output_html(_json.dumps(REVISIONS))
        assert html is not None
        assert 'table class="out"' in html

    def test_caps_rows_and_columns(self):
        wide = {"fields": [{}]}
        for i in range(20):
            wide["fields"][0][f"col{i}"] = i
        html = render_tool_output_html(wide)
        assert html is not None
        assert "+6 more column(s) not shown" in html

        long = {"fields": [{"i": i} for i in range(205)]}
        html = render_tool_output_html(long)
        assert html is not None
        assert "+5 more not shown" in html


class TestSharedAssets:
    def test_session_assets_are_one_implementation(self):
        # The JS is shared verbatim: the escaping helper stays page-provided so
        # the two pages cannot drift into two renderers.
        for symbol in ("jsonPreview", "tableShape", "scalarMeta", "renderTable", "renderToolOutput"):
            assert f"function {symbol}" in TABLE_JS
        assert "function esc(" not in TABLE_JS
        assert 'details class="out-raw"' in TABLE_JS
        assert "table.out" in TABLE_CSS
        assert ".outmeta .mchip" in TABLE_CSS


class TestPageIntegrations:
    """Each surface must ship the shared renderer and route tool JSON to it."""

    def test_chat_page_ships_shared_renderer_and_uses_it(self):
        from apx_agent._ui_chat import _render_agent_ui

        html = _render_agent_ui(None)
        # Shared assets are substituted (no leftover template markers).
        assert "{APX_TABLE_JS}" not in html and "{APX_TABLE_CSS}" not in html
        assert "function renderToolOutput" in html
        assert "function tableShape" in html
        assert "table.out" in html
        # Chat detail / inline steps / trace replay all go through fmtResp.
        assert "if (tableShape(obj)) return renderToolOutput(rawStr);" in html
        # The warehouse SQL viewer still wins for _sql/data payloads.
        assert "if (obj._sql || Array.isArray(obj.data))" in html

    def test_tools_page_ships_shared_renderer(self):
        from apx_agent._ui_tools import _render_tools_ui

        html = _render_tools_ui()
        assert "{APX_TABLE_JS}" not in html and "{APX_TABLE_CSS}" not in html
        assert "function renderToolOutput" in html
        assert "table.out" in html

    def test_trace_detail_renders_tabular_tool_output_as_table(self):
        from apx_agent._dev import _render_trace_detail

        spans = [{
            "span_id": "s1", "parent_id": None, "name": "list_mapping_revisions",
            "span_type": "TOOL", "status": "OK",
            "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
            "inputs": {}, "outputs": REVISIONS, "events": [],
        }]
        html = _render_trace_detail("tr-1", spans, None)
        assert 'table class="out"' in html
        assert "rows: revisions" in html
        assert "source = live" in html          # scalar chips
        assert "Raw JSON" in html

    def test_trace_detail_keeps_pre_fallback_for_non_tabular(self):
        from apx_agent._dev import _render_trace_detail

        spans = [{
            "span_id": "s1", "parent_id": None, "name": "get_field_registry",
            "span_type": "TOOL", "status": "OK",
            "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
            "inputs": {},
            "outputs": {"error": "Revision 'x' not found.", "known_revisions": ["a", "b"]},
            "events": [],
        }]
        html = _render_trace_detail("tr-2", spans, None)
        assert 'table class="out"' not in html
        assert 'io-pre' in html

    def test_trace_detail_leaves_agent_state_dumps_as_raw_json(self):
        """Only TOOL responses table. A LangGraph state update is a framework
        payload, not a tool result; its shape stays visible as JSON."""
        from apx_agent._dev import _render_trace_detail

        spans = [{
            "span_id": "s1", "parent_id": None, "name": "LangGraph",
            "span_type": "CHAIN", "status": "OK",
            "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
            "inputs": None,
            "outputs": {"graph": None, "resume": None, "goto": None,
                        "update": [{"graph": None, "update": None, "resume": None, "goto": None}]},
            "events": [],
        }]
        html = _render_trace_detail("tr-3", spans, None)
        assert 'table class="out"' not in html
        assert 'io-pre' in html
