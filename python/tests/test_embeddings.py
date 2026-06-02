"""Tests for make_embedding_fn.

Response-shape note: Databricks embeddings endpoints return OpenAI-style
``response.data``: a list of EmbeddingsV1ResponseEmbeddingElement objects
(each with ``.embedding: list[float]`` and ``.index: int``).
NOT ``.predictions`` (which is for custom ML serving endpoints).
The query call uses ``input=list[str]`` (not ``inputs``).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _mock_embedding_element(index: int, dims: int) -> Any:
    """Build a mock EmbeddingsV1ResponseEmbeddingElement."""
    elem = MagicMock()
    elem.embedding = [float(i) for i in range(dims)]
    elem.index = index
    return elem


def _mock_ws_with_embeddings(dims: int = 4) -> Any:
    """Return a mock WorkspaceClient whose serving_endpoints.query returns
    OpenAI-style data (response.data[i].embedding), matching the real SDK."""
    ws = MagicMock()

    def _query(**kw: Any) -> Any:
        texts = kw.get("input", []) or []
        if isinstance(texts, str):
            texts = [texts]
        response = MagicMock()
        response.data = [_mock_embedding_element(i, dims) for i in range(len(texts))]
        return response

    ws.serving_endpoints.query.side_effect = _query
    return ws


class TestMakeEmbeddingFn:
    def test_returns_callable(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "databricks-bge-large-en")
        assert callable(fn)

    def test_returns_list_of_lists(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings(dims=4)
        fn = make_embedding_fn(ws, "databricks-bge-large-en")
        result = fn(["hello", "world"])
        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(row, list) for row in result)
        assert all(isinstance(v, float) for v in result[0])

    def test_single_text_works(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings(dims=3)
        fn = make_embedding_fn(ws, "bge-large")
        assert len(fn(["only one"])) == 1

    def test_calls_serving_endpoints_query(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "my-embed-model")
        fn(["test"])
        ws.serving_endpoints.query.assert_called_once()
        assert ws.serving_endpoints.query.call_args.kwargs.get("name") == "my-embed-model"

    def test_uses_input_kwarg_not_inputs(self):
        """Embeddings endpoints require 'input=', not 'inputs=' (tensor endpoint param)."""
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "my-embed-model")
        fn(["test"])
        call_kwargs = ws.serving_endpoints.query.call_args.kwargs
        assert "input" in call_kwargs, "should use 'input' kwarg for embeddings endpoints"
        assert "inputs" not in call_kwargs, "'inputs' is for tensor endpoints, not embeddings"

    def test_empty_list_returns_empty(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "model")
        assert fn([]) == []

    def test_satisfies_embedding_fn_type(self):
        from apx_agent._embeddings import make_embedding_fn

        ws = _mock_ws_with_embeddings()
        fn = make_embedding_fn(ws, "m")
        result = fn(["a", "b"])
        assert isinstance(result, list)
        assert isinstance(result[0], list)
        assert isinstance(result[0][0], float)

    def test_degrades_gracefully_on_error(self):
        """A failing endpoint returns zero vectors rather than raising."""
        from apx_agent._embeddings import make_embedding_fn

        ws = MagicMock()
        ws.serving_endpoints.query.side_effect = RuntimeError("connection refused")
        fn = make_embedding_fn(ws, "broken-endpoint")
        result = fn(["a", "b"])
        assert len(result) == 2
        assert result[0] == [0.0]
        assert result[1] == [0.0]

    def test_reorders_by_index_when_response_out_of_order(self):
        """Embeddings endpoints don't guarantee response order; the fn must
        sort response.data by .index to match input order."""
        from apx_agent._embeddings import make_embedding_fn

        # Response comes back as index 2, 0, 1 — output must be sorted to 0,1,2.
        elems = [
            MagicMock(index=2, embedding=[2.0]),
            MagicMock(index=0, embedding=[0.0]),
            MagicMock(index=1, embedding=[1.0]),
        ]
        ws = MagicMock()
        ws.serving_endpoints.query.return_value = MagicMock(data=elems)
        fn = make_embedding_fn(ws, "m")
        result = fn(["a", "b", "c"])
        assert result == [[0.0], [1.0], [2.0]]  # reordered by index, not response order
