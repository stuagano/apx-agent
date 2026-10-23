"""Bounded native domain input; schema and destination are server declarations."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from ._a2a_models import data_parts

NATIVE_ARTIFACTS_KEY = "apx_native_artifacts"


def validate_input_declaration(schema: type[BaseModel] | None, key: str | None) -> None:
    if schema is None and key is None:
        return
    if (not isinstance(schema, type) or not issubclass(schema, BaseModel)
            or schema.model_config.get("extra") != "forbid" or schema.model_json_schema().get("type") != "object"):
        raise ValueError("input_schema requires an object BaseModel with extra='forbid'")
    if (not isinstance(key, str) or re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", key) is None
            or key in {NATIVE_ARTIFACTS_KEY, "messages", "state", "auth", "session_id", "invocation_id", "principal", "headers", "session_tokens", "session_usd"}):
        raise ValueError("input_key must be a non-reserved domain state slot")


def _bounded_json(value: Any) -> str:
    def check(item: Any, depth: int = 0) -> None:
        if depth > 16 or isinstance(item, (dict, list)) and len(item) > 256:
            raise ValueError("Native domain input exceeds depth or field limits")
        if isinstance(item, dict):
            for child in item.values():
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)

    check(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise ValueError("Native domain input exceeds 64 KiB")
    return encoded


def native_data_part(schema: type[BaseModel], payload: Any, part: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate the declared JSON object and preserve verified artifact metadata."""
    encoded = _bounded_json(payload)
    try:
        schema.model_validate_json(encoded, strict=True)
    except Exception:
        raise ValueError("Native domain input violates its declared schema") from None
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if part is None:
        part = {"kind": "data", "data": payload,
                "metadata": {"artifact_id": digest, "sha256": digest}}
    _bounded_json(part)
    if set(part) != {"kind", "data", "metadata"}:
        raise ValueError("Native input requires one data artifact with integrity metadata")
    validated = data_parts({"apx_data_parts": [part]})[0]
    metadata = validated.get("metadata")
    if (not isinstance(metadata, dict) or set(metadata) != {"artifact_id", "sha256"}
            or not isinstance(metadata["artifact_id"], str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", metadata["artifact_id"]) is None
            or metadata["sha256"] != digest or _bounded_json(validated["data"]) != encoded):
        raise ValueError("Native input artifact integrity mismatch")
    return deepcopy(part)


def input_leaf(agent: Any) -> Any:
    from ._agents import SequentialAgent

    while isinstance(agent, SequentialAgent):
        agent = agent._agents[0]
    return agent
