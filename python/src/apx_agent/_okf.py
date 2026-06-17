"""Vendored OKF v0.1 (Draft) reader/writer — apx's grounding substrate.

Mirrors the OKF reference implementation (GoogleCloudPlatform/knowledge-catalog
/okf, Apache-2.0) ``OKFDocument.parse/serialize/validate`` and adds the
``# Schema`` pipe-table -> ``"col(type)"`` parser the reference lacks. Pinned to
OKF SPEC v0.1 §4. Re-check on ``okf_version`` bumps.

Totality contract: every reader here returns ``None``/``[]`` on bad input and
NEVER raises out to callers (mirrors ``load_baked_schema``'s None-on-error). The
only function that raises is ``validate()``, which is EMIT-side only and MUST NOT
be called on the read path (spec §3, F5).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

REQUIRED_FRONTMATTER_KEYS = ("type", "title", "description", "timestamp")
OKF_VERSION = "0.1"

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


@dataclass
class OKFDocument:
    frontmatter: dict
    body: str

    @classmethod
    def parse(cls, text: str) -> "OKFDocument":
        m = _FM_RE.match(text)
        if m:
            import yaml

            fm = yaml.safe_load(m.group(1)) or {}
            return cls(
                frontmatter=fm if isinstance(fm, dict) else {},
                body=m.group(2).lstrip("\n"),
            )
        return cls(frontmatter={}, body=text)

    def serialize(self) -> str:
        import yaml

        fm = yaml.safe_dump(self.frontmatter, sort_keys=False).strip()
        return f"---\n{fm}\n---\n\n{self.body}"

    def validate(self) -> None:
        """Emit-side conformance gate. NEVER call on the read path (F5)."""
        for k in REQUIRED_FRONTMATTER_KEYS:
            if not self.frontmatter.get(k):
                raise ValueError(f"OKF concept missing required frontmatter key: {k!r}")
