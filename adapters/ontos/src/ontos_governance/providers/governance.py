"""GovernanceProvider — reads governance data from Delta tables.

Implements the GovernanceProvider protocol by querying the
``platform.governance.*`` tables written by the Governance Monitoring scan engine.
Uses the Databricks SQL Connector (no Spark session required).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from databricks import sql

from ontos_governance.models import (
    ExceptionFilters,
    ExceptionRecord,
    ExceptionSummary,
    Grant,
    GrantSummary,
    OntologyClass,
    OntologyTree,
    OntologyTreeNode,
    Policy,
    PolicyBase,
    PolicyBreakdown,
    PolicyFilters,
    PolicyVersion,
    Resource,
    ResourceDetail,
    ResourceFilters,
    ScanDetail,
    ScanRun,
    ValidationResult,
    Violation,
    ViolationFilters,
    ViolationSummary,
)

logger = logging.getLogger(__name__)


def _parse_proposed_state(proposed_sql: str) -> str:
    """Extract tag key-value pairs from a SET TAGS SQL statement.

    Returns a JSON string of {key: value} pairs.
    Falls back to empty dict if SQL doesn't match the expected pattern.
    """
    match = re.search(r"SET TAGS\s*\((.+)\)", proposed_sql)
    if not match:
        return "{}"
    state = {}
    for pair in re.finditer(r"'([^']+)'\s*=\s*'([^']*)'", match.group(1)):
        state[pair.group(1)] = pair.group(2)
    return json.dumps(state)


class GovernanceProvider:
    """Governance provider backed by Governance Delta tables.

    Args:
        catalog: Unity Catalog name (default: ``platform``)
        schema: Schema within the catalog (default: ``governance``)
        ontology_dir: Path to the directory containing ``resource_classes.yml``
        server_hostname: Databricks workspace hostname
        http_path: SQL warehouse HTTP path
        access_token: PAT or OAuth token (mutually exclusive with client credentials)
        client_id: Service principal client ID
        client_secret: Service principal client secret
    """

    def __init__(
        self,
        *,
        catalog: str = "platform",
        schema: str = "governance",
        ontology_dir: str | Path | None = None,
        server_hostname: str | None = None,
        http_path: str | None = None,
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._catalog = catalog
        self._schema = schema
        self._ontology_dir = Path(ontology_dir) if ontology_dir else None

        self._active_metastore: str | None = None

        self._conn_kwargs: dict[str, Any] = {}
        if server_hostname:
            self._conn_kwargs["server_hostname"] = server_hostname.replace("https://", "")
        if http_path:
            self._conn_kwargs["http_path"] = http_path
        if access_token:
            self._conn_kwargs["access_token"] = access_token
        elif client_id and client_secret:
            self._conn_kwargs["client_id"] = client_id
            self._conn_kwargs["client_secret"] = client_secret

    @classmethod
    def from_env(cls) -> GovernanceProvider:
        """Create a provider from environment variables.

        Reads: ``GOVERNANCE_CATALOG``, ``GOVERNANCE_SCHEMA``,
        ``DATABRICKS_HOST``, ``DATABRICKS_WAREHOUSE_HTTP_PATH``,
        ``DATABRICKS_TOKEN``, ``DATABRICKS_CLIENT_ID``,
        ``DATABRICKS_CLIENT_SECRET``, ``GOVERNANCE_ONTOLOGY_DIR``.
        """
        return cls(
            catalog=os.environ.get("GOVERNANCE_CATALOG", "platform"),
            schema=os.environ.get("GOVERNANCE_SCHEMA", "governance"),
            ontology_dir=os.environ.get("GOVERNANCE_ONTOLOGY_DIR"),
            server_hostname=os.environ.get("DATABRICKS_HOST"),
            http_path=os.environ.get("DATABRICKS_WAREHOUSE_HTTP_PATH"),
            access_token=os.environ.get("DATABRICKS_TOKEN"),
            client_id=os.environ.get("DATABRICKS_CLIENT_ID"),
            client_secret=os.environ.get("DATABRICKS_CLIENT_SECRET"),
        )

    # ── Connection helpers ────────────────────────────────────────────────

    @lru_cache(maxsize=1)
    def _connection(self):
        return sql.connect(**self._conn_kwargs)

    def _execute(self, query: str) -> list[dict[str, Any]]:
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(query)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def _execute_write(self, query: str) -> None:
        conn = self._connection()
        with conn.cursor() as cur:
            cur.execute(query)

    def _tbl(self, name: str) -> str:
        return f"{self._catalog}.{self._schema}.{name}"

    @staticmethod
    def _esc(value: str) -> str:
        """Escape a string for SQL literals (single-quote doubling)."""
        return value.replace("'", "''")

    # ── Metastore helpers ─────────────────────────────────────────────────

    def set_active_metastore(self, metastore_id: str | None) -> None:
        """Set the active metastore filter. None clears the filter."""
        self._active_metastore = metastore_id

    def _resolve_metastore(self, metastore_id: str | None = None) -> str | None:
        """Resolve metastore: explicit param > active metastore > None."""
        return metastore_id or self._active_metastore or None

    def _metastore_clause(
        self,
        metastore_id: str | None = None,
        *,
        prefix: str = "AND",
    ) -> str:
        """Return a SQL clause fragment for metastore filtering.

        Returns empty string when no metastore is active.
        """
        resolved = self._resolve_metastore(metastore_id)
        if not resolved:
            return ""
        return f"{prefix} metastore_id = '{self._esc(resolved)}'"

    def list_metastores(self) -> list[dict]:
        """List all metastores with latest scan timestamp and resource count."""
        rows = self._execute(f"""
            SELECT metastore_id,
                   MAX(CAST(last_seen AS STRING)) AS last_scanned,
                   COUNT(DISTINCT resource_id)     AS resource_count
            FROM {self._tbl('resource_inventory')}
            WHERE metastore_id IS NOT NULL AND metastore_id != ''
            GROUP BY metastore_id
            ORDER BY last_scanned DESC
        """)
        return rows

    # ── Violations ────────────────────────────────────────────────────────

    def violations_summary(self, *, metastore_id: str | None = None) -> ViolationSummary:
        ms_where = self._metastore_clause(metastore_id, prefix="WHERE")
        rows = self._execute(f"""
            SELECT
                COUNT(*)                                                      AS total,
                SUM(CASE WHEN active THEN 1 ELSE 0 END)                      AS active,
                SUM(CASE WHEN active AND severity = 'critical' THEN 1 ELSE 0 END) AS critical,
                SUM(CASE WHEN active AND severity = 'high'     THEN 1 ELSE 0 END) AS high,
                SUM(CASE WHEN active AND severity = 'medium'   THEN 1 ELSE 0 END) AS medium,
                SUM(CASE WHEN active AND severity = 'low'      THEN 1 ELSE 0 END) AS low
            FROM {self._tbl('violations')}
            {ms_where}
        """)
        return ViolationSummary(**(rows[0] if rows else {}))

    def list_violations(self, filters: ViolationFilters, *, metastore_id: str | None = None) -> list[Violation]:
        resolved_ms = self._resolve_metastore(metastore_id)
        conditions = [f"active = {str(filters.active).lower()}"]
        if resolved_ms:
            conditions.append(f"metastore_id = '{self._esc(resolved_ms)}'")
        if filters.severity:
            conditions.append(f"severity = '{self._esc(filters.severity)}'")
        if filters.policy_id:
            conditions.append(f"policy_id = '{self._esc(filters.policy_id)}'")
        if filters.resource_id:
            conditions.append(f"resource_id = '{self._esc(filters.resource_id)}'")
        if filters.domain:
            conditions.append(f"domain = '{self._esc(filters.domain)}'")
        where = "WHERE " + " AND ".join(conditions)

        rows = self._execute(f"""
            SELECT
                violation_id, resource_id, resource_name, resource_type,
                policy_id, policy_name, severity, domain,
                CAST(first_seen AS STRING) AS first_seen,
                CAST(last_seen  AS STRING) AS last_seen,
                active, scan_id
            FROM {self._tbl('violations')}
            {where}
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high'     THEN 1
                    WHEN 'medium'   THEN 2
                    ELSE 3
                END,
                last_seen DESC
            LIMIT {filters.limit} OFFSET {filters.offset}
        """)
        return [Violation(**r) for r in rows]

    # ── Scans ─────────────────────────────────────────────────────────────

    def list_scans(self, limit: int = 50) -> list[ScanRun]:
        rows = self._execute(f"""
            SELECT
                scan_id,
                CAST(MIN(evaluated_at) AS STRING) AS started_at,
                CAST(MAX(evaluated_at) AS STRING) AS finished_at,
                COUNT(DISTINCT resource_id)        AS resources_scanned,
                COUNT(*)                           AS evaluations,
                SUM(CASE WHEN result = 'fail' THEN 1 ELSE 0 END) AS failures
            FROM {self._tbl('scan_results')}
            GROUP BY scan_id
            ORDER BY MIN(evaluated_at) DESC
            LIMIT {limit}
        """)
        return [ScanRun(**r) for r in rows]

    def get_scan(self, scan_id: str) -> ScanDetail:
        safe_id = self._esc(scan_id)
        rows = self._execute(f"""
            SELECT
                scan_id,
                CAST(MIN(evaluated_at) AS STRING) AS started_at,
                CAST(MAX(evaluated_at) AS STRING) AS finished_at,
                COUNT(DISTINCT resource_id)        AS resources_scanned,
                COUNT(*)                           AS evaluations,
                SUM(CASE WHEN result = 'fail' THEN 1 ELSE 0 END) AS failures
            FROM {self._tbl('scan_results')}
            WHERE scan_id = '{safe_id}'
            GROUP BY scan_id
        """)
        if not rows:
            raise LookupError(f"Scan {scan_id} not found")

        breakdown = self._execute(f"""
            SELECT
                policy_id, domain, severity,
                COUNT(*)                                          AS evaluations,
                SUM(CASE WHEN result = 'fail' THEN 1 ELSE 0 END) AS failures
            FROM {self._tbl('scan_results')}
            WHERE scan_id = '{safe_id}'
            GROUP BY policy_id, domain, severity
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high'     THEN 1
                    WHEN 'medium'   THEN 2
                    ELSE 3
                END
        """)

        return ScanDetail(
            **rows[0],
            policy_breakdown=[PolicyBreakdown(**b) for b in breakdown],
        )

    # ── Resources ─────────────────────────────────────────────────────────

    def list_resources(self, filters: ResourceFilters, *, metastore_id: str | None = None) -> list[Resource]:
        if filters.scan_id:
            scan_filter = f"scan_id = '{self._esc(filters.scan_id)}'"
        else:
            scan_filter = f"""
                scan_id = (
                    SELECT scan_id
                    FROM {self._tbl('scan_results')}
                    ORDER BY evaluated_at DESC
                    LIMIT 1
                )
            """

        resolved_ms = self._resolve_metastore(metastore_id)
        conditions = [scan_filter]
        if resolved_ms:
            conditions.append(f"metastore_id = '{self._esc(resolved_ms)}'")
        if filters.resource_type:
            conditions.append(f"resource_type = '{self._esc(filters.resource_type)}'")
        where = "WHERE " + " AND ".join(conditions)

        rows = self._execute(f"""
            SELECT
                resource_id, resource_name, resource_type,
                CAST(first_seen AS STRING) AS first_seen,
                CAST(last_seen  AS STRING) AS last_seen,
                scan_id, metadata
            FROM {self._tbl('resource_inventory')}
            {where}
            ORDER BY resource_type, resource_name
            LIMIT {filters.limit} OFFSET {filters.offset}
        """)
        return [Resource(**r) for r in rows]

    def get_resource(self, resource_id: str) -> ResourceDetail:
        safe_id = self._esc(resource_id)
        inv = self._execute(f"""
            SELECT resource_id, resource_name, resource_type,
                   CAST(first_seen AS STRING) AS first_seen,
                   CAST(last_seen  AS STRING) AS last_seen,
                   scan_id, metadata
            FROM {self._tbl('resource_inventory')}
            WHERE resource_id = '{safe_id}'
            ORDER BY last_seen DESC
            LIMIT 1
        """)
        if not inv:
            raise LookupError(f"Resource {resource_id} not found")

        classifications = self._execute(f"""
            SELECT class_name, class_ancestors, root_class,
                   CAST(classified_at AS STRING) AS classified_at
            FROM {self._tbl('resource_classifications')}
            WHERE resource_id = '{safe_id}'
            ORDER BY class_name
        """)

        violations = self._execute(f"""
            SELECT violation_id, policy_id, policy_name, severity, domain,
                   CAST(first_seen AS STRING) AS first_seen,
                   CAST(last_seen  AS STRING) AS last_seen,
                   active
            FROM {self._tbl('violations')}
            WHERE resource_id = '{safe_id}'
            ORDER BY active DESC,
                CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                              WHEN 'medium' THEN 2 ELSE 3 END
        """)

        exceptions = self._execute(f"""
            SELECT exception_id, policy_id, approved_by, justification,
                   CAST(approved_at AS STRING) AS approved_at,
                   CAST(expires_at  AS STRING) AS expires_at,
                   active,
                   CASE
                       WHEN expires_at IS NULL                                  THEN 'permanent'
                       WHEN expires_at < current_timestamp()                    THEN 'expired'
                       WHEN expires_at < current_timestamp() + INTERVAL 30 DAY THEN 'expiring_soon'
                       ELSE 'active'
                   END AS expiry_status
            FROM {self._tbl('exceptions')}
            WHERE resource_id = '{safe_id}'
            ORDER BY active DESC, approved_at DESC
        """)

        # dq_status is optional — only created when the crawler is
        # configured with DQX metrics tables — so a missing table must
        # not break the resource detail endpoint.
        metrics: list[dict[str, Any]] = []
        try:
            metrics = self._execute(f"""
                SELECT source, metric, value, status, anomaly,
                       CAST(checked_at AS STRING) AS checked_at
                FROM {self._tbl('dq_status')}
                WHERE table_id = '{safe_id}'
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY metric ORDER BY checked_at DESC
                ) = 1
                ORDER BY metric
            """)
        except Exception:
            logger.debug("dq_status unavailable for %s", resource_id, exc_info=True)

        return ResourceDetail(
            **inv[0],
            classifications=classifications,
            violations=violations,
            exceptions=exceptions,
            metrics=metrics,
        )

    # ── Policies ──────────────────────────────────────────────────────────

    def list_policies(self, filters: PolicyFilters) -> list[Policy]:
        conditions = []
        if filters.origin:
            conditions.append(f"origin = '{self._esc(filters.origin)}'")
        if filters.active is not None:
            conditions.append(f"active = {str(filters.active).lower()}")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        rows = self._execute(f"""
            SELECT policy_id, policy_name, applies_to, domain, severity,
                   description, remediation, active, rule_json, origin,
                   CAST(updated_at AS STRING) AS updated_at
            FROM {self._tbl('policies')}
            {where}
            ORDER BY domain, severity DESC, policy_id
        """)
        return [Policy(**r) for r in rows]

    def get_policy(self, policy_id: str) -> Policy:
        rows = self._execute(f"""
            SELECT policy_id, policy_name, applies_to, domain, severity,
                   description, remediation, active, rule_json, origin,
                   CAST(updated_at AS STRING) AS updated_at
            FROM {self._tbl('policies')}
            WHERE policy_id = '{self._esc(policy_id)}'
        """)
        if not rows:
            raise LookupError(f"Policy {policy_id} not found")
        return Policy(**rows[0])

    def create_policy(
        self,
        body: PolicyBase,
        *,
        policy_id: str | None = None,
        author: str = "unknown",
    ) -> Policy:
        pid = policy_id or f"POL-U-{str(uuid.uuid4())[:8].upper()}"

        existing = self._execute(f"""
            SELECT policy_id, origin FROM {self._tbl('policies')}
            WHERE policy_id = '{self._esc(pid)}'
        """)
        if existing:
            raise ValueError(
                f"Policy {pid} already exists (origin={existing[0]['origin']}). "
                "Use update_policy to modify."
            )

        self._execute_write(f"""
            INSERT INTO {self._tbl('policies')}
            (policy_id, policy_name, applies_to, domain, severity, description,
             remediation, active, rule_json, source_file, origin, updated_at)
            VALUES (
                '{self._esc(pid)}', '{self._esc(body.policy_name)}',
                '{self._esc(body.applies_to)}', '{self._esc(body.domain)}',
                '{self._esc(body.severity)}',
                '{self._esc(body.description)}',
                '{self._esc(body.remediation)}',
                {str(body.active).lower()},
                '{self._esc(body.rule_json)}',
                'user', 'user', current_timestamp()
            )
        """)

        self._execute_write(f"""
            INSERT INTO {self._tbl('policies_history')}
            (policy_id, version, policy_name, applies_to, domain, severity,
             description, remediation, rule_json, active, origin,
             change_type, changed_by, changed_at)
            VALUES (
                '{self._esc(pid)}', 1, '{self._esc(body.policy_name)}',
                '{self._esc(body.applies_to)}', '{self._esc(body.domain)}',
                '{self._esc(body.severity)}',
                '{self._esc(body.description)}',
                '{self._esc(body.remediation)}',
                '{self._esc(body.rule_json)}',
                {str(body.active).lower()}, 'user', 'created',
                '{self._esc(author)}', current_timestamp()
            )
        """)

        return self.get_policy(pid)

    def update_policy(
        self,
        policy_id: str,
        body: PolicyBase,
        *,
        author: str = "unknown",
    ) -> Policy:
        safe_pid = self._esc(policy_id)
        existing = self._execute(f"""
            SELECT origin FROM {self._tbl('policies')}
            WHERE policy_id = '{safe_pid}'
        """)
        if not existing:
            raise LookupError(f"Policy {policy_id} not found")
        if existing[0]["origin"] == "yaml":
            raise PermissionError(
                "YAML-origin policies cannot be edited via the API. "
                "Edit the policy YAML in git and redeploy, or create a new user policy."
            )

        self._execute_write(f"""
            UPDATE {self._tbl('policies')} SET
                policy_name = '{self._esc(body.policy_name)}',
                applies_to  = '{self._esc(body.applies_to)}',
                domain      = '{self._esc(body.domain)}',
                severity    = '{self._esc(body.severity)}',
                description = '{self._esc(body.description)}',
                remediation = '{self._esc(body.remediation)}',
                active      = {str(body.active).lower()},
                rule_json   = '{self._esc(body.rule_json)}',
                updated_at  = current_timestamp()
            WHERE policy_id = '{safe_pid}' AND origin = 'user'
        """)

        version_row = self._execute(f"""
            SELECT COALESCE(MAX(version), 0) + 1 AS next_v
            FROM {self._tbl('policies_history')}
            WHERE policy_id = '{safe_pid}'
        """)
        next_v = version_row[0]["next_v"] if version_row else 1

        self._execute_write(f"""
            INSERT INTO {self._tbl('policies_history')}
            (policy_id, version, policy_name, applies_to, domain, severity,
             description, remediation, rule_json, active, origin,
             change_type, changed_by, changed_at)
            VALUES (
                '{safe_pid}', {next_v}, '{self._esc(body.policy_name)}',
                '{self._esc(body.applies_to)}', '{self._esc(body.domain)}',
                '{self._esc(body.severity)}',
                '{self._esc(body.description)}',
                '{self._esc(body.remediation)}',
                '{self._esc(body.rule_json)}',
                {str(body.active).lower()}, 'user', 'updated',
                '{self._esc(author)}', current_timestamp()
            )
        """)

        return self.get_policy(policy_id)

    def policy_history(self, policy_id: str) -> list[PolicyVersion]:
        rows = self._execute(f"""
            SELECT version, policy_name, applies_to, severity, active,
                   rule_json, change_type, changed_by,
                   CAST(changed_at AS STRING) AS changed_at
            FROM {self._tbl('policies_history')}
            WHERE policy_id = '{self._esc(policy_id)}'
            ORDER BY version DESC
        """)
        return [PolicyVersion(**r) for r in rows]

    def list_applies_to_classes(self) -> list[str]:
        rows = self._execute(f"""
            SELECT DISTINCT applies_to
            FROM {self._tbl('policies')}
            WHERE applies_to IS NOT NULL
            ORDER BY 1
        """)
        return [r["applies_to"] for r in rows]

    # ── Exceptions ────────────────────────────────────────────────────────

    def list_exceptions(self, filters: ExceptionFilters) -> list[ExceptionRecord]:
        conditions = [f"active = {str(filters.active).lower()}"]
        if filters.expiring_soon:
            conditions.append(
                "expires_at BETWEEN current_timestamp() "
                "AND current_timestamp() + INTERVAL 30 DAY"
            )
        if filters.resource_id:
            conditions.append(f"resource_id = '{self._esc(filters.resource_id)}'")
        where = "WHERE " + " AND ".join(conditions)

        rows = self._execute(f"""
            SELECT exception_id, resource_id, policy_id, approved_by,
                   justification,
                   CAST(approved_at AS STRING) AS approved_at,
                   CAST(expires_at  AS STRING) AS expires_at,
                   active,
                   CASE
                       WHEN expires_at IS NULL THEN 'permanent'
                       WHEN expires_at < current_timestamp() THEN 'expired'
                       WHEN expires_at < current_timestamp() + INTERVAL 30 DAY THEN 'expiring_soon'
                       ELSE 'active'
                   END AS expiry_status
            FROM {self._tbl('exceptions')}
            {where}
            ORDER BY
                CASE WHEN expires_at < current_timestamp() THEN 0
                     WHEN expires_at < current_timestamp() + INTERVAL 30 DAY THEN 1
                     ELSE 2 END,
                expires_at ASC NULLS LAST
        """)
        return [ExceptionRecord(**r) for r in rows]

    def exceptions_summary(self) -> ExceptionSummary:
        rows = self._execute(f"""
            SELECT
                COUNT(*)                                                                    AS total,
                SUM(CASE WHEN active THEN 1 ELSE 0 END)                                    AS active,
                SUM(CASE WHEN active AND expires_at IS NULL THEN 1 ELSE 0 END)             AS permanent,
                SUM(CASE WHEN active AND expires_at < current_timestamp() THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN active
                          AND expires_at BETWEEN current_timestamp()
                              AND current_timestamp() + INTERVAL 30 DAY
                          THEN 1 ELSE 0 END)                                                AS expiring_soon
            FROM {self._tbl('exceptions')}
        """)
        return ExceptionSummary(**(rows[0] if rows else {}))

    def exceptions_for_resource(self, resource_id: str) -> list[ExceptionRecord]:
        rows = self._execute(f"""
            SELECT exception_id, policy_id, approved_by, justification,
                   CAST(approved_at AS STRING) AS approved_at,
                   CAST(expires_at  AS STRING) AS expires_at,
                   active,
                   CASE
                       WHEN expires_at IS NULL THEN 'permanent'
                       WHEN expires_at < current_timestamp() THEN 'expired'
                       WHEN expires_at < current_timestamp() + INTERVAL 30 DAY THEN 'expiring_soon'
                       ELSE 'active'
                   END AS expiry_status
            FROM {self._tbl('exceptions')}
            WHERE resource_id = '{self._esc(resource_id)}'
            ORDER BY approved_at DESC
        """)
        return [ExceptionRecord(**r) for r in rows]

    def approve_exceptions(
        self,
        resource_id: str,
        policy_ids: list[str],
        justification: str,
        expires_days: int | None,
        *,
        approved_by: str = "unknown",
    ) -> dict:
        expires_expr = (
            "NULL"
            if expires_days is None
            else f"current_timestamp() + INTERVAL {expires_days} DAY"
        )
        approved = []
        for pid in policy_ids:
            eid = str(uuid.uuid4())
            self._execute_write(f"""
                INSERT INTO {self._tbl('exceptions')}
                (exception_id, resource_id, policy_id, approved_by,
                 justification, approved_at, expires_at, active)
                VALUES (
                    '{eid}',
                    '{self._esc(resource_id)}',
                    '{self._esc(pid)}',
                    '{self._esc(approved_by)}',
                    '{self._esc(justification)}',
                    current_timestamp(),
                    {expires_expr},
                    true
                )
            """)
            approved.append({"exception_id": eid, "policy_id": pid})

        return {
            "approved": approved,
            "resource_id": resource_id,
            "approved_by": approved_by,
            "expires_days": expires_days,
        }

    def revoke_exception(self, exception_id: str, *, revoked_by: str = "unknown") -> dict:
        safe_eid = self._esc(exception_id)
        rows = self._execute(f"""
            SELECT exception_id, resource_id, policy_id, active
            FROM {self._tbl('exceptions')}
            WHERE exception_id = '{safe_eid}'
        """)
        if not rows:
            raise LookupError(f"Exception {exception_id} not found")
        if not rows[0]["active"]:
            raise ValueError("Exception is already inactive")

        self._execute_write(f"""
            UPDATE {self._tbl('exceptions')}
            SET active = false
            WHERE exception_id = '{safe_eid}'
        """)
        return {
            "revoked": exception_id,
            "resource_id": rows[0]["resource_id"],
            "policy_id": rows[0]["policy_id"],
            "revoked_by": revoked_by,
        }

    def bulk_revoke_expired(self, *, revoked_by: str = "unknown") -> dict:
        expired = self._execute(f"""
            SELECT exception_id, resource_id, policy_id
            FROM {self._tbl('exceptions')}
            WHERE active = true AND expires_at < current_timestamp()
        """)
        if not expired:
            return {"revoked": 0, "exceptions": []}

        self._execute_write(f"""
            UPDATE {self._tbl('exceptions')}
            SET active = false
            WHERE active = true AND expires_at < current_timestamp()
        """)
        return {
            "revoked": len(expired),
            "revoked_by": revoked_by,
            "exceptions": [
                {"exception_id": r["exception_id"], "policy_id": r["policy_id"]}
                for r in expired
            ],
        }

    # ── Grants ────────────────────────────────────────────────────────────

    def list_grants(
        self,
        resource_id: str | None = None,
        grantee: str | None = None,
    ) -> list[Grant]:
        conditions = ["resource_type = 'grant'"]
        conditions.append(f"""
            scan_id = (
                SELECT MAX(scan_id) FROM {self._tbl('resource_inventory')}
            )
        """)
        if resource_id:
            conditions.append(
                f"metadata['securable_full_name'] = '{self._esc(resource_id)}'"
            )
        if grantee:
            conditions.append(f"metadata['grantee'] = '{self._esc(grantee)}'")
        where = "WHERE " + " AND ".join(conditions)

        rows = self._execute(f"""
            SELECT
                resource_id,
                metadata['securable_type']     AS securable_type,
                metadata['securable_full_name'] AS securable_full_name,
                metadata['grantee']            AS grantee,
                metadata['privilege']          AS privilege,
                metadata['grantor']            AS grantor,
                metadata['inherited_from']     AS inherited_from
            FROM {self._tbl('resource_inventory')}
            {where}
            ORDER BY resource_id
        """)
        return [Grant(**r) for r in rows]

    def grant_summary(self, resource_id: str) -> GrantSummary:
        safe_id = self._esc(resource_id)
        rows = self._execute(f"""
            SELECT
                metadata['privilege']          AS privilege,
                metadata['grantee']            AS grantee
            FROM {self._tbl('resource_inventory')}
            WHERE resource_type = 'grant'
              AND scan_id = (
                  SELECT MAX(scan_id) FROM {self._tbl('resource_inventory')}
              )
              AND metadata['securable_full_name'] = '{safe_id}'
        """)

        grants_by_privilege: dict[str, int] = {}
        for r in rows:
            priv = r.get("privilege", "unknown")
            grants_by_privilege[priv] = grants_by_privilege.get(priv, 0) + 1

        # Count overprivileged (ALL PRIVILEGES) and direct user grants
        overprivileged_count = sum(
            1 for r in rows
            if r.get("privilege", "").upper() == "ALL PRIVILEGES"
        )

        # Check violations for this resource to count direct user grants
        violation_rows = self._execute(f"""
            SELECT policy_id
            FROM {self._tbl('violations')}
            WHERE active = true
              AND resource_id IN (
                  SELECT resource_id
                  FROM {self._tbl('resource_inventory')}
                  WHERE resource_type = 'grant'
                    AND metadata['securable_full_name'] = '{safe_id}'
              )
              AND policy_id = 'POL-A002'
        """)
        direct_user_grant_count = len(violation_rows)

        return GrantSummary(
            resource_id=resource_id,
            total_grants=len(rows),
            grants_by_privilege=grants_by_privilege,
            overprivileged_count=overprivileged_count,
            direct_user_grant_count=direct_user_grant_count,
        )

    # ── Ontology ──────────────────────────────────────────────────────────

    def _ontology_path(self) -> Path:
        if self._ontology_dir:
            return self._ontology_dir
        env = os.environ.get("GOVERNANCE_ONTOLOGY_DIR")
        if env:
            return Path(env)
        raise FileNotFoundError(
            "Ontology directory not configured. Set GOVERNANCE_ONTOLOGY_DIR "
            "or pass ontology_dir to the constructor."
        )

    def _load_classes(self) -> dict[str, dict[str, Any]]:
        path = self._ontology_path() / "resource_classes.yml"
        if not path.exists():
            raise FileNotFoundError(f"Ontology file not found at {path}")

        with path.open() as f:
            raw = yaml.safe_load(f)

        classes: dict[str, dict[str, Any]] = {}

        for name, defn in (raw.get("base_classes") or {}).items():
            classes[name] = {
                "name": name,
                "kind": "base",
                "parent": None,
                "description": defn.get("description", ""),
                "matches_resource_types": defn.get("matches_resource_types", []),
                "classifier": None,
            }

        for name, defn in (raw.get("derived_classes") or {}).items():
            classes[name] = {
                "name": name,
                "kind": "derived",
                "parent": defn.get("parent"),
                "description": defn.get("description", ""),
                "matches_resource_types": [],
                "classifier": defn.get("classifier"),
            }

        return classes

    def _ancestry(self, name: str, classes: dict) -> list[str]:
        chain = []
        current = name
        seen: set[str] = set()
        while current and current not in seen:
            chain.append(current)
            seen.add(current)
            current = classes.get(current, {}).get("parent")
        return chain

    def _children_map(self, classes: dict) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {n: [] for n in classes}
        for name, defn in classes.items():
            if defn["parent"] and defn["parent"] in children:
                children[defn["parent"]].append(name)
        return children

    def list_ontology_classes(self, *, kind: str | None = None) -> list[OntologyClass]:
        classes = self._load_classes()
        children_map = self._children_map(classes)

        result = []
        for name, defn in sorted(classes.items()):
            if kind and defn["kind"] != kind:
                continue
            result.append(OntologyClass(
                **defn,
                ancestry=self._ancestry(name, classes),
                children=sorted(children_map.get(name, [])),
            ))
        return result

    def get_ontology_class(self, class_name: str) -> OntologyClass:
        classes = self._load_classes()
        if class_name not in classes:
            raise LookupError(f"Class '{class_name}' not found")

        children_map = self._children_map(classes)
        defn = classes[class_name]
        return OntologyClass(
            **defn,
            ancestry=self._ancestry(class_name, classes),
            children=sorted(children_map.get(class_name, [])),
        )

    def ontology_tree(self) -> OntologyTree:
        classes = self._load_classes()
        children_map = self._children_map(classes)

        def _node(name: str) -> OntologyTreeNode:
            d = classes[name]
            return OntologyTreeNode(
                name=name,
                kind=d["kind"],
                description=d["description"],
                children=sorted(
                    [_node(c) for c in children_map.get(name, [])],
                    key=lambda x: x.name,
                ),
            )

        roots = [n for n, d in classes.items() if d["parent"] is None]
        return OntologyTree(
            roots=sorted([_node(r) for r in roots], key=lambda x: x.name),
            total_classes=len(classes),
        )

    def validate_ontology(self) -> ValidationResult:
        path = self._ontology_path() / "resource_classes.yml"
        if not path.exists():
            return ValidationResult(
                valid=False,
                errors=[f"Ontology file not found at {path}"],
                warnings=[],
            )

        try:
            with path.open() as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            return ValidationResult(
                valid=False, errors=[f"YAML parse error: {e}"], warnings=[]
            )

        errors: list[str] = []
        warnings: list[str] = []

        base = raw.get("base_classes") or {}
        derived = raw.get("derived_classes") or {}
        all_names = set(base) | set(derived)

        overlap = set(base) & set(derived)
        for name in overlap:
            errors.append(
                f"Class '{name}' appears in both base_classes and derived_classes"
            )

        valid_operators = {
            "tag_equals", "tag_in", "tag_exists", "tag_matches",
            "all_of", "any_of", "none_of",
            "metadata_equals", "metadata_matches",
        }

        for name, defn in derived.items():
            parent = defn.get("parent")
            if parent and parent not in all_names:
                errors.append(f"Class '{name}' references unknown parent '{parent}'")

            classifier = defn.get("classifier")
            if not classifier:
                if parent is None:
                    errors.append(f"Class '{name}' has no parent and no classifier")
                else:
                    warnings.append(
                        f"Derived class '{name}' has no classifier (will never match)"
                    )
            elif isinstance(classifier, dict):
                for op in classifier:
                    if op not in valid_operators:
                        warnings.append(
                            f"Class '{name}' uses unknown classifier operator '{op}'"
                        )

        def _has_cycle(name: str, visited: set) -> bool:
            if name in visited:
                return True
            visited.add(name)
            parent = derived.get(name, {}).get("parent")
            if parent:
                return _has_cycle(parent, visited)
            return False

        for name in derived:
            if _has_cycle(name, set()):
                errors.append(f"Class '{name}' is in a circular inheritance chain")

        return ValidationResult(
            valid=len(errors) == 0, errors=errors, warnings=warnings
        )

    # ── Remediation ──────────────────────────────────────────────────────

    def remediation_funnel(self) -> dict:
        rows = self._execute(
            f"SELECT * FROM {self._tbl('v_remediation_funnel')}"
        )
        if not rows:
            return {
                "total_violations": 0, "with_remediation": 0,
                "pending_review": 0, "approved": 0, "applied": 0,
                "verified": 0, "verification_failed": 0, "rejected": 0,
            }
        return rows[0]

    def agent_effectiveness(self) -> list[dict]:
        return self._execute(
            f"SELECT * FROM {self._tbl('v_agent_effectiveness')}"
        )

    def reviewer_load(self) -> list[dict]:
        return self._execute(
            f"SELECT * FROM {self._tbl('v_reviewer_load')}"
        )

    def list_proposals(self, filters) -> list[dict]:
        status_clause = f"AND p.status = '{self._esc(filters.status)}'" if filters.status else ""
        return self._execute(f"""
            SELECT
                p.proposal_id,
                p.violation_id,
                v.resource_id,
                COALESCE(v.resource_name, v.resource_id) AS resource_name,
                v.resource_type,
                v.policy_id,
                pol.policy_name,
                pol.severity,
                pol.domain,
                p.agent_id,
                p.agent_version,
                p.status,
                p.confidence,
                p.proposed_sql,
                p.created_at
            FROM {self._tbl('remediation_proposals')} p
            JOIN {self._tbl('violations')} v
                ON p.violation_id = v.violation_id
            JOIN {self._tbl('policies')} pol
                ON v.policy_id = pol.policy_id
            WHERE 1=1 {status_clause}
            ORDER BY
                CASE pol.severity
                    WHEN 'critical' THEN 1
                    WHEN 'high' THEN 2
                    WHEN 'medium' THEN 3
                    WHEN 'low' THEN 4
                END ASC,
                p.confidence ASC
            LIMIT {filters.limit} OFFSET {filters.offset}
        """)

    def get_proposal(self, proposal_id: str) -> dict:
        rows = self._execute(f"""
            SELECT
                p.proposal_id,
                p.violation_id,
                v.resource_id,
                COALESCE(v.resource_name, v.resource_id) AS resource_name,
                v.resource_type,
                v.policy_id,
                pol.policy_name,
                pol.severity,
                pol.domain,
                p.agent_id,
                p.agent_version,
                p.status,
                p.confidence,
                p.proposed_sql,
                p.created_at,
                p.context_json,
                p.citations,
                '{{}}' AS pre_state
            FROM {self._tbl('remediation_proposals')} p
            JOIN {self._tbl('violations')} v
                ON p.violation_id = v.violation_id
            JOIN {self._tbl('policies')} pol
                ON v.policy_id = pol.policy_id
            WHERE p.proposal_id = '{self._esc(proposal_id)}'
        """)
        if not rows:
            raise LookupError(f"Proposal {proposal_id} not found")

        proposal = rows[0]

        # Fetch review history
        reviews = self._execute(f"""
            SELECT
                review_id, proposal_id, reviewer, decision,
                reasoning, reassigned_to, reviewed_at
            FROM {self._tbl('remediation_reviews')}
            WHERE proposal_id = '{self._esc(proposal_id)}'
            ORDER BY reviewed_at ASC
        """)
        proposal["review_history"] = reviews
        proposal["proposed_state"] = _parse_proposed_state(proposal.get("proposed_sql", ""))
        return proposal

    def submit_review(
        self,
        proposal_id: str,
        decision: str,
        reasoning: str,
        *,
        reassigned_to: str | None = None,
        reviewer: str = "unknown",
    ) -> dict:
        from governance.remediation.review import (
            approve_proposal,
            reassign_proposal,
            reject_proposal,
        )

        # Fetch current proposal
        rows = self._execute(f"""
            SELECT * FROM {self._tbl('remediation_proposals')}
            WHERE proposal_id = '{self._esc(proposal_id)}'
        """)
        if not rows:
            raise LookupError(f"Proposal {proposal_id} not found")

        proposal = rows[0]

        if decision == "approved":
            updated, review = approve_proposal(proposal, reviewer, reasoning)
        elif decision == "rejected":
            updated, review = reject_proposal(proposal, reviewer, reasoning)
        elif decision == "reassigned":
            if not reassigned_to:
                raise ValueError("reassigned_to is required for reassign")
            updated, review = reassign_proposal(
                proposal, reviewer, reassigned_to, reasoning
            )
        else:
            raise ValueError(f"Invalid decision: {decision}")

        # Write updated proposal status
        self._execute_write(f"""
            UPDATE {self._tbl('remediation_proposals')}
            SET status = '{self._esc(updated["status"])}'
            WHERE proposal_id = '{self._esc(proposal_id)}'
        """)

        # Write review record
        self._execute_write(f"""
            INSERT INTO {self._tbl('remediation_reviews')}
            (review_id, proposal_id, reviewer, decision, reasoning,
             reassigned_to, reviewed_at)
            VALUES (
                '{self._esc(review["review_id"])}',
                '{self._esc(review["proposal_id"])}',
                '{self._esc(review["reviewer"])}',
                '{self._esc(review["decision"])}',
                '{self._esc(review["reasoning"])}',
                {f"'{self._esc(review['reassigned_to'])}'" if review["reassigned_to"] else "NULL"},
                '{review["reviewed_at"].isoformat()}'
            )
        """)

        return {
            "review_id": review["review_id"],
            "proposal_id": proposal_id,
            "decision": decision,
            "status": updated["status"],
        }
