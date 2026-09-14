"""Static handoff-readiness linter for a DABs ``databricks.yml`` bundle.

A build is "handoff-ready" when someone other than its author can run and own
it without inheriting a personal identity, a runaway cost, or a name that only
resolves in the author's current catalog. These pure functions parse a bundle
dict and return a ``list[str]`` of violations (empty = clean); each message
names the offending path/identifier so caps evidence is real, not a bare count.

Shared by both callers (Ponytail: one linter, two callers):
  * the cheap pytest gate  — python/tests/test_handoff_ready.py
  * the live prove script  — python/checks/prove_handoff_ready.py

There is no generic DABs parser in ``src``; ``load_bundle`` reads the file with
PyYAML directly.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def load_bundle(path: str | Path) -> dict[str, Any]:
    """Parse a bundle ``databricks.yml`` into a dict (empty file -> {})."""
    doc = yaml.safe_load(Path(path).read_text())
    if not isinstance(doc, dict):
        return {}
    return doc


def _resources(bundle: dict[str, Any], kind: str) -> dict[str, Any]:
    """Return ``resources.<kind>`` as a name->definition dict (or empty)."""
    resources = bundle.get("resources")
    if not isinstance(resources, dict):
        return {}
    section = resources.get(kind)
    if not isinstance(section, dict):
        return {}
    return {k: v for k, v in section.items() if isinstance(v, dict)}


def check_job_timeouts(bundle: dict[str, Any]) -> list[str]:
    """Every job (or each of its tasks) must declare ``timeout_seconds``."""
    violations: list[str] = []
    for job_name, job in _resources(bundle, "jobs").items():
        job_has_timeout = bool(job.get("timeout_seconds"))
        tasks = job.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            if not job_has_timeout:
                violations.append(f"job {job_name!r} has no timeout_seconds")
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_key = task.get("task_key", "<unnamed>")
            if not job_has_timeout and not task.get("timeout_seconds"):
                violations.append(
                    f"job {job_name!r} task {task_key!r} has no timeout_seconds"
                )
    return violations


def check_warehouse_autostop_and_scale_to_zero(bundle: dict[str, Any]) -> list[str]:
    """Warehouses must auto-stop; serving endpoints must scale to zero."""
    violations: list[str] = []
    for wh_name, wh in _resources(bundle, "sql_warehouses").items():
        auto_stop = wh.get("auto_stop_mins")
        if not isinstance(auto_stop, int) or auto_stop <= 0:
            violations.append(
                f"warehouse {wh_name!r} has no positive auto_stop_mins "
                f"(got {auto_stop!r})"
            )
    for ep_name, ep in _resources(bundle, "model_serving_endpoints").items():
        config = ep.get("config")
        served = []
        if isinstance(config, dict):
            served = config.get("served_entities") or config.get("served_models") or []
        if not isinstance(served, list) or not served:
            violations.append(
                f"serving endpoint {ep_name!r} declares no served entities to scale to zero"
            )
            continue
        for entity in served:
            if not isinstance(entity, dict):
                continue
            entity_name = entity.get("name", "<unnamed>")
            if entity.get("scale_to_zero_enabled") is not True:
                violations.append(
                    f"serving endpoint {ep_name!r} entity {entity_name!r} "
                    f"does not set scale_to_zero_enabled: true"
                )
    return violations


def _is_personal(identity: str) -> bool:
    # ponytail: a personal owner is a user email; an SP is an application-id/name.
    return identity.endswith("@databricks.com")


def _iter_owner_identities(node: Any) -> list[tuple[str, str]]:
    """Collect (source, identity) for every declared owner in the bundle tree.

    Owners come from ``run_as.user_name``, ``owner``/``owner_email``, and
    ``permissions[].user_name``. Service-principal fields are ignored — they are
    the desired handoff owner.
    """
    found: list[tuple[str, str]] = []

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            run_as = obj.get("run_as")
            if isinstance(run_as, dict) and isinstance(run_as.get("user_name"), str):
                found.append((f"{path}.run_as.user_name", run_as["user_name"]))
            for key in ("owner", "owner_email"):
                val = obj.get(key)
                if isinstance(val, str):
                    found.append((f"{path}.{key}", val))
            perms = obj.get("permissions")
            if isinstance(perms, list):
                for i, perm in enumerate(perms):
                    if isinstance(perm, dict) and isinstance(perm.get("user_name"), str):
                        found.append(
                            (f"{path}.permissions[{i}].user_name", perm["user_name"])
                        )
            for k, v in obj.items():
                if k not in ("run_as", "permissions", "owner", "owner_email"):
                    walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{path}[{i}]")

    walk(node, "")
    return found


def check_no_personal_owner(bundle: dict[str, Any]) -> list[str]:
    """No declared owner may be a personal ``@databricks.com`` identity."""
    return [
        f"{source} is a personal identity {identity!r} (should be a service principal)"
        for source, identity in _iter_owner_identities(bundle)
        if _is_personal(identity)
    ]


_TABLE_KEYS = frozenset(
    {"table", "table_name", "source_table", "target_table", "full_name"}
)
_TEMPLATE = re.compile(r"\$\{[^}]*\}")


def _fqn_segments(ref: str) -> int:
    """Count dotted segments, masking ``${...}`` templates first.

    ``${var.catalog}.sales.orders`` -> 3 (the catalog slot is templated, so the
    static heuristic treats it as fully-qualified — see the FQN-heuristic ceiling
    in the PRD risks).
    """
    return len(_TEMPLATE.sub("X", ref).split("."))


def check_tables_fqn(bundle: dict[str, Any]) -> list[str]:
    """Table references must be ``catalog.schema.table`` (static heuristic).

    ponytail: naive dot-segment count after masking ``${...}``; a genuinely
    resolved ``${var.catalog}`` reference reads as FQN and the live inventory
    check (AC-11) covers the real fact. Bare (1) / catalog-less (2) segment refs
    are flagged.
    """
    violations: list[str] = []

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                here = f"{path}.{k}" if path else str(k)
                if k in _TABLE_KEYS and isinstance(v, str) and _fqn_segments(v) < 3:
                    violations.append(
                        f"{here} references non-fully-qualified table {v!r} "
                        f"(want catalog.schema.table)"
                    )
                elif k == "tables" and isinstance(v, list):
                    for i, ref in enumerate(v):
                        if isinstance(ref, str) and _fqn_segments(ref) < 3:
                            violations.append(
                                f"{here}[{i}] references non-fully-qualified table "
                                f"{ref!r} (want catalog.schema.table)"
                            )
                        else:
                            walk(ref, f"{here}[{i}]")
                else:
                    walk(v, here)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{path}[{i}]")

    walk(bundle, "")
    return violations


def _has_named_owner(job: dict[str, Any], bundle: dict[str, Any]) -> bool:
    for scope in (job, bundle):
        run_as = scope.get("run_as")
        if isinstance(run_as, dict) and (
            run_as.get("service_principal_name") or run_as.get("user_name")
        ):
            return True
    return False


def check_schedules_paused_or_owned(bundle: dict[str, Any]) -> list[str]:
    """A running (non-PAUSED) schedule must have a named owner (run_as)."""
    violations: list[str] = []
    for job_name, job in _resources(bundle, "jobs").items():
        schedule = job.get("schedule")
        if not isinstance(schedule, dict):
            continue
        if schedule.get("pause_status") == "PAUSED":
            continue
        if not _has_named_owner(job, bundle):
            violations.append(
                f"job {job_name!r} schedule is not PAUSED and has no named run_as owner"
            )
    return violations


def lint_bundle(bundle: dict[str, Any]) -> list[str]:
    """Aggregate every handoff check. Empty list == handoff-ready."""
    violations: list[str] = []
    for check in (
        check_job_timeouts,
        check_warehouse_autostop_and_scale_to_zero,
        check_no_personal_owner,
        check_tables_fqn,
        check_schedules_paused_or_owned,
    ):
        violations.extend(check(bundle))
    return violations


def demo() -> None:
    """Self-check: a violating bundle yields named violations; a clean one none."""
    dirty = {
        "permissions": [{"level": "CAN_MANAGE", "user_name": "author@databricks.com"}],
        "resources": {
            "jobs": {
                "ingest": {
                    "tasks": [{"task_key": "load"}],
                    "schedule": {"pause_status": "UNPAUSED"},
                }
            },
            "sql_warehouses": {"wh": {}},
            "model_serving_endpoints": {
                "ep": {"config": {"served_entities": [{"name": "m"}]}}
            },
        },
        "tables": ["orders", "main.sales.orders"],
    }
    report = lint_bundle(dirty)
    assert any("timeout_seconds" in v for v in report), report
    assert any("personal identity" in v for v in report), report
    assert any("auto_stop_mins" in v for v in report), report
    assert any("scale_to_zero" in v for v in report), report
    assert any("non-fully-qualified" in v and "orders'" in v for v in report), report
    assert any("not PAUSED" in v for v in report), report
    # the FQN'd table must not be flagged
    assert not any("main.sales.orders" in v for v in report), report

    clean = {
        "run_as": {"service_principal_name": "1234-app-id"},
        "resources": {
            "jobs": {
                "ingest": {
                    "timeout_seconds": 3600,
                    "tasks": [{"task_key": "load"}],
                    "schedule": {"pause_status": "PAUSED"},
                }
            },
            "sql_warehouses": {"wh": {"auto_stop_mins": 10}},
            "model_serving_endpoints": {
                "ep": {
                    "config": {
                        "served_entities": [{"name": "m", "scale_to_zero_enabled": True}]
                    }
                }
            },
        },
        "tables": ["main.sales.orders"],
    }
    assert lint_bundle(clean) == [], lint_bundle(clean)
    print("ok")


if __name__ == "__main__":
    demo()
