"""Canary / A-B deployment helpers — multi-version serving with traffic split.

Databricks Model Serving lets one endpoint serve multiple model
versions concurrently with a configurable traffic split. The platform
primitives (``ws.serving_endpoints.update_config`` + ``TrafficConfig``
+ ``Route``) are the right surface; what's missing is the *workflow*:
"add v42 alongside v41 at 10% traffic", "promote v42 to 100%",
"rollback to the previous split", "did v42 actually do better than v41
in the last 24h?"

This module wraps those four moves:

  * ``deploy_canary(endpoint, new_version, traffic_pct=10, ...)`` —
    Add ``new_version`` as a second served entity. Allocate
    ``traffic_pct`` to it; redistribute the rest across the existing
    served entities proportionally to their current split.

  * ``promote_canary(endpoint, version, ...)`` — Send 100% of traffic
    to ``version``. Other served entities stay configured so a
    rollback can swap traffic back without re-adding them.

  * ``rollback_canary(endpoint, version, ...)`` — Inverse of promote:
    100% to ``version`` (the prior production version).

  * ``analyze_canary(endpoint, experiment, lookback_hours=24, ws=...)``
    — Walk MLflow traces emitted by the endpoint over the window,
    partition by which served-entity handled each request, return
    per-version request counts + latency P50/P95 + error counts.

Per-version trace correlation depends on each served entity having a
distinct served-entity ``name`` (the Databricks SDK names them
``<short_model>-<version>``). When MLflow traces don't carry the
served-entity attribute (older deployments), ``analyze_canary``
returns ``versions=["unknown"]`` and the breakdown is workspace-wide
rather than per-version.

Note: this module assumes the canonical Databricks Agent Framework
naming convention — that ``databricks.agents.deploy`` produces a
served entity per (registered-model, version) tuple. If your endpoint
was wired differently (e.g. external model gateway, direct PUT to
update_config), the helpers still work but the entity names will not
match the convention.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanaryConfig:
    """Snapshot of an endpoint's current canary state.

    Attributes:
        endpoint: Serving endpoint name.
        served_entities: List of ``(name, entity_name, entity_version)``
            tuples — one per served entity on the endpoint.
        traffic_split: ``{served_entity_name: traffic_percentage}``
            summing to ~100.
    """

    endpoint: str
    served_entities: tuple[tuple[str, str, str], ...]
    traffic_split: dict[str, int]


@dataclass(frozen=True)
class VersionMetrics:
    """Per-version aggregate metrics from MLflow traces."""

    version: str
    requests: int
    errors: int
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    latency_avg_ms: float | None

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


@dataclass(frozen=True)
class CanaryReport:
    """Per-version comparison across a lookback window."""

    endpoint: str
    lookback_hours: int
    versions: tuple[VersionMetrics, ...] = field(default_factory=tuple)

    def best_by_latency(self) -> VersionMetrics | None:
        """Version with the lowest P95 latency (tie-broken by request count)."""
        eligible = [v for v in self.versions if v.latency_p95_ms is not None]
        if not eligible:
            return None
        return min(eligible, key=lambda v: (v.latency_p95_ms or 0, -v.requests))

    def best_by_error_rate(self) -> VersionMetrics | None:
        """Version with the lowest error rate (tie-broken by latency)."""
        eligible = [v for v in self.versions if v.requests > 0]
        if not eligible:
            return None
        return min(eligible, key=lambda v: (v.error_rate, v.latency_p95_ms or 0))


# ---------------------------------------------------------------------------
# Endpoint config inspection
# ---------------------------------------------------------------------------


def get_canary_config(endpoint: str, *, ws: "WorkspaceClient") -> CanaryConfig:
    """Read the endpoint's current served entities + traffic split.

    Returns a ``CanaryConfig`` snapshot. Callers use it to inspect
    state, build modified configs for ``update_config`` calls, or
    surface in CLI output.
    """
    info = ws.serving_endpoints.get(endpoint)
    config = getattr(info, "config", None) or getattr(info, "pending_config", None)
    if config is None:
        return CanaryConfig(endpoint=endpoint, served_entities=(), traffic_split={})

    served = (
        getattr(config, "served_entities", None)
        or getattr(config, "served_models", None)
        or []
    )
    entries = tuple(
        (
            getattr(s, "name", "") or "",
            getattr(s, "entity_name", None) or getattr(s, "model_name", "") or "",
            str(getattr(s, "entity_version", None) or getattr(s, "model_version", "") or ""),
        )
        for s in served
    )

    traffic_config = getattr(config, "traffic_config", None)
    split: dict[str, int] = {}
    if traffic_config is not None:
        for route in getattr(traffic_config, "routes", None) or []:
            key = (
                getattr(route, "served_entity_name", None)
                or getattr(route, "served_model_name", "")
                or ""
            )
            if key:
                split[key] = int(getattr(route, "traffic_percentage", 0) or 0)

    return CanaryConfig(endpoint=endpoint, served_entities=entries, traffic_split=split)


# ---------------------------------------------------------------------------
# Deploy / promote / rollback
# ---------------------------------------------------------------------------


def _entity_name(model_full_name: str, version: int | str) -> str:
    """Build the served-entity name the SDK uses for (model, version) tuples.

    Convention: short model name + "-" + version (e.g. ``customer_triage-3``).
    """
    short = model_full_name.rsplit(".", 1)[-1]
    return f"{short}-{version}"


def deploy_canary(
    *,
    endpoint: str,
    registered_model_name: str,
    new_version: int | str,
    canary_traffic_pct: int = 10,
    ws: "WorkspaceClient",
    scale_to_zero_enabled: bool = True,
    workload_size: str = "Small",
) -> CanaryConfig:
    """Add ``new_version`` as a canary served entity on ``endpoint``.

    Allocates ``canary_traffic_pct`` to the new version and redistributes
    the remainder across the existing served entities, weighted by
    their *current* traffic share (so a 70/30 split becomes
    70*0.9 / 30*0.9 / new at 10).

    Args:
        endpoint: Serving endpoint name.
        registered_model_name: Three-part UC name of the registered model
            (``catalog.schema.model``).
        new_version: Model version number to canary.
        canary_traffic_pct: Percentage to route to the new version.
            Must be in ``[1, 99]``.
        ws: ``WorkspaceClient``.
        scale_to_zero_enabled: Set on the new served entity. Default
            True (matches ``databricks.agents.deploy`` default).
        workload_size: Workload size for the new served entity.
            Default ``"Small"``.

    Returns:
        Post-update ``CanaryConfig`` reflecting the new split.
    """
    if not 1 <= canary_traffic_pct <= 99:
        raise ValueError(
            f"canary_traffic_pct must be in [1, 99]; got {canary_traffic_pct!r}"
        )

    from databricks.sdk.service.serving import (
        Route,
        ServedEntityInput,
        TrafficConfig,
    )

    current = get_canary_config(endpoint, ws=ws)
    new_entity_name = _entity_name(registered_model_name, new_version)

    # Build the new served-entities list = existing + canary.
    served_inputs: list[Any] = []
    seen: set[str] = set()
    for name, entity_name, entity_version in current.served_entities:
        if not name or name == new_entity_name:
            continue
        seen.add(name)
        served_inputs.append(ServedEntityInput(
            name=name,
            entity_name=entity_name or None,
            entity_version=entity_version or None,
            scale_to_zero_enabled=scale_to_zero_enabled,
            workload_size=workload_size,
        ))
    served_inputs.append(ServedEntityInput(
        name=new_entity_name,
        entity_name=registered_model_name,
        entity_version=str(new_version),
        scale_to_zero_enabled=scale_to_zero_enabled,
        workload_size=workload_size,
    ))

    # No existing served entities to canary against (newly-created or empty
    # endpoint): there is nothing to split traffic *with*, and Model Serving
    # rejects a TrafficConfig that doesn't sum to 100. Route 100% to the new
    # entity — it is the only thing serving.
    if not seen:
        routes_only = [Route(served_entity_name=new_entity_name, traffic_percentage=100)]
        ws.serving_endpoints.update_config(
            name=endpoint,
            served_entities=served_inputs,
            traffic_config=TrafficConfig(routes=routes_only),
        )
        logger.info(
            "deploy_canary: no existing served entities on %s — routing 100%% to %s",
            endpoint, new_entity_name,
        )
        return get_canary_config(endpoint, ws=ws)

    # Redistribute traffic — existing entities share (100 - canary_traffic_pct),
    # weighted by their current allocation; canary gets canary_traffic_pct.
    remaining = 100 - canary_traffic_pct
    existing_split = {k: v for k, v in current.traffic_split.items() if k in seen}
    total_existing = sum(existing_split.values())
    routes: list[Any] = []
    allocated = 0
    if total_existing > 0 and seen:
        scaled = []
        for name in seen:
            share = existing_split.get(name, 0) / total_existing
            pct = round(share * remaining)
            scaled.append((name, pct))
            allocated += pct
        # Reconcile rounding drift back into the largest existing entity
        if scaled and allocated != remaining:
            scaled.sort(key=lambda kv: -kv[1])
            top_name, top_pct = scaled[0]
            scaled[0] = (top_name, top_pct + (remaining - allocated))
        for name, pct in scaled:
            routes.append(Route(served_entity_name=name, traffic_percentage=pct))
    elif seen:
        # No prior split (newly created endpoint?) — distribute evenly.
        even = remaining // len(seen)
        extra = remaining - even * len(seen)
        for i, name in enumerate(sorted(seen)):
            routes.append(Route(
                served_entity_name=name,
                traffic_percentage=even + (extra if i == 0 else 0),
            ))
    routes.append(Route(
        served_entity_name=new_entity_name,
        traffic_percentage=canary_traffic_pct,
    ))

    ws.serving_endpoints.update_config(
        name=endpoint,
        served_entities=served_inputs,
        traffic_config=TrafficConfig(routes=routes),
    )
    logger.info(
        "deploy_canary: %s %% to %s on %s",
        canary_traffic_pct, new_entity_name, endpoint,
    )
    return get_canary_config(endpoint, ws=ws)


def promote_canary(
    *,
    endpoint: str,
    registered_model_name: str,
    version: int | str,
    ws: "WorkspaceClient",
) -> CanaryConfig:
    """Send 100% of traffic to ``version``. Other entities stay configured.

    Keeping the older served entities in the config (just at 0%
    traffic) means ``rollback_canary`` can swap traffic back without
    re-adding them — fast rollback path for the case where the canary
    looks bad on a metric the lookback didn't catch.
    """
    from databricks.sdk.service.serving import Route, TrafficConfig

    target = _entity_name(registered_model_name, version)
    current = get_canary_config(endpoint, ws=ws)
    served_names = {name for name, _, _ in current.served_entities if name}

    if target not in served_names:
        raise ValueError(
            f"Cannot promote {target!r}: it isn't a served entity on {endpoint!r}. "
            f"Available: {sorted(served_names)}"
        )

    routes = [
        Route(
            served_entity_name=name,
            traffic_percentage=(100 if name == target else 0),
        )
        for name in sorted(served_names)
    ]
    ws.serving_endpoints.update_config(
        name=endpoint,
        traffic_config=TrafficConfig(routes=routes),
    )
    logger.info("promote_canary: 100%% to %s on %s", target, endpoint)
    return get_canary_config(endpoint, ws=ws)


def rollback_canary(
    *,
    endpoint: str,
    registered_model_name: str,
    version: int | str,
    ws: "WorkspaceClient",
) -> CanaryConfig:
    """Send 100% of traffic to ``version`` — typically the prior production version.

    Functionally identical to ``promote_canary``; named separately so the
    call site reads as the rollback semantic the operator intends.
    """
    return promote_canary(
        endpoint=endpoint,
        registered_model_name=registered_model_name,
        version=version,
        ws=ws,
    )


# ---------------------------------------------------------------------------
# Per-version analysis
# ---------------------------------------------------------------------------


def analyze_canary(
    *,
    endpoint: str,
    experiment: str,
    ws: "WorkspaceClient",
    lookback_hours: int = 24,
    max_traces: int = 2000,
) -> CanaryReport:
    """Partition MLflow traces by served-entity version + report metrics per version.

    Pulls traces from ``experiment`` over the lookback window, reads
    each trace's served-entity attribute (typically
    ``apx.model.endpoint_version`` or the platform's
    ``mlflow.databricks.served_entity_name``), and aggregates requests
    / errors / latency P50 / P95 / avg per version.

    Args:
        endpoint: Endpoint to scope the analysis to. Filters by
            ``apx.model.endpoint == endpoint`` when traces carry that
            attribute; otherwise includes all matching experiment
            traces.
        experiment: MLflow experiment name/path.
        ws: ``WorkspaceClient`` (unused today but kept for future
            cases where served-entity → version mapping needs a
            workspace lookup).
        lookback_hours: Window to aggregate over. Default 24h.
        max_traces: Cap on traces fetched. Default 2000.
    """
    try:
        import mlflow
    except ImportError as e:  # pragma: no cover — exercised only without extra
        raise ImportError(
            "analyze_canary requires mlflow. Install with: pip install 'apx-agent[eval]'"
        ) from e

    # Bound the query to the lookback window so stale traffic can't mask a
    # regression. MLflow trace search uses a millisecond ``timestamp_ms``.
    start_time_ms = int((time.time() - lookback_hours * 3600) * 1000)
    filter_string = f"timestamp_ms > {start_time_ms}"

    try:
        # include_spans=False keeps this a metadata-only read so it works even
        # when blob storage is unreachable (private-link / FEVM workspaces
        # where *.storage.cloud.databricks.com is blocked). We only read
        # trace-level tags / status / latency below, never span data.
        traces_raw = mlflow.search_traces(  # type: ignore[attr-defined]
            experiment_names=[experiment],
            filter_string=filter_string,
            max_results=max_traces,
            include_spans=False,
        )
    except Exception as e:
        # Do NOT swallow this into a clean empty report — an empty canary
        # report reads as "no errors observed" and could greenlight
        # promoting a bad model. Surface the failure so the operator knows
        # the analysis did not run (e.g. blocked blob storage, bad token).
        raise RuntimeError(
            f"analyze_canary: search_traces failed for experiment {experiment!r}: {e}"
        ) from e

    if hasattr(traces_raw, "to_dict"):
        try:
            rows: Any = traces_raw.to_dict(orient="records")
        except Exception:
            rows = []
    else:
        rows = traces_raw or []

    # Aggregator: version -> {requests, errors, latencies}
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        attrs = _trace_attrs(r)
        ep = attrs.get("apx.model.endpoint")
        if ep is not None and ep != endpoint:
            continue
        version = _trace_version(attrs)
        bucket = buckets.setdefault(version, {"requests": 0, "errors": 0, "latencies": []})
        bucket["requests"] += 1
        status = _trace_status(r)
        if status and str(status).upper() not in ("OK", "SUCCESS"):
            bucket["errors"] += 1
        latency = _trace_latency_ms(r)
        if latency is not None:
            bucket["latencies"].append(latency)

    versions = []
    for version, bucket in sorted(buckets.items()):
        latencies = bucket["latencies"]
        latencies.sort()
        versions.append(VersionMetrics(
            version=version,
            requests=bucket["requests"],
            errors=bucket["errors"],
            latency_p50_ms=_percentile(latencies, 50),
            latency_p95_ms=_percentile(latencies, 95),
            latency_avg_ms=(
                sum(latencies) / len(latencies) if latencies else None
            ),
        ))

    return CanaryReport(
        endpoint=endpoint,
        lookback_hours=lookback_hours,
        versions=tuple(versions),
    )


def _trace_attrs(t: Any) -> dict[str, Any]:
    if isinstance(t, dict):
        return t.get("tags") or t.get("attributes") or {}
    info = getattr(t, "info", None)
    return dict(getattr(info, "tags", {}) or {}) if info else {}


def _trace_version(attrs: dict[str, Any]) -> str:
    """Extract the served-entity version from trace attributes.

    Looks for, in order: apx.model.endpoint_version, the version
    parsed off the served-entity name (e.g. ``customer_triage-3`` →
    ``3``), or ``"unknown"``.
    """
    explicit = attrs.get("apx.model.endpoint_version")
    if explicit:
        return str(explicit)
    served_entity = attrs.get("mlflow.databricks.served_entity_name") or attrs.get("apx.model.served_entity")
    if served_entity and "-" in str(served_entity):
        return str(served_entity).rsplit("-", 1)[-1]
    return "unknown"


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
    """Compute the requested percentile from a sorted list (or return None)."""
    if not values:
        return None
    if percentile <= 0:
        return values[0]
    if percentile >= 100:
        return values[-1]
    # Nearest-rank — fine for the small samples (~hundreds) we expect.
    idx = max(0, min(len(values) - 1, int(round(len(values) * percentile / 100)) - 1))
    return values[idx]
