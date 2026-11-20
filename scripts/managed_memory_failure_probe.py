"""Read-only #856 failure probe; never creates stores, entries, or grants.

Run with ``uv run --project python --frozen python scripts/managed_memory_failure_probe.py
--scenario denied --profile <restricted-profile> --store <existing-store>``.
Repeat with ``--scenario unsupported`` and a workspace without managed memory.
A missing store, successful read, or unrelated failure is NOT passing evidence.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from importlib.metadata import version
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks_agentbricks.errors import AgentCliError

from apx_agent._memory_managed import ManagedMemoryStore

EXPECTED_CODES = {
    "denied": {"PERMISSION_DENIED"},
    "unsupported": {"FEATURE_DISABLED", "NOT_IMPLEMENTED", "UNIMPLEMENTED"},
}


def probe(ws: WorkspaceClient, *, store_name: str, scenario: str) -> dict[str, Any]:
    """Record the adapter's exception, not a synthetic or transport-only result."""
    store = ManagedMemoryStore(
        ws=ws, store_name=store_name, scope_resolver=lambda: "apx-nxc-failure-probe",
    )
    result: dict[str, Any] = {
        "scenario": scenario,
        "store": store_name,
        "operation": "ManagedMemoryStore.get",
        "expected_codes": sorted(EXPECTED_CODES[scenario]),
        "verified": False,
    }
    try:
        # GET of store metadata, then a sentinel entry if metadata is accessible.
        # No list/search of other actors' content, writes, or permission changes.
        memory = store.get("apx-nxc-missing-entry")
    except Exception as exc:
        result.update(
            exception_type=type(exc).__name__,
            error_code=exc.error_code if isinstance(exc, AgentCliError) else None,
        )
        result["verified"] = isinstance(exc, AgentCliError) and exc.error_code in EXPECTED_CODES[scenario]
    else:
        result.update(exception_type=None, error_code=None, entry_returned=memory is not None)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(EXPECTED_CODES), required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--store", required=True)
    args = parser.parse_args()
    ws = WorkspaceClient(profile=args.profile)
    result = probe(ws, store_name=args.store, scenario=args.scenario)
    result.update(
        timestamp=datetime.now(timezone.utc).isoformat(),
        profile=args.profile,
        host=ws.config.host,
        agentbricks_version=version("databricks-agentbricks"),
        sdk_version=version("databricks-sdk"),
    )
    # Do not print credentials, service messages, or memory content.
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
