from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ManifestError(Exception):
    """Raised when capabilities.yaml is malformed or invalid."""


VALID_TIERS = ("cheap", "live")
DEFAULT_FRESHNESS = {"cheap": "code", "live": "24h"}
REQUIRED_FIELDS = ("id", "description", "given", "when", "then", "tier", "check")


@dataclass
class Capability:
    id: str
    description: str
    given: str
    when: str
    then: str
    tier: str
    deps: list[str]
    freshness: str
    check_kind: str  # "pytest" | "shell"
    check_target: str
    warnings: list[str] = field(default_factory=list)


def _parse_check(raw: str | dict, cap_id: str) -> tuple[str, str]:
    if isinstance(raw, str):
        return "pytest", raw
    if isinstance(raw, dict) and list(raw.keys()) == ["shell"]:
        return "shell", str(raw["shell"])
    raise ManifestError(
        f"capability {cap_id!r}: 'check' must be a pytest node string "
        f"or a single-key mapping {{shell: ...}}"
    )


def load_manifest(path: str | Path) -> list[Capability]:
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"could not parse {path}: {e}") from e

    if "capabilities" not in doc:
        raise ManifestError("manifest must have a top-level 'capabilities:' list")
    entries = doc["capabilities"]
    if entries is None:
        entries = []  # `capabilities:` with no items (e.g. a freshly-init'd manifest) = zero
    if not isinstance(entries, list):
        raise ManifestError("manifest must have a top-level 'capabilities:' list")

    caps: list[Capability] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise ManifestError(f"each capability must be a mapping, got {raw!r}")
        missing = [f for f in REQUIRED_FIELDS if f not in raw]
        if missing:
            raise ManifestError(
                f"capability {raw.get('id', '<no id>')!r} missing fields: {missing}"
            )
        cid = str(raw["id"])
        if cid in seen:
            raise ManifestError(f"duplicate capability id: {cid!r}")
        seen.add(cid)

        tier = str(raw["tier"])
        if tier not in VALID_TIERS:
            raise ManifestError(
                f"capability {cid!r}: tier must be one of {VALID_TIERS}, got {tier!r}"
            )

        check_kind, check_target = _parse_check(raw["check"], cid)

        warnings: list[str] = []
        deps = raw.get("deps")
        if deps is None:
            deps = []
            warnings.append("deps not declared; code-freshness covers only the check file")
        elif not isinstance(deps, list):
            raise ManifestError(f"capability {cid!r}: deps must be a list of globs")
        deps = [str(d) for d in deps]

        freshness = str(raw.get("freshness", DEFAULT_FRESHNESS[tier]))

        caps.append(
            Capability(
                id=cid,
                description=str(raw["description"]),
                given=str(raw["given"]),
                when=str(raw["when"]),
                then=str(raw["then"]),
                tier=tier,
                deps=deps,
                freshness=freshness,
                check_kind=check_kind,
                check_target=check_target,
                warnings=warnings,
            )
        )
    return caps
