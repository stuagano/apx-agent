"""Shared values the dev-UI route families close over."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DevRouteContext:
    api_prefix: str
    instruction_source_path: Path | None
    instruction_source: str | None
