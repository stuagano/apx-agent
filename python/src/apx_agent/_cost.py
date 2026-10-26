"""USD estimates from token usage, for the session budget guard and the dev UI.

``usd_for_token_usage`` prices input/output tokens with published per-million-token
rates for known foundation models, and returns ``None`` for an unknown model.
"""

from __future__ import annotations

# Published FMAPI list prices used by the traces panel and session usd caps (#836).
# Values are USD per million tokens (input, output).
_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "databricks-claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}


def usd_for_token_usage(input_tokens: int, output_tokens: int, *, model: str | None) -> float | None:
    """Dollar cost of one usage blob, or ``None`` when the model has no published rate."""
    if model is None or not str(model).strip():
        return None
    key = str(model).strip()
    if key.startswith("databricks:/"):
        key = key[len("databricks:/"):]
    rates = _USD_PER_MTOK.get(key)
    if rates is None and key.startswith("databricks-"):
        rates = _USD_PER_MTOK.get(key[len("databricks-"):])
    if rates is None:
        return None
    inp, out = rates
    return (input_tokens * inp + output_tokens * out) / 1_000_000
