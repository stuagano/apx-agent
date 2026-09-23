"""Ontos Semantic Link Sync — pushes governance classifications to Ontos.

After each evaluate run, resource_classifications holds the current class
assignments for every resource. This module reads those assignments and
creates EntitySemanticLink records in Ontos so Catalog Commander can show
ontology class annotations on UC assets.

How it works:
  1. Read distinct (resource_id, resource_type, class_name) from the
     latest scan's resource_classifications.
  2. For UC resource types (table, schema, catalog), map:
       resource_type → Ontos entity_type
       resource_id   → Ontos entity_id (same format: catalog.schema.table)
       class_name    → ontology IRI (ONTOLOGY_BASE_IRI + class_name)
  3. POST to Ontos /api/semantic-links/ for each new link.
     Existing links are skipped (409 handled gracefully).

Auth:
  Ontos uses the x-forwarded-access-token header as the caller identity.
  Token resolution order:
    1. Explicit ontos_token= parameter
    2. DATABRICKS_TOKEN env var (only OAuth JWTs; serverless dkea... tokens rejected)
    3. SDK config DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET env vars
    4. Secret scope ontos_client_id + ontos_client_secret — M2M OAuth, never expires
    5. Secret scope ontos_token — pre-stored JWT, expires ~1h (dev only)
  The token must belong to a user/SP with 'semantic-models' write access
  in Ontos (admins have this by default).

Non-UC resource types (jobs, clusters, warehouses, pipelines, users) are
skipped — Ontos Catalog Commander only surfaces UC assets.
"""

import logging
import os
from typing import Optional

import requests
from databricks.sdk import WorkspaceClient
from pyspark.sql import SparkSession

logger = logging.getLogger(__name__)

def resolve_ontology_base_iri(
    ontology_base_iri: str | None = None,
    workspace_host: str = "",
) -> str:
    """Resolve the ontology class IRI base with fallback chain.

    Priority:
      1. Explicit ontology_base_iri parameter
      2. GOVERNANCE_ONTOLOGY_BASE_IRI environment variable
      3. Default: https://{workspace_host}/ontology/governance/class/
    """
    iri = ontology_base_iri or os.environ.get("GOVERNANCE_ONTOLOGY_BASE_IRI", "")
    if not iri:
        host = workspace_host.rstrip("/")
        iri = f"{host}/ontology/governance/class/"
    if not iri.endswith("/"):
        iri += "/"
    return iri

# Ontos entity_type for each UC resource type (non-UC types are excluded)
_UC_ENTITY_TYPE_MAP = {
    "table":   "uc_table",
    "schema":  "uc_schema",
    "catalog": "uc_catalog",
}


def _get_m2m_token(host: str, client_id: str, client_secret: str) -> str:
    """Get an M2M OAuth JWT via client_credentials grant.

    This produces an eyJ... JWT that the Databricks Apps proxy accepts.
    PATs and the serverless job's internal execution token do not work with
    the Databricks Apps proxy — only OAuth JWTs are accepted.
    """
    try:
        resp = requests.post(
            f"{host.rstrip('/')}/oidc/v1/token",
            data={"grant_type": "client_credentials", "scope": "all-apis"},
            auth=(client_id, client_secret),
            timeout=10,
        )
        if resp.ok:
            return resp.json().get("access_token", "")
        logger.warning("M2M token request failed: %s", resp.status_code)
    except Exception as exc:
        logger.debug("M2M token request exception: %s", exc)
    return ""


def _resolve_token(
    ontos_token: Optional[str],
    w: Optional[WorkspaceClient] = None,
    secret_scope: Optional[str] = None,
    secret_key: str = "ontos_token",
) -> str:
    """Resolve a Databricks OAuth JWT token for calling the Ontos Databricks App.

    The Databricks Apps proxy requires an OAuth JWT (eyJ...), not a PAT (dapi...)
    or the serverless job's internal execution token.

    Resolution order:
      1. Explicit ontos_token parameter
      2. DATABRICKS_TOKEN env var (only if it is an OAuth JWT)
      3. SDK config client_id/client_secret (set via DATABRICKS_CLIENT_ID/SECRET env vars)
      4. Secret scope ontos_client_id + ontos_client_secret — M2M OAuth for
         serverless jobs; generates a fresh token on every run, never expires
      5. Secret scope <secret_key> (ontos_token) — pre-stored OAuth JWT;
         expires in ~1h, suitable for dev/testing only
    """
    if ontos_token:
        return ontos_token

    env_token = os.environ.get("DATABRICKS_TOKEN", "")
    # Only use DATABRICKS_TOKEN if it's an OAuth JWT (eyJ...).
    # Serverless jobs set this to an internal dkea... token that the
    # Databricks Apps proxy rejects; PATs (dapi...) are also rejected.
    if env_token and env_token.startswith("eyJ"):
        return env_token

    client = w or WorkspaceClient()

    # M2M OAuth via SDK config env vars (DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET)
    sdk_client_id = client.config.client_id
    sdk_client_secret = client.config.client_secret
    if sdk_client_id and sdk_client_secret:
        token = _get_m2m_token(client.config.host, sdk_client_id, sdk_client_secret)
        if token:
            logger.info("Ontos auth: M2M OAuth token via SDK config SP credentials")
            return token

    # M2M OAuth via secret scope — preferred for serverless jobs.
    # Store SP credentials as ontos_client_id and ontos_client_secret in the
    # secret scope. Generates a fresh token on every run; no expiry to manage.
    if secret_scope:
        try:
            sc_client_id = client.dbutils.secrets.get(secret_scope, "ontos_client_id")
            sc_client_secret = client.dbutils.secrets.get(secret_scope, "ontos_client_secret")
            if sc_client_id and sc_client_secret:
                token = _get_m2m_token(client.config.host, sc_client_id, sc_client_secret)
                if token:
                    logger.info("Ontos auth: M2M OAuth token via secret scope SP credentials")
                    return token
        except Exception as exc:
            logger.debug("Secret scope M2M credentials not found: %s", exc)

    # Last resort: pre-stored OAuth JWT. Expires in ~1h — dev/testing only.
    if secret_scope:
        try:
            secret_token = client.dbutils.secrets.get(secret_scope, secret_key)
            if secret_token:
                logger.info("Ontos auth: token from secret %s/%s (expires ~1h)", secret_scope, secret_key)
                return secret_token
        except Exception as exc:
            logger.debug("Secret %s/%s not found: %s", secret_scope, secret_key, exc)

    return ""


def sync_classifications_to_ontos(
    spark: SparkSession,
    catalog: str,
    schema: str,
    ontos_url: str,
    ontology_base_iri: Optional[str] = None,
    ontos_token: Optional[str] = None,
    ontos_user_email: Optional[str] = None,
    dry_run: bool = False,
    w: Optional[WorkspaceClient] = None,
    secret_scope: Optional[str] = None,
) -> dict:
    """Push the latest resource classifications to Ontos as semantic links.

    Args:
        spark: Active SparkSession.
        catalog: Governance catalog name (e.g., 'platform').
        schema: Governance schema name (e.g., 'governance').
        ontos_url: Base URL of the Ontos app
                   (e.g., 'https://ontos-7474657313075170.aws.databricksapps.com').
        ontology_base_iri: Override the ontology class IRI base. Falls back to
                           GOVERNANCE_ONTOLOGY_BASE_IRI env var, then derives
                           from the workspace host.
        ontos_token: Databricks access token to pass as x-forwarded-access-token.
                     Falls back to DATABRICKS_TOKEN env var, then SDK auth.
        ontos_user_email: Optional email for the x-forwarded-email header (helps
                          Ontos cache the workspace client by user rather than
                          token hash).
        dry_run: If True, log what would be posted without calling the API.
        w: Optional WorkspaceClient — used for secret and SDK token resolution.
           If not supplied, a new client is created.
        secret_scope: Databricks secret scope to look up 'ontos_token'. Used
                      when running as a serverless job SP (no env token available).

    Returns:
        dict with keys: posted, skipped, errors, total_candidates
    """
    client = w or WorkspaceClient()
    token = _resolve_token(ontos_token, w=client, secret_scope=secret_scope)
    if not token and not dry_run:
        raise ValueError(
            "No Ontos auth token available. Pass ontos_token=, set "
            "DATABRICKS_TOKEN env var, or ensure the SDK can authenticate."
        )
    base_iri = resolve_ontology_base_iri(
        ontology_base_iri=ontology_base_iri,
        workspace_host=client.config.host or "",
    )

    classifications_table = f"{catalog}.{schema}.resource_classifications"

    # Latest scan_id
    latest_scan = spark.sql(f"""
        SELECT MAX(scan_id) AS scan_id
        FROM {classifications_table}
    """).first()

    if not latest_scan or not latest_scan.scan_id:
        logger.info("No classifications found — skipping Ontos sync.")
        return {"posted": 0, "skipped": 0, "errors": 0, "total_candidates": 0}

    scan_id = latest_scan.scan_id

    # Distinct (resource_id, resource_type, class_name) for UC resources only
    rows = spark.sql(f"""
        SELECT DISTINCT resource_id, resource_type, class_name
        FROM {classifications_table}
        WHERE scan_id = '{scan_id}'
          AND resource_type IN ('table', 'schema', 'catalog')
          AND class_name IS NOT NULL
        ORDER BY resource_id, class_name
    """).collect()

    if not rows:
        logger.info("No UC resource classifications in scan %s.", scan_id)
        return {"posted": 0, "skipped": 0, "errors": 0, "total_candidates": 0}

    api_url = f"{ontos_url.rstrip('/')}/api/semantic-links/"
    # Databricks Apps proxy: authenticate with Authorization: Bearer.
    # Also set x-forwarded-access-token directly in case the proxy passes
    # custom headers through to the downstream app (Ontos reads this header).
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "x-forwarded-access-token": token,
    }
    if ontos_user_email:
        headers["X-Forwarded-Email"] = ontos_user_email
        headers["X-Forwarded-User"] = ontos_user_email

    posted = skipped = errors = 0

    for row in rows:
        entity_type = _UC_ENTITY_TYPE_MAP[row.resource_type]
        iri = f"{base_iri}{row.class_name}"
        payload = {
            "entity_id":   row.resource_id,
            "entity_type": entity_type,
            "iri":         iri,
            "label":       row.class_name,
        }

        if dry_run:
            logger.info("[dry-run] POST %s → %s (%s)", iri, row.resource_id, entity_type)
            posted += 1
            continue

        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                posted += 1
            elif resp.status_code == 409:
                # Link already exists — idempotent, not an error
                skipped += 1
            elif resp.status_code == 400 and "already exists" in resp.text.lower():
                skipped += 1
            else:
                logger.warning(
                    "Failed to post semantic link %s → %s: %s %s",
                    iri, row.resource_id, resp.status_code, resp.text[:200],
                )
                errors += 1
        except Exception as exc:
            logger.warning("Error posting semantic link %s → %s: %s", iri, row.resource_id, exc)
            errors += 1

    total = len(rows)
    logger.info(
        "Ontos sync complete: %d posted, %d already existed, %d errors (scan %s)",
        posted, skipped, errors, scan_id,
    )
    return {
        "posted": posted,
        "skipped": skipped,
        "errors": errors,
        "total_candidates": total,
    }
