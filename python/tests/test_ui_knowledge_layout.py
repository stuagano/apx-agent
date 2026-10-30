"""The Knowledge page leads with the glossary and keeps tables compact and searchable."""
from __future__ import annotations

from apx_agent._ui_knowledge import _render_knowledge_html

VIEWS = {
    "peer_rates": {
        "description": "Peer rates for `FIRST_NAT`.",
        "columns": [{"name": "rate_bps", "type": "int", "description": "Rate in bps, e.g. `480` <b>x</b>"}],
    }
}
GLOSSARY = [{"term": "Peer median", "definition": "Median of peer rates.", "synonyms": ["median"]}]


def test_glossary_comes_before_tables():
    page = _render_knowledge_html([], VIEWS, GLOSSARY)
    assert page.index(">Glossary<") < page.index(">Tables<")
    assert ">Views<" not in page


def test_each_table_is_one_collapsed_row():
    page = _render_knowledge_html([], VIEWS, GLOSSARY)
    assert '<details class="view-card"' in page and "<details open" not in page
    assert "1 column" in page


def test_inline_code_renders_and_markup_stays_escaped():
    page = _render_knowledge_html([], VIEWS, GLOSSARY)
    assert "<code>FIRST_NAT</code>" in page and "<code>480</code>" in page
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


def test_page_has_a_filter():
    page = _render_knowledge_html([], VIEWS, GLOSSARY)
    assert 'id="knowledge-filter"' in page
