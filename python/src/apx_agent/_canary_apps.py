"""Canary helpers for the Databricks Apps deploy target.

See ``docs/apps-canary-hotswap-design.md`` for the full rationale.

The Apps target has no platform-level traffic split. This module
implements the "soak environment" analog: a second App, deployed off
the same bundle source tree but under a deterministically-named target,
running alongside production at a separate URL. Promote / rollback
re-deploy ``prod`` from one tree or another. Analyze partitions MLflow
traces by App name across the lookback window.

For the Model Serving canary (two served entities, one endpoint, real
traffic split), see ``_canary.py``.

Public surface:

  * ``deploy_canary_app`` — write a ``canary-<version>`` DAB target,
    deploy + run it. Returns an ``AppsCanaryConfig`` capturing the
    prod / canary App names + URLs.

  * ``promote_canary_app`` — re-deploy ``prod`` off the canary's
    source tree. Optionally tear down the canary target. The
    operator is expected to have tagged / committed the canary tree
    beforehand; this module does not manage source control.

  * ``rollback_canary_app`` — re-deploy ``prod`` off a prior tagged
    tree (i.e. the operator passes the previous version label).
    Inverse intent of promote; same code path.

  * ``analyze_canary_app`` — partition MLflow traces by the
    ``apx.app.name`` tag over the lookback window, return per-App
    request counts + latency + error counts in an ``AppsCanaryReport``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppsCanaryConfig:
    """Snapshot of an Apps-target canary deployment.

    Attributes:
        bundle_target: DAB target name for the canary (e.g.
            ``canary-feat-x``).
        canary_version: The version label the operator passed
            (sanitized form).
        prod_app_name: Workspace App name for production.
        canary_app_name: Workspace App name for the canary
            (``<prod>-canary-<version>``).
        canary_app_url: URL of the deployed canary App, or ``None`` if
            the deploy step did not return one.
        traffic_hint: The ``--traffic`` value the operator passed.
            Not a routing directive — Apps has no platform traffic
            split — but recorded for the operator's own bookkeeping
            and for downstream tooling that may consume it.
    """

    bundle_target: str
    canary_version: str
    prod_app_name: str
    canary_app_name: str
    canary_app_url: str | None = None
    traffic_hint: int = 0


@dataclass(frozen=True)
class AppsVersionMetrics:
    """Per-partition aggregate metrics from MLflow traces.

    ``app_name`` holds the partition label: a workspace App name for the
    app-tag fallback rows, or a version identity (``apx.model_version`` /
    ``apx.git_sha`` value) for version-keyed rows (issue #404).
    """

    app_name: str
    requests: int
    errors: int
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    latency_avg_ms: float | None

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


@dataclass(frozen=True)
class AppsCanaryReport:
    """Per-App comparison across a lookback window."""

    prod_app_name: str
    canary_app_name: str
    lookback_hours: int
    apps: tuple[AppsVersionMetrics, ...] = field(default_factory=tuple)

    def by_name(self, name: str) -> AppsVersionMetrics | None:
        for a in self.apps:
            if a.app_name == name:
                return a
        return None

    def best_by_latency(self) -> AppsVersionMetrics | None:
        eligible = [a for a in self.apps if a.latency_p95_ms is not None]
        if not eligible:
            return None
        return min(eligible, key=lambda a: (a.latency_p95_ms or 0, -a.requests))

    def best_by_error_rate(self) -> AppsVersionMetrics | None:
        eligible = [a for a in self.apps if a.requests > 0]
        if not eligible:
            return None
        return min(eligible, key=lambda a: (a.error_rate, a.latency_p95_ms or 0))


@dataclass(frozen=True)
class AppsPromoteResult:
    """Outcome of an Apps-target promote / rollback."""

    prod_app_name: str
    promoted_from_version: str
    canary_target_removed: bool


# ---------------------------------------------------------------------------
# Version sanitization + naming
# ---------------------------------------------------------------------------


_SAFE_VERSION = re.compile(r"[^a-z0-9-]+")


def sanitize_version(version: str) -> str:
    """Normalize a free-form version label into a DAB-target-safe slug.

    Lowercase, ``_/.`` → ``-``, strip anything else outside ``[a-z0-9-]``.
    """
    lowered = version.strip().lower()
    lowered = lowered.replace("_", "-").replace("/", "-").replace(".", "-")
    cleaned = _SAFE_VERSION.sub("-", lowered).strip("-")
    if not cleaned:
        raise ValueError(
            f"Canary version {version!r} sanitized to empty string. "
            "Pass a label with at least one alphanumeric character."
        )
    return cleaned


def canary_target_name(version: str) -> str:
    """Return the DAB target name for a canary version."""
    return f"canary-{sanitize_version(version)}"


def canary_app_name(base_app_name: str, version: str) -> str:
    """Return the App name for a canary version of ``base_app_name``.

    Convention: ``<base>-canary-<sanitized-version>``.
    """
    return f"{base_app_name}-canary-{sanitize_version(version)}"


# ---------------------------------------------------------------------------
# Bundle YAML mutation
# ---------------------------------------------------------------------------


def add_canary_target_to_yml(
    doc: dict[str, Any],
    *,
    bundle_key: str,
    base_app_name: str,
    version: str,
    base_target: str = "prod",
) -> dict[str, Any]:
    """Insert a canary target block under ``targets.canary-<version>``.

    The new target's App name overrides the base App name so the
    canary lives as a separate App in the workspace. Everything else
    is inherited from ``base_target`` via DAB's target-merging
    semantics (DAB merges target overlays against the root resources
    block — we only need to override what differs).

    Returns the mutated ``doc`` (mutated in place; returned for chaining).

    Raises:
        KeyError: if the bundle has no ``resources.apps.<bundle_key>``.
    """
    target_name = canary_target_name(version)
    new_app_name = canary_app_name(base_app_name, version)

    resources = doc.setdefault("resources", {})
    apps = resources.setdefault("apps", {})
    if bundle_key not in apps:
        raise KeyError(
            f"Bundle has no resources.apps.{bundle_key} — cannot derive canary."
        )

    targets = doc.setdefault("targets", {})
    targets[target_name] = {
        "resources": {
            "apps": {
                bundle_key: {
                    "name": new_app_name,
                },
            },
        },
    }
    # Mark the base target's intent without rewriting it — operators
    # tracking source state can grep for the canary block by name.
    return doc


def remove_canary_target_from_yml(
    doc: dict[str, Any],
    *,
    version: str,
) -> bool:
    """Remove ``targets.canary-<version>`` if present. Returns True iff removed."""
    target_name = canary_target_name(version)
    targets = doc.get("targets")
    if not isinstance(targets, dict) or target_name not in targets:
        return False
    del targets[target_name]
    return True


def write_databricks_yml(path: Path, doc: dict[str, Any]) -> None:
    """Serialize ``doc`` back to ``path``.

    Same shape as ``cli._auto_update_databricks_yml`` — comments will
    be lost on rewrite. This is documented in
    ``docs/apps-canary-hotswap-design.md``.
    """
    import yaml

    path.write_text(yaml.safe_dump(doc, default_flow_style=False, sort_keys=False))


# ---------------------------------------------------------------------------
# Analyze — partition MLflow traces by app name
# ---------------------------------------------------------------------------


# Tag that the ResponsesAgent / chat_agent compile paths stamp on each
# trace so analyze can partition without screen-scraping logs. If the
# wiring hasn't been updated yet, analyze still works — the bucket
# just lands under ``"unknown"`` and the operator gets a warning.
TRACE_APP_NAME_TAG = "apx.app.name"

# Version-correlation tags (issue #404): the runtime stamps these trace
# tags from the APX_MODEL_VERSION / APX_GIT_SHA env vars the deploy path
# injects. When present, analyze partitions on them — real per-version
# attribution. Absent (pre-#404 deploys) → today's app-name / "unknown"
# fallback. Keys are the AuditAttrs schema in ``_audit.py``.
TRACE_MODEL_VERSION_TAG = "apx.model_version"
TRACE_GIT_SHA_TAG = "apx.git_sha"


def _trace_attrs(t: Any) -> dict[str, Any]:
    if isinstance(t, dict):
        return t.get("tags") or t.get("attributes") or {}
    info = getattr(t, "info", None)
    return dict(getattr(info, "tags", {}) or {}) if info else {}


def _trace_app(attrs: dict[str, Any]) -> str:
    val = attrs.get(TRACE_APP_NAME_TAG)
    if val:
        return str(val)
    return "unknown"


def _trace_version_key(attrs: dict[str, Any]) -> str | None:
    """Version identity for a trace, or None when it carries none.

    Prefers the explicit UC version (``apx.model_version``) over the git
    sha (``apx.git_sha``) — mirrors ``_canary._trace_version``'s
    explicit-first ordering.
    """
    for tag in (TRACE_MODEL_VERSION_TAG, TRACE_GIT_SHA_TAG):
        val = attrs.get(tag)
        if val:
            return str(val)
    return None


def _trace_status(t: Any) -> str | None:
    if isinstance(t, dict):
        return t.get("status")
    info = getattr(t, "info", None)
    if info is None:
        return None
    return getattr(info, "status", None)


def _trace_latency_ms(t: Any) -> int | None:
    if isinstance(t, dict):
        val = t.get("execution_time_ms")
    else:
        info = getattr(t, "info", None)
        val = getattr(info, "execution_time_ms", None) if info else None
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    if percentile <= 0:
        return values[0]
    if percentile >= 100:
        return values[-1]
    idx = max(0, min(len(values) - 1, int(round(len(values) * percentile / 100)) - 1))
    return values[idx]


def analyze_canary_app(
    *,
    prod_app_name: str,
    canary_app_name: str,
    experiment: str,
    lookback_hours: int = 24,
    max_traces: int = 2000,
) -> AppsCanaryReport:
    """Partition MLflow traces per version / App and aggregate metrics.

    Both Apps emit traces to the same experiment via the
    ResponsesAgent compile path. This function:

    1. Calls ``mlflow.search_traces`` for the experiment.
    2. Groups by ``apx.model_version`` / ``apx.git_sha`` when present
       (issue #404 — stamped by #404+ deploys), else the ``apx.app.name``
       tag, else ``"unknown"``.
    3. Returns request count, error count, latency P50/P95/avg per
       partition: always one row per App name, plus one row per observed
       version key.

    Args:
        prod_app_name: Workspace name of the production App.
        canary_app_name: Workspace name of the canary App.
        experiment: MLflow experiment name/path.
        lookback_hours: Window. Default 24.
        max_traces: Cap on traces fetched. Default 2000.
    """
    try:
        import mlflow
    except ImportError as e:  # pragma: no cover — only without [eval] extra
        raise ImportError(
            "analyze_canary_app requires mlflow. "
            "Install with: pip install 'apx-agent[eval]'"
        ) from e

    # Bound the query to the lookback window so stale traffic can't mask a
    # regression. MLflow trace search uses a millisecond ``timestamp_ms``.
    start_time_ms = int((time.time() - lookback_hours * 3600) * 1000)
    filter_string = f"timestamp_ms > {start_time_ms}"

    from ._mlflow_tracing import search_traces_for_experiment

    try:
        # include_spans=False keeps this a metadata-only read so it works even
        # when blob storage is unreachable (private-link / FEVM workspaces).
        # We only read trace-level tags / status / latency below.
        traces_raw = search_traces_for_experiment(
            experiment,
            filter_string=filter_string,
            max_results=max_traces,
            include_spans=False,
        )
    except Exception as e:
        # Do NOT swallow into a clean empty report — that reads as "no errors
        # observed" and could greenlight promoting a bad App. Surface the
        # failure so the operator knows the analysis did not run.
        raise RuntimeError(
            f"analyze_canary_app: search_traces failed for experiment "
            f"{experiment!r}: {e}"
        ) from e

    if hasattr(traces_raw, "to_dict"):
        try:
            rows: Any = traces_raw.to_dict(orient="records")
        except Exception:
            rows = []
    else:
        rows = traces_raw or []

    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        attrs = _trace_attrs(r)
        # Version identity first (issue #404): traces from #404+ deploys
        # carry apx.model_version / apx.git_sha and partition per version.
        # Older traces fall back to the app-name tag, then "unknown".
        version = _trace_version_key(attrs)
        if version is not None:
            key = version
        else:
            key = _trace_app(attrs)
            if key not in (prod_app_name, canary_app_name, "unknown"):
                continue
        bucket = buckets.setdefault(key, {"requests": 0, "errors": 0, "latencies": []})
        bucket["requests"] += 1
        status = _trace_status(r)
        if status and str(status).upper() not in ("OK", "SUCCESS"):
            bucket["errors"] += 1
        latency = _trace_latency_ms(r)
        if latency is not None:
            bucket["latencies"].append(latency)

    def _metrics(name: str, bucket: dict[str, Any]) -> AppsVersionMetrics:
        latencies = sorted(bucket["latencies"])
        return AppsVersionMetrics(
            app_name=name,
            requests=bucket["requests"],
            errors=bucket["errors"],
            latency_p50_ms=_percentile(latencies, 50),
            latency_p95_ms=_percentile(latencies, 95),
            latency_avg_ms=(sum(latencies) / len(latencies) if latencies else None),
        )

    apps: list[AppsVersionMetrics] = []
    for name in (prod_app_name, canary_app_name):
        bucket = buckets.get(name)
        if bucket is None:
            apps.append(AppsVersionMetrics(
                app_name=name, requests=0, errors=0,
                latency_p50_ms=None, latency_p95_ms=None, latency_avg_ms=None,
            ))
            continue
        apps.append(_metrics(name, bucket))

    # Version-keyed rows (issue #404) follow the two app rows, sorted for
    # stable output. "unknown" stays unreported, exactly as before.
    for key in sorted(buckets):
        if key in (prod_app_name, canary_app_name, "unknown"):
            continue
        apps.append(_metrics(key, buckets[key]))

    return AppsCanaryReport(
        prod_app_name=prod_app_name,
        canary_app_name=canary_app_name,
        lookback_hours=lookback_hours,
        apps=tuple(apps),
    )


# ---------------------------------------------------------------------------
# Deploy / promote / rollback orchestration
# ---------------------------------------------------------------------------
#
# These helpers are pure orchestration over the bundle YAML + the
# `databricks` CLI subprocess seam (``apx_agent.cli._run_databricks_cmd``).
# The CLI handler wires them up; isolating the orchestration here keeps
# the click handler thin and lets tests exercise the logic without
# Click's invocation harness.


# Type alias: the seam the CLI exposes for running `databricks ...`.
# Callers (CLI + tests) pass a callable that returns a
# ``subprocess.CompletedProcess``-shaped object with .returncode /
# .stdout / .stderr.
RunCmd = Callable[..., Any]


# Type alias for the injected deploy seam — the CLI passes a wrapper over
# _deploy_apps_impl so this module never imports cli (no cycle).
DeployFn = Callable[..., Any]


def deploy_canary_app(
    *,
    cwd: Path,
    bundle_key: str,
    base_app_name: str,
    canary_version: str,
    traffic_hint: int,
    deploy_fn: "DeployFn",
    base_target: str = "prod",
    git_sha: str | None = None,
) -> AppsCanaryConfig:
    """Write the canary target, then deploy it through the SHARED deploy path.

    ``deploy_fn`` is the prod deploy pipeline (``_deploy_apps_impl``) injected
    by the CLI. Delegating to it — instead of shelling out to a thin
    deploy/run/get subset here — is what makes the soak App a faithful preview:
    it gets the same validate → wheel build → manifest staging → poll → readyz
    → UC registration that prod gets. See
    docs/superpowers/specs/2026-06-12-apps-soak-promote-design.md (Phase 0/P1).

    ``git_sha`` (P1 provenance): when provided, it is stamped on the canary's
    UC manifest version as the ``apx.apps.git_sha`` tag — the exact commit the
    soak App was deployed from. ``promote`` reads it to verify prod ships the
    same commit (gate-don't-mutate). ``None`` when the deploy tree isn't a git
    repo; the tag is simply omitted.
    """
    target_name = canary_target_name(canary_version)
    new_app_name = canary_app_name(base_app_name, canary_version)

    import yaml
    yml_path = cwd / "databricks.yml"
    doc = yaml.safe_load(yml_path.read_text()) or {}
    add_canary_target_to_yml(
        doc, bundle_key=bundle_key, base_app_name=base_app_name,
        version=canary_version, base_target=base_target,
    )
    write_databricks_yml(yml_path, doc)

    version_tags = {"apx.apps.role": "canary"}
    if git_sha:
        version_tags["apx.apps.git_sha"] = git_sha

    deploy_fn(
        bundle_target=target_name,
        app_name_override=new_app_name,
        extra_version_tags=version_tags,
    )

    return AppsCanaryConfig(
        bundle_target=target_name,
        canary_version=sanitize_version(canary_version),
        prod_app_name=base_app_name,
        canary_app_name=new_app_name,
        canary_app_url=None,  # URL now surfaced by the shared deploy path's output
        traffic_hint=traffic_hint,
    )


def promote_canary_app(
    *,
    cwd: Path,
    bundle_key: str,
    base_app_name: str,
    canary_version: str,
    run_cmd: RunCmd,
    profile: str | None = None,
    prod_target: str = "prod",
    keep_canary: bool = False,
) -> AppsPromoteResult:
    """Re-deploy prod from the canary's source tree.

    This is the "ship the canary" move. The caller is expected to
    have the canary's commit checked out, or to have already merged
    the canary's changes onto the prod branch. This function does
    not manage source control — it triggers the prod re-deploy.

    By default tears down the canary target after a successful prod
    deploy. Pass ``keep_canary=True`` to leave it in place (useful
    when you want both URLs serving in parallel for a soak period).

    Raises:
        RuntimeError: if the prod ``bundle deploy`` / ``bundle run`` fails
            or prod cannot be verified as serving. In that case the canary
            target is left intact so it remains a viable rollback target.
    """
    deploy_proc = run_cmd(["bundle", "deploy", "--target", prod_target], profile=profile)
    if getattr(deploy_proc, "returncode", 0) != 0:
        raise RuntimeError(
            f"bundle deploy --target {prod_target} failed "
            f"(exit {deploy_proc.returncode}). stderr: "
            f"{getattr(deploy_proc, 'stderr', '')!r}"
        )
    run_proc = run_cmd(
        ["bundle", "run", bundle_key, "--target", prod_target], profile=profile,
    )
    if getattr(run_proc, "returncode", 0) != 0:
        logger.warning(
            "bundle run for prod returned %s: %s",
            run_proc.returncode, getattr(run_proc, "stderr", ""),
        )

    # Verify prod actually came up serving the promoted deployment BEFORE we
    # tear down the canary. A nonzero ``bundle run`` is sometimes benign (the
    # App was already running), but if prod genuinely failed to come up and we
    # delete the canary, the operator has neither working prod nor a rollback
    # target. So we confirm prod health and refuse to tear down otherwise.
    if not _prod_is_serving(base_app_name=base_app_name, run_cmd=run_cmd, profile=profile):
        raise RuntimeError(
            f"prod App {base_app_name!r} is not serving after deploy to "
            f"target {prod_target!r}; leaving the canary in place as a "
            "rollback target. Inspect the prod App and re-run promote once "
            "prod is healthy (or roll back to the canary)."
        )

    removed = False
    if not keep_canary:
        removed = _teardown_canary(
            cwd=cwd, canary_version=canary_version,
            run_cmd=run_cmd, profile=profile,
            base_app_name=base_app_name,
        )

    return AppsPromoteResult(
        prod_app_name=base_app_name,
        promoted_from_version=sanitize_version(canary_version),
        canary_target_removed=removed,
    )


def rollback_canary_app(
    *,
    cwd: Path,
    bundle_key: str,
    base_app_name: str,
    canary_version: str,
    run_cmd: RunCmd,
    profile: str | None = None,
    prod_target: str = "prod",
) -> AppsPromoteResult:
    """Re-deploy prod from a prior tagged tree.

    Functionally identical to ``promote_canary_app`` — both end with
    a prod deploy + canary teardown. Named separately so the call
    site reads as the rollback semantic the operator intends.
    """
    return promote_canary_app(
        cwd=cwd,
        bundle_key=bundle_key,
        base_app_name=base_app_name,
        canary_version=canary_version,
        run_cmd=run_cmd,
        profile=profile,
        prod_target=prod_target,
        keep_canary=False,
    )


def _prod_is_serving(
    *,
    base_app_name: str,
    run_cmd: RunCmd,
    profile: str | None,
) -> bool:
    """Return True iff ``apps get`` shows prod RUNNING with a SUCCEEDED deploy.

    Reads ``databricks apps get <name>`` and checks the App is RUNNING and
    its active deployment SUCCEEDED — the same signals the deployment
    runbook polls before declaring a deploy live. Treats an
    unparseable / failed ``apps get`` as "not serving" so we err on the
    side of keeping the rollback target.
    """
    import json as _json

    get_proc = run_cmd(["apps", "get", base_app_name], profile=profile)
    if getattr(get_proc, "returncode", 0) != 0:
        logger.warning(
            "apps get %s returned %s while verifying prod health: %s",
            base_app_name, getattr(get_proc, "returncode", "?"),
            getattr(get_proc, "stderr", ""),
        )
        return False
    try:
        payload = _json.loads(getattr(get_proc, "stdout", "") or "{}")
    except Exception as e:
        logger.warning("could not parse apps get %s output: %s", base_app_name, e)
        return False

    app_state = str(((payload.get("app_status") or {}).get("state")) or "").upper()
    compute_state = str(((payload.get("compute_status") or {}).get("state")) or "").upper()
    deploy_state = str(
        (((payload.get("active_deployment") or {}).get("status") or {}).get("state"))
        or ""
    ).upper()

    # The App must be up: RUNNING app status (or ACTIVE compute as a fallback
    # signal). When the payload carries an active deployment, it must have
    # SUCCEEDED — a FAILED / IN_PROGRESS deployment means prod did not come up.
    app_up = app_state == "RUNNING" or compute_state == "ACTIVE"
    deploy_ok = deploy_state in ("", "SUCCEEDED")  # absent → don't over-constrain
    serving = app_up and deploy_ok
    if not serving:
        logger.warning(
            "prod App %s not serving: app_status=%s compute_status=%s "
            "active_deployment=%s",
            base_app_name, app_state or "unknown",
            compute_state or "unknown", deploy_state or "unknown",
        )
    return serving


def _teardown_canary(
    *,
    cwd: Path,
    canary_version: str,
    run_cmd: RunCmd,
    profile: str | None,
    base_app_name: str,
) -> bool:
    """Best-effort removal of the canary App + its target block.

    Returns True iff the YAML target block was removed. The App
    deletion is fire-and-forget — Databricks Apps can be torn down
    via `databricks apps delete`, but the App may have been deleted
    already (operator UI action, prior cleanup, etc.) so we don't
    raise on a non-zero exit.
    """
    import yaml

    yml_path = cwd / "databricks.yml"
    doc = yaml.safe_load(yml_path.read_text()) or {}
    removed = remove_canary_target_from_yml(doc, version=canary_version)
    if removed:
        write_databricks_yml(yml_path, doc)

    new_app_name = canary_app_name(base_app_name, canary_version)
    delete_proc = run_cmd(["apps", "delete", new_app_name], profile=profile)
    if getattr(delete_proc, "returncode", 0) != 0:
        logger.warning(
            "apps delete %s returned %s (continuing): %s",
            new_app_name, delete_proc.returncode,
            getattr(delete_proc, "stderr", ""),
        )
    return removed
