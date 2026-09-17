"""Declared external-model serving endpoints (``model = "bedrock:…"``).

An apx author normally declares ``model`` as the name of a serving endpoint
that already exists. A *provider scheme* — ``bedrock:`` — instead declares a
**non-native** model that apx provisions as a governed Databricks external-model
serving endpoint (Mosaic AI Gateway: guardrails + usage tracking) on deploy.

A provider is a **data row** in ``_PROVIDER_REGISTRY``, not a code path: adding
one is adding a dict entry. A bare ``model`` (no known scheme) is left alone —
today's existing-endpoint behavior. An unknown scheme fails clear at parse time.

The reconcile is **create-or-update, never delete** (v1) and **fail-closed**: a
missing/unreachable credential or a failing create/update call raises, so a
deploy never leaves a half-provisioned, ungoverned endpoint behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._models import GatewayConfig


# ---------------------------------------------------------------------------
# Provider registry — data, not code. Adding a provider = adding a row.
# ---------------------------------------------------------------------------
# Each row maps a declared scheme to the Databricks external-model *provider*
# name (lowercase, as the serving API expects) and the provider-specific config
# block key that carries the credential + region. No per-scheme branching lives
# anywhere else — ``parse_model_scheme`` / ``build_endpoint_payload`` read these
# fields generically.
#
# ponytail: bedrock only. azure/anthropic aren't rows yet because the generic
# credential fragment (``_resolve_credential``) is bedrock-shaped
# (``uc_service_credential_name`` / secret pair); Azure ``openai_config`` and
# ``anthropic_config`` need different auth fields (api base, deployment name,
# provider api key). Adding a row is still data — but do it WITH its
# provider-specific credential mapping + a live-proven test, not as a stub.
_PROVIDER_REGISTRY: dict[str, dict[str, str]] = {
    "bedrock": {"provider": "amazon-bedrock", "config_key": "amazon_bedrock_config"},
}


@dataclass(frozen=True)
class ExternalModelSpec:
    """A parsed ``<scheme>:<model_name>`` declaration."""

    scheme: str
    provider: str  # Databricks external-model provider, lowercase
    model_name: str
    endpoint_name: str  # the serving endpoint apx provisions/references


def _slug(text: str) -> str:
    """Lowercase, dash-safe slug for a serving-endpoint name."""
    out = [c if c.isalnum() else "-" for c in text.lower()]
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "model"


def parse_model_scheme(model: str) -> ExternalModelSpec | None:
    """Parse ``model`` into an :class:`ExternalModelSpec`, or ``None``.

    * ``"bedrock:anthropic.claude-3-5-sonnet"`` → an ``ExternalModelSpec`` whose
      ``provider`` is registry-resolved (``"amazon-bedrock"``).
    * ``"databricks-claude-sonnet-4-6"`` (no ``:``) → ``None`` — treat as an
      existing endpoint name, no external-model provisioning.
    * ``"quantum:foo"`` (unknown scheme) → raises ``ValueError`` naming the
      unknown scheme and the known ones; never builds a payload.
    """
    if ":" not in model:
        return None
    scheme, model_name = model.split(":", 1)
    row = _PROVIDER_REGISTRY.get(scheme)
    if row is None:
        known = ", ".join(sorted(_PROVIDER_REGISTRY))
        raise ValueError(
            f"Unknown model provider scheme {scheme!r} in model={model!r}. "
            f"Known schemes: {known}. (A bare endpoint name with no scheme is "
            f"treated as an existing serving endpoint.)"
        )
    endpoint_name = f"apx-ext-{scheme}-{_slug(model_name)}"
    return ExternalModelSpec(
        scheme=scheme,
        provider=row["provider"],
        model_name=model_name,
        endpoint_name=endpoint_name,
    )


def _resolve_credential(gateway: "GatewayConfig | None") -> dict[str, str]:
    """Return the provider-config credential fragment, or fail closed.

    UC service credential (``credential``) is preferred; a ``secret_scope`` /
    ``secret_key`` pair is the documented fallback. Neither present → raise
    (deploy fails closed; no create/update call is made).
    """
    if gateway is not None and gateway.credential:
        # CLI --json strips this field (memory project_bedrock_via_ai_gateway);
        # the REST path in reconcile carries it.
        return {"uc_service_credential_name": gateway.credential}
    if gateway is not None and gateway.secret_scope and gateway.secret_key:
        # ponytail: literal scope/key keys stand in for the provider's real
        # secret-ref fields; live wiring is the manual AC-13 runbook.
        return {"secret_scope": gateway.secret_scope, "secret_key": gateway.secret_key}
    raise ValueError(
        "External-model endpoint requires a credential: set [tool.apx.agent.gateway] "
        "credential (a UC service credential name) or a secret_scope/secret_key pair. "
        "Refusing to provision an endpoint with no credential (fail-closed)."
    )


def _guardrail_side() -> dict[str, Any]:
    """Governance-ON guardrail block (PII + safety) for one direction."""
    return {"pii": {"behavior": "BLOCK"}, "safety": True}


def build_endpoint_payload(
    spec: ExternalModelSpec, gateway: "GatewayConfig | None"
) -> dict[str, Any]:
    """Build the ``POST /api/2.0/serving-endpoints`` body for *spec*.

    Carries ``external_model`` (provider lowercase + model name + credential)
    and an ``ai_gateway`` block (guardrails + usage tracking, governance-ON by
    default). ``name`` lives inside the body. Fails closed when no credential is
    declared (see :func:`_resolve_credential`).
    """
    row = _PROVIDER_REGISTRY[spec.scheme]
    config_key = row["config_key"]
    provider_config: dict[str, Any] = dict(_resolve_credential(gateway))
    if gateway is not None and gateway.aws_region:
        provider_config["aws_region"] = gateway.aws_region

    ai_gateway: dict[str, Any] = {
        "usage_tracking_config": {
            "enabled": gateway.usage_tracking if gateway is not None else True
        },
    }
    if gateway is None or gateway.guardrails:
        ai_gateway["guardrails"] = {
            "input": _guardrail_side(),
            "output": _guardrail_side(),
        }

    return {
        "name": spec.endpoint_name,
        "config": {
            "served_entities": [
                {
                    "name": _slug(spec.model_name),
                    "external_model": {
                        "provider": spec.provider,
                        "name": spec.model_name,
                        "task": "llm/v1/chat",
                        config_key: provider_config,
                    },
                }
            ]
        },
        "ai_gateway": ai_gateway,
    }


def reconcile_external_model_endpoint(ws: Any, payload: dict[str, Any]) -> None:
    """Provision or converge the external-model endpoint. Never deletes.

    get → create-if-absent / update-config-if-present. Fail-closed: a failing
    create/update call propagates (no swallow, no partial success). Only a
    not-found on ``get`` routes to the create path.
    """
    from databricks.sdk.errors import NotFound

    name = payload["name"]
    try:
        ws.serving_endpoints.get(name)
        exists = True
    except NotFound:
        exists = False

    if not exists:
        ws.api_client.do("POST", "/api/2.0/serving-endpoints", body=payload)
        return

    # Converge an existing endpoint to the declaration — never delete (v1).
    ws.api_client.do(
        "PUT", f"/api/2.0/serving-endpoints/{name}/config", body=payload["config"]
    )
    ws.api_client.do(
        "PUT",
        f"/api/2.0/serving-endpoints/{name}/ai-gateway",
        body=payload["ai_gateway"],
    )
