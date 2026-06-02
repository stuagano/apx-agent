"""Embedding function builder for config-declared memory backends (E3b).

Databricks **embeddings** serving endpoints (external/foundation model type)
return an OpenAI-compatible response: ``response.data`` is a list of
``EmbeddingsV1ResponseEmbeddingElement`` objects, each with
``.embedding: list[float]`` and ``.index: int``.  This differs from custom
ML serving endpoints, which use ``response.predictions``.

The query call uses ``input=list[str]`` (the embeddings-specific kwarg); the
``inputs`` kwarg is for tensor-based serving endpoints and must not be used
here.

This module bridges ``embedding_model`` (a string endpoint name) →
``EmbeddingFn`` (a batched callable).  No built-in embedder otherwise exists
in apx-agent.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._memory import EmbeddingFn

logger = logging.getLogger(__name__)


def make_embedding_fn(ws: Any, endpoint_name: str) -> "EmbeddingFn":
    """Return a batched embedding function calling a Databricks serving endpoint.

    Satisfies ``EmbeddingFn = Callable[[Sequence[str]], list[list[float]]]``.

    The returned callable sends ``input=list[str]`` to the named endpoint and
    extracts ``response.data[i].embedding`` (OpenAI-style, as returned by
    Databricks external/foundation-model embeddings endpoints).

    On failure (network error, misconfigured endpoint, unexpected response
    shape) the function logs a warning and returns zero-length vectors so that
    the calling agent degrades gracefully rather than crashing.  Task 1.4
    (validate_at_boot) will surface configuration errors earlier.
    """

    def _embed(texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = ws.serving_endpoints.query(
                name=endpoint_name,
                input=list(texts),  # 'input' is the embeddings-specific kwarg
            )
        except Exception as exc:
            logger.error(
                "Embedding call to %r failed: %s — returning zero vectors.",
                endpoint_name,
                exc,
            )
            return [[0.0] for _ in texts]

        # Real embeddings response: response.data is list[EmbeddingsV1ResponseEmbeddingElement]
        # Each element has .embedding: list[float] and .index: int.
        data = getattr(response, "data", None)
        if not data:
            logger.warning(
                "Embedding response from %r has no data — returning zero vectors.",
                endpoint_name,
            )
            return [[0.0] for _ in texts]

        # Sort by index to guarantee input order is preserved
        try:
            ordered = sorted(data, key=lambda e: getattr(e, "index", 0))
        except Exception:
            ordered = data

        return [list(map(float, getattr(elem, "embedding", []))) for elem in ordered]

    _embed.__name__ = f"embed_{endpoint_name}"
    return _embed
