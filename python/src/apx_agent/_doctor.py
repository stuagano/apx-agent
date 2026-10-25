"""Environment diagnostics for the apx CLI.

The *facts* layer behind `apx-agent doctor` and the inline preflights in cli.py.
Each `check_*` function inspects one thing and returns a `Check`. cli.py owns
presentation; this module owns what's wrong and how to fix it. References to
cli helpers (`_detect_target`, `_databrickscfg_profiles`) are lazy imports so
this module has no import-time dependency on cli (cli imports this module).
"""

from __future__ import annotations

import enum
import importlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ._models import AgentConfig

MIN_PYTHON = (3, 11)

_INDEX_ENV_VARS = ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "PIP_INDEX_URL")

# The only hosts a public-PyPI uv.lock resolves packages from. Anything else
# in an index (`registry = ...`) or package-download (`url = ...`) entry is a
# mirror `cli._sanitize_uv_lock` has no rewrite rule for (#416).
_PUBLIC_PYPI_HOSTS = frozenset({"pypi.org", "files.pythonhosted.org"})
_LOCK_URL_RE = re.compile(r'(?:registry|url) = "https://([^/"]+)')
_PYPI_PROXY_HOST_RE = re.compile(
    r"pypi-proxy\.[A-Za-z0-9-]+\.databricks\.com"
)


def _is_nonpublic_index(value: str | None) -> bool:
    if not value:
        return False
    return urlparse(value).hostname not in _PUBLIC_PYPI_HOSTS


def nonpublic_lock_hosts(lock_text: str) -> list[str]:
    """Index/download hosts in a uv.lock that are not public PyPI, sorted.

    Only ``registry = "https://..."`` and ``url = "https://..."`` entries are
    scanned — git sources (``git = "https://..."``) are not package indexes and
    stay out of scope. Shared by ``cli._warn_unknown_lock_mirrors`` and
    ``check_pypi_index``.
    """
    return sorted(
        {h for h in _LOCK_URL_RE.findall(lock_text) if h not in _PUBLIC_PYPI_HOSTS}
    )


class Status(enum.Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class Check:
    """One diagnostic result. `fix` is a copy-pasteable next step or None."""

    name: str
    status: Status
    detail: str
    fix: str | None = None


# ---------------------------------------------------------------------------
# Sub-agent reachability probe (issue #445)
#
# Lives here — not in _ui_probe — so the operate path (doctor, `agents
# status`, /readyz) shares one probe without importing the dev-UI modules.
# Mirrors _ui_probe._check_sub_agent: GET each declared sub-agent's
# /.well-known/agent.json, concurrently, with a short per-request timeout.
# ---------------------------------------------------------------------------

_SUB_AGENT_PROBE_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class SubAgentProbe:
    """Reachability of one declared sub-agent URL."""

    url: str  # as declared in config (may be a $VAR / ${VAR} env ref)
    reachable: bool
    name: str | None = None  # agent-card name when reachable
    error: str | None = None  # short reason when unreachable

    def as_dict(self) -> dict[str, str | bool]:
        """JSON shape shared by `agents status --json` and /readyz."""
        payload: dict[str, str | bool] = {"url": self.url, "reachable": self.reachable}
        if self.name is not None:
            payload["name"] = self.name
        if self.error is not None:
            payload["error"] = self.error
        return payload


def probe_sub_agents(
    sub_agents: list[str],
    *,
    timeout_s: float = _SUB_AGENT_PROBE_TIMEOUT_S,
    auth_headers: dict[str, str] | None = None,
) -> list[SubAgentProbe]:
    """Fetch each declared sub-agent's ``/.well-known/agent.json`` card.

    ``$VAR`` / ``${VAR}`` refs resolve the same way the runtime wiring does;
    an unset ref reports as unreachable rather than raising. URLs are probed
    concurrently with a short per-request timeout, so total wall time is
    roughly one ``timeout_s`` — callers (doctor, ``agents status``,
    ``/readyz``) stay snappy.
    """
    import asyncio

    import httpx

    from ._env import resolve_env_var

    async def _probe_one(client: httpx.AsyncClient, raw: str) -> SubAgentProbe:
        url = resolve_env_var(raw)
        if not url:
            return SubAgentProbe(
                url=raw,
                reachable=False,
                error="env ref resolved to empty — variable unset",
            )
        card_url = f"{url.rstrip('/')}/.well-known/agent.json"
        try:
            resp = await client.get(card_url, headers=auth_headers)
        except Exception as exc:
            return SubAgentProbe(url=raw, reachable=False, error=str(exc)[:160])
        if resp.status_code != 200:
            return SubAgentProbe(
                url=raw, reachable=False, error=f"HTTP {resp.status_code} from {card_url}"
            )
        try:
            card = resp.json()
        except Exception:
            card = None
        card_name = card.get("name") if isinstance(card, dict) else None
        return SubAgentProbe(
            url=raw,
            reachable=True,
            name=card_name if isinstance(card_name, str) else "",
        )

    async def _gather() -> list[SubAgentProbe]:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            return list(
                await asyncio.gather(*[_probe_one(client, raw) for raw in sub_agents])
            )

    return asyncio.run(_gather())


def run_checks(cwd: Path, *, online: bool, durable: bool = False) -> list[tuple[str, list[Check]]]:
    """Run all checks, grouped and ordered for presentation.

    `online=True` adds the live workspace round-trip; it is skipped when auth
    can't even be resolved (nothing to live-test).
    """
    environment = [
        check_python_version(),
        check_apx_install(),
        check_uv(),
        check_pypi_index(cwd),
        check_databricks_cli(),
        check_uvicorn(),
    ]
    auth = check_databricks_auth()
    authentication = [auth]
    federation = check_apps_m2m_federation()
    if federation is not None:
        authentication.append(federation)
    if online:
        authentication.append(
            check_databricks_workspace(auth_ok=auth.status is Status.OK)
        )
        model_check = check_model_endpoint(cwd, auth_ok=auth.status is Status.OK)
        if model_check is not None:
            authentication.append(model_check)
        guardrails_check = check_gateway_guardrails(cwd, auth_ok=auth.status is Status.OK)
        if guardrails_check is not None:
            authentication.append(guardrails_check)
        apps_check = check_apps_enabled(auth_ok=auth.status is Status.OK)
        if apps_check is not None:
            authentication.append(apps_check)
    project = [
        check_project_layout(cwd),
        check_target(cwd),
        check_extras(cwd),
        check_databricks_yml(cwd),
    ]
    from ._inspection import _load_agent_config

    try:
        agent_config = _load_agent_config(pyproject_path=cwd / "pyproject.toml")
    except (ValueError, OSError) as exc:
        project.append(Check("Agent prerequisites", Status.FAIL, type(exc).__name__, "Fix the agent configuration."))
    else:
        project.extend(check_agent_prerequisites(agent_config, online=online and auth.status is Status.OK))
        image_check = check_agent_image(cwd, agent_config)
        if image_check is not None:
            project.append(image_check)
        if agent_config is None or agent_config.target == "durable_agent_server":
            project.extend(_agent_toml_rows(cwd, agent_config))
    memory_check = check_memory_backend(cwd, auth_ok=auth.status is Status.OK)
    if memory_check is not None:
        project.append(memory_check)
    abac_check = check_abac_compute_floor(cwd, auth_ok=auth.status is Status.OK)
    if abac_check is not None:
        project.append(abac_check)
    uc_check = check_uc_data_source(cwd, auth_ok=auth.status is Status.OK)
    if uc_check is not None:
        project.append(uc_check)
    provenance_check = check_deploy_provenance(
        cwd, auth_ok=auth.status is Status.OK
    )
    if provenance_check is not None:
        project.append(provenance_check)
    project.extend(check_declared_tools(cwd, auth_ok=auth.status is Status.OK, online=online))
    sub_agents_check = check_sub_agents(cwd)
    if sub_agents_check is not None:
        project.append(sub_agents_check)
    a2a_trust_check = check_a2a_trust(cwd)
    if a2a_trust_check is not None:
        project.append(a2a_trust_check)
    groups = [
        ("Environment", environment),
        ("Authentication", authentication),
        ("Project", project),
    ]
    if durable:
        from ._doctor_durable import check_durable_readiness

        groups.append(("Durable readiness", check_durable_readiness(cwd)))
    return groups


def _agent_toml_rows(cwd: Path, config: AgentConfig | None) -> list[Check]:
    """agent.toml findings, loading the agent and plan the way deploy does."""
    from contextlib import chdir  # noqa: PLC0415

    from . import _apps_authorization  # noqa: PLC0415
    from .cli import _detect_module_spec, _load_finalized_agent  # noqa: PLC0415

    try:
        with chdir(cwd):
            agent = _load_finalized_agent(_detect_module_spec(cwd) or "agent:agent", template_ws=False)
    except Exception as exc:  # reported, not raised: doctor keeps going
        return [Check("agent.toml", Status.SKIP, f"agent not importable: {type(exc).__name__}: {exc}")]
    try:
        from ._agent_toml import check_agent_toml  # noqa: PLC0415

        model = config.model if config is not None else AgentConfig.model_fields["model"].default
        identity = config.identity if config is not None else None
        plan = _apps_authorization.compile_authorization_plan(
            agent, model=model, agent_identity=identity)
        return check_agent_toml(cwd, agent, plan, config)
    except ImportError as exc:
        return [Check("agent.toml", Status.SKIP, f"needs the apx-agent[agentbricks] extra: {exc}",
                      "Install apx-agent[agentbricks] to check agent.toml.")]
    except ValueError as exc:
        return [Check("agent.toml", Status.FAIL, str(exc))]


def check_python_version() -> Check:
    major, minor, micro = sys.version_info[:3]
    version = f"{major}.{minor}.{micro}"
    if (major, minor) >= MIN_PYTHON:
        return Check("Python", Status.OK, version, None)
    return Check(
        "Python",
        Status.FAIL,
        f"{version} — apx-agent requires Python >= "
        f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        f"Install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
        "(e.g. `uv python install 3.12`) and recreate the venv.",
    )


def check_apx_install() -> Check:
    try:
        version = importlib.metadata.version("apx-agent")
        return Check("apx-agent install", Status.OK, f"version {version}", None)
    except importlib.metadata.PackageNotFoundError:
        return Check(
            "apx-agent install", Status.OK, "dev (editable install)", None
        )


def check_uv() -> Check:
    if shutil.which("uv"):
        return Check("uv", Status.OK, "found", None)
    return Check(
        "uv",
        Status.WARN,
        "not found — used by `uv sync` and the scaffold dev loop",
        "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh",
    )


def check_pypi_index(cwd: Path) -> Check:
    """Flag non-public package hosts in uv.lock or an index env var.

    A `uv.lock` pinned to a recognized organization proxy fails ``uv sync``
    for external users and deployed Apps (the blocking case) — FAIL, and
    ``apx-agent deploy`` sanitizes it. Any *other* non-public registry/download
    host may be reachable for the target environment, so it is a WARN. A
    non-public index environment variable is also a WARN: local proxy access
    is valid, but the committed lock must remain portable.
    """
    lock = cwd / "uv.lock"
    try:
        if lock.exists():
            lock_text = lock.read_text()
            lock_hosts = nonpublic_lock_hosts(lock_text)
            if any(_PYPI_PROXY_HOST_RE.fullmatch(host) for host in lock_hosts):
                return Check(
                    "PyPI index",
                    Status.FAIL,
                    "uv.lock pins packages to a recognized internal proxy — "
                    "the endpoint is intentionally omitted; it is unreachable for "
                    "external users and "
                    "deployed Apps, so `uv sync`/deploy will fail off the corp network",
                    "Use the organization-approved proxy only for local resolution "
                    "and keep the committed uv.lock on public PyPI (`apx-agent deploy` "
                    "also sanitizes recognized mirrors before bundling).",
                )
            if lock_hosts:
                return Check(
                    "PyPI index",
                    Status.WARN,
                    "uv.lock resolves packages from non-public host(s): "
                    f"{', '.join(lock_hosts)} — a deployed container's `uv sync` "
                    "will fail unless it can reach them, and `apx-agent deploy` "
                    "only rewrites recognized mirrors",
                    "Re-point the lock at public PyPI: unset any custom index "
                    "env vars, then `uv lock` to re-resolve.",
                )
    except OSError:
        pass

    nonpublic_env = next(
        (
            variable for variable in _INDEX_ENV_VARS
            if _is_nonpublic_index(os.environ.get(variable))
        ),
        None,
    )
    if nonpublic_env:
        return Check(
            "PyPI index",
            Status.WARN,
            f"{nonpublic_env} points at a non-public package index — valid for "
            "local access, but committed uv.lock files must remain on public PyPI",
            f"Use {nonpublic_env} only for local resolution; do not commit its "
            "endpoint in pyproject.toml or uv.lock.",
        )

    return Check("PyPI index", Status.OK, "public PyPI", None)


def check_databricks_cli() -> Check:
    path = shutil.which("databricks")
    if not path:
        return Check(
            "Databricks CLI",
            Status.WARN,
            "not found — needed for `apx-agent deploy`",
            "brew install databricks/tap/databricks  "
            "(or see docs.databricks.com/dev-tools/cli)",
        )
    try:
        out = subprocess.run(
            ["databricks", "--version"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        detail = (out.stdout or out.stderr or "found").strip().splitlines()[0]
    except (subprocess.SubprocessError, OSError):
        detail = "found"
    return Check("Databricks CLI", Status.OK, detail, None)


def check_uvicorn() -> Check:
    try:
        importlib.import_module("uvicorn")
        return Check("uvicorn", Status.OK, "installed", None)
    except ImportError:
        return Check(
            "uvicorn",
            Status.WARN,
            "not importable — required by `apx-agent run`",
            "uv add 'uvicorn[standard]'",
        )


def check_databricks_auth() -> Check:
    """Confirm a Databricks Config can be constructed (offline, no API call)."""
    try:
        from databricks.sdk.core import Config  # noqa: F401
    except Exception as e:  # SDK missing in a minimal install
        return Check(
            "Databricks auth",
            Status.FAIL,
            f"databricks-sdk not importable: {e}",
            "uv add databricks-sdk  (normally pulled in by apx-agent)",
        )

    from apx_agent.cli import _databrickscfg_profiles

    env_profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if os.environ.get("DATABRICKS_HOST") and (
        os.environ.get("DATABRICKS_TOKEN")
        or os.environ.get("DATABRICKS_CLIENT_ID")
    ):
        return Check("Databricks auth", Status.OK, "credentials resolved from env", None)
    profiles = _databrickscfg_profiles()
    if env_profile:
        if not profiles or env_profile in profiles:
            return Check(
                "Databricks auth",
                Status.OK,
                f"credentials resolved from profile {env_profile}",
                None,
            )
        return Check(
            "Databricks auth",
            Status.FAIL,
            f"profile {env_profile!r} is not in ~/.databrickscfg",
            f"Pick a profile: DATABRICKS_CONFIG_PROFILE=<name> apx-agent ...  "
            f"(configured: {', '.join(profiles)})",
        )
    if len(profiles) == 1:
        return Check(
            "Databricks auth",
            Status.OK,
            f"credentials resolved from profile {profiles[0]}",
            None,
        )
    if profiles:
        return Check(
            "Databricks auth",
            Status.FAIL,
            "credentials unresolved — profile unset or ambiguous",
            "Pick a profile: DATABRICKS_CONFIG_PROFILE=<name> apx-agent ...  "
            f"(configured: {', '.join(profiles)})",
        )
    return Check(
        "Databricks auth",
        Status.FAIL,
        "no profiles in ~/.databrickscfg",
        "databricks auth login --host "
        "https://<your-workspace>.cloud.databricks.com  "
        "(or `databricks configure --token`)",
    )


def check_databricks_workspace(*, auth_ok: bool) -> Check:
    """Live round-trip: confirm the resolved token authenticates (online)."""
    if not auth_ok:
        return Check(
            "Workspace reachable",
            Status.SKIP,
            "skipped — fix Databricks auth first",
            None,
        )
    try:
        from databricks.sdk import WorkspaceClient

        client = WorkspaceClient()
        me = client.current_user.me()
        host = getattr(getattr(client, "config", None), "host", "") or "workspace"
        user = getattr(me, "user_name", None) or getattr(me, "userName", "user")
        return Check(
            "Workspace reachable", Status.OK, f"{user} @ {host}", None
        )
    except Exception as e:
        msg = str(e)
        lower = msg.lower()
        if "401" in msg or "invalid" in lower or "expired" in lower:
            return Check(
                "Workspace reachable",
                Status.FAIL,
                f"token rejected ({msg})",
                "Your token is expired or invalid — re-run "
                "`databricks auth login --host https://<workspace>...`",
            )
        if "403" in msg or "permission" in lower or "denied" in lower:
            return Check(
                "Workspace reachable",
                Status.FAIL,
                f"permission denied ({msg})",
                "Authenticated, but the principal lacks workspace access — "
                "confirm you targeted the right workspace.",
            )
        return Check(
            "Workspace reachable",
            Status.FAIL,
            f"could not reach workspace ({msg})",
            "Check the workspace host URL, your network/VPN, and TLS.",
        )


def _ai_gateway_model_service_name(model: str) -> str | None:
    """Resource name for ``GET /api/2.1/unity-catalog/{name}``, or None.

    That API requires ``model-services/{catalog}.{schema}.{id}``. A three-part
    UC name (``system.ai.claude-sonnet-4-5``) maps directly. A ``databricks-*``
    foundation-model name is the string ``get_llm`` sends unchanged; it is not
    that resource name, so doctor does not guess a rewrite.
    """
    if model.startswith("model-services/"):
        return model
    parts = model.split(".")
    if len(parts) == 3 and all(parts):
        return f"model-services/{model}"
    return None


def _declared_settings(cwd: Path) -> "dict[str, Any] | Check":
    """Doctor's view of the agent settings declared in agent.py (3b-2, 3d), from an isolated
    import and only when the TOML is valid (the agent-config check reports an invalid one).
    A FAIL check if agent.py won't import."""
    from ._doctor_durable import effective_config_isolated
    from ._inspection import _load_agent_config

    settings: dict[str, Any] = {}
    try:
        _load_agent_config(pyproject_path=cwd / "pyproject.toml")
    except Exception:  # noqa: BLE001 — an invalid TOML config is the agent-config check's to report
        return settings
    try:
        config = effective_config_isolated(cwd)
    except Exception as exc:  # noqa: BLE001 — doctor reports, never raises, on a broken agent.py
        return Check("Agent import", Status.FAIL, f"could not import agent.py: {type(exc).__name__}: {exc}",
                     "Fix the error in agent.py, then re-run apx-agent doctor.")
    for key in ("model", "service_policies"):
        if key not in settings and config is not None and key in config.model_fields_set:
            value = getattr(config, key)
            settings[key] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return settings


def check_model_endpoint(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Verify the configured chat model is reachable through UC AI Gateway.

    Only runs when inside an apx project (reads `model` from pyproject.toml)
    and auth is available. Returns None when there's nothing to check.

    Looks the model up with ``GET /api/2.1/unity-catalog/{name}``. Newer SDK
    versions expose that as ``WorkspaceClient.ai_gateway.get_model_service``;
    this process pins a release that does not, so the call goes through
    ``api_client`` instead of an attribute the type stubs do not have. Does
    not call ``serving_endpoints.get``: the chat LLM is not a Model Serving
    endpoint.
    """
    if not auth_ok:
        return None
    if not _is_apx_project(cwd):
        return None
    settings = _declared_settings(cwd)
    if isinstance(settings, Check):
        return settings
    model: str | None = settings.get("model")
    if not model:
        return None
    label = f"Model service ({model})"
    service_name = _ai_gateway_model_service_name(model)
    if service_name is None:
        return Check(
            label, Status.WARN,
            f"chat model {model!r} is not a model-service name "
            "(expected catalog.schema.id); UC AI Gateway is still the client",
            "get_llm sends this name unchanged to /ai-gateway/mlflow/v1. "
            "Doctor cannot look a databricks-* name up as a model service "
            "without guessing a rewrite. Confirm the name on AI Gateway if "
            "calls 404.",
        )
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
        service = ws.api_client.do("GET", f"/api/2.1/unity-catalog/{service_name}")
        name = service.get("name") if isinstance(service, dict) else None
        if not name:
            return Check(
                label, Status.WARN,
                "model service lookup returned no name",
                "Confirm the model under Unity Catalog AI Gateway.",
            )
        return Check(label, Status.OK, "model service exists and is reachable", None)
    except Exception as e:
        msg = str(e)
        if "404" in msg or "does not exist" in msg.lower() or "not found" in msg.lower():
            return Check(
                label, Status.FAIL,
                f"model service not found: {service_name}",
                "Update `model=` on the agent in agent.py to a "
                "Unity Catalog AI Gateway model service (catalog.schema.id).",
            )
        return Check(
            label, Status.WARN,
            f"could not verify model service ({msg})",
            "Model-service lookup failed — check auth and workspace access.",
        )


def check_gateway_guardrails(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Abstain: name the services whose guardrail policies need a UI check.

    Guardrails on Unity AI Gateway services (External and other service
    policies) are attached in the Unity Gateway UI only. No REST, SDK, or SQL
    surface reads an attachment or its log/enforce mode back, and a
    ``ModelService`` / ``McpService`` payload carries no policy field. Mosaic
    serving-endpoint guardrails do not govern the chat call either. Reporting
    OK would be a false pass, so this check WARNs, lists the bound model
    service plus every declared ``service_policies`` attachment target, and
    calls no API.

    Returns None (nothing to check) unless: an apx project, auth is available,
    a ``model`` is declared, and the target is ``apps``. Never FAILs.
    """
    if not auth_ok or not _is_apx_project(cwd):
        return None
    settings = _declared_settings(cwd)
    if isinstance(settings, Check):
        return None  # an import failure is reported once, by check_model_endpoint
    model: str | None = settings.get("model")
    if not model:
        return None

    from apx_agent.cli import _detect_target
    if _detect_target(cwd).target != "apps":
        return None

    services = [_ai_gateway_model_service_name(model) or model]
    for attachment in settings.get("service_policies", {}).get("attachments", []):
        # Malformed entries are reported by the agent-config check.
        if "target_type" not in attachment or "target" not in attachment:
            continue
        target = f"{attachment['target_type']}:{attachment['target']}"
        if target not in services:
            services.append(target)

    return Check(
        f"AI Gateway guardrails ({model})", Status.WARN,
        "service policy attachment (External or otherwise) is not readable "
        "through any API; serving-endpoint guardrails do not apply to the "
        "Unity Catalog AI Gateway chat call. Unverified: " + ", ".join(services),
        "Confirm each service's policy and log/enforce mode in the Unity "
        "Gateway UI. This check abstains rather than reporting a guardrail "
        "it cannot read back.",
    )


def _data_source_from_agent_py(cwd: Path) -> "tuple[str, str] | None":
    """Read (catalog, schema) from a top-level ``agent.py`` data-agent call.

    Looks for ``DataAgent("cat", "sch", ...)`` / ``CoworkerAgent("cat", "sch", ...)``
    and returns the first two string args (positional or ``catalog=``/``schema=``).
    AST-based and best-effort — returns None if agent.py is absent or unparseable.
    """
    import ast

    agent_py = cwd / "agent.py"
    if not agent_py.exists():
        return None
    try:
        tree = ast.parse(agent_py.read_text())
    except Exception:
        return None
    targets = {"DataAgent", "CoworkerAgent"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            fname = fn.id
        elif isinstance(fn, ast.Attribute):
            fname = fn.attr
        else:
            continue
        if fname not in targets:
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}

        def _str(v: "ast.expr | None") -> str:
            return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else ""

        catalog = _str(node.args[0] if node.args else None) or _str(kw.get("catalog"))
        schema = _str(node.args[1] if len(node.args) > 1 else None) or _str(kw.get("schema"))
        if catalog and schema:
            return catalog, schema
    return None


def check_uc_data_source(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Verify the UC catalog.schema a DataAgent / CoworkerAgent reads exists.

    The data source is the agent's constructor in agent.py
    (``DataAgent("main", "sales")`` / ``CoworkerAgent(...)``). A missing or
    inaccessible schema is the most common silent failure: the agent starts fine,
    every SQL query 403s, and the user sees "I can't find any data" with no clear
    diagnosis. Returns None without auth or when no data source is declared.
    """
    if not auth_ok:
        return None
    catalog, schema = _data_source_from_agent_py(cwd) or ("", "")
    if not catalog or not schema:
        return None
    fqn = f"{catalog}.{schema}"
    label = f"UC schema ({fqn})"
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
        ws.schemas.get(full_name=fqn)
        return Check(label, Status.OK, "exists and readable", None)
    except Exception as e:
        msg = str(e)
        if "404" in msg or "does not exist" in msg.lower() or "not found" in msg.lower():
            return Check(
                label, Status.FAIL,
                f"schema not found: {fqn}",
                f"Confirm `{fqn}` exists in Unity Catalog and your principal has USE SCHEMA. "
                "Update the catalog/schema in agent.py to match.",
            )
        if "403" in msg or "permission" in msg.lower() or "denied" in msg.lower():
            return Check(
                label, Status.FAIL,
                f"permission denied on {fqn}",
                f"Grant USE CATALOG on `{catalog}` and USE SCHEMA on `{fqn}` to your principal.",
            )
        return Check(
            label, Status.WARN,
            f"could not verify schema ({msg})",
            "Schema lookup failed — check auth and Unity Catalog grants.",
        )


def check_sub_agents(cwd: Path) -> Check | None:
    """Probe each declared sub-agent's agent card (issue #445).

    Nothing on the operate path used to verify a declared sub-agent is
    reachable — a typo'd/dead URL stayed green through deploy and status while
    the orchestrator degraded at runtime. Returns None when cwd isn't an apx
    project or declares no ``sub_agents`` (nothing to check). Never FAILs —
    a down peer degrades the agent rather than killing it, and network
    flakiness must not redden doctor — so unreachable peers WARN with
    per-URL reasons.
    """
    if not _is_apx_project(cwd):
        return None
    try:
        from ._doctor_durable import effective_config_isolated  # noqa: PLC0415

        cfg = effective_config_isolated(cwd)  # settings declared on the agent count (3d)
    except Exception:
        return None
    if cfg is None or not cfg.sub_agents:
        return None
    probes = probe_sub_agents(cfg.sub_agents)
    unreachable = [p for p in probes if not p.reachable]
    if not unreachable:
        described = ", ".join(
            f"{p.url} ({p.name})" if p.name else p.url for p in probes
        )
        return Check(
            "Sub-agents", Status.OK, f"all {len(probes)} reachable: {described}", None
        )
    reasons = "; ".join(f"{p.url}: {p.error}" for p in unreachable)
    return Check(
        "Sub-agents",
        Status.WARN,
        f"{len(unreachable)}/{len(probes)} unreachable — {reasons}",
        "Verify each URL serves /.well-known/agent.json (peer deployed and "
        "running, no typo in sub_agents= on the agent); "
        "for $VAR refs, export the variable.",
    )


_AGENT_CARD_SUFFIX = "/.well-known/agent.json"


def _declared_app_peer_urls(cwd: Path) -> list[str] | None:
    """Resolved Databricks App peer URLs from ``sub_agents`` and bindings.

    Returns ``None`` when cwd is not an apx project or config cannot be
    loaded. An empty list means the project has no App peers to check.
    """
    if not _is_apx_project(cwd):
        return None
    try:
        from ._doctor_durable import effective_config_isolated  # noqa: PLC0415

        cfg = effective_config_isolated(cwd)  # settings declared on the agent count (3d)
    except Exception:
        return None
    if cfg is None:
        return None
    from ._apps_authorization import _is_apps_https_url  # noqa: PLC0415
    from ._env import resolve_env_var  # noqa: PLC0415

    raw_values = [*cfg.sub_agents, *(value if isinstance(value, str) else value.url for value in cfg.bindings.values())]
    peers: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        resolved = resolve_env_var(raw)
        if not resolved:
            continue
        url = resolved.rstrip("/")
        if url.endswith(_AGENT_CARD_SUFFIX):
            url = url[: -len(_AGENT_CARD_SUFFIX)]
        if not _is_apps_https_url(url) or url in seen:
            continue
        seen.add(url)
        peers.append(url)
    return peers


def check_a2a_trust(cwd: Path) -> Check | None:
    """WARN that A2A trust is per-hop user OBO, not SP-to-SP ``CAN_USE``.

    Issue #814: some app groups share one service principal, so an SP-to-SP
    ``CAN_USE`` grant is a self-grant and cannot establish trust. When the
    project declares Databricks App peers, confirm the documented trust
    path is user OBO and warn that ``CAN_USE`` is inapplicable in a shared-SP
    topology. Shared-SP cannot be ruled out from the project alone, so this
    is always a WARN when App peers exist. Never FAILs.
    """
    peers = _declared_app_peer_urls(cwd)
    if peers is None or not peers:
        return None
    listed = ", ".join(peers)
    return Check(
        "A2A trust",
        Status.WARN,
        f"{len(peers)} Databricks App peer(s); per-hop user OBO is the trust "
        f"boundary. SP-to-SP CAN_USE is a fallback for distinct-SP topologies "
        f"and is inapplicable where apps share one SP: {listed}",
        "Serve the caller behind the Apps gateway so X-Forwarded-Access-Token "
        "is forwarded each hop. Do not rely on SP-to-SP CAN_USE in a shared-SP "
        "app group. See docs/multi-agent/a2a.md.",
    )


def check_apps_m2m_federation() -> Check | None:
    """Warn when Apps M2M is half-configured without a client secret.

    Databricks Apps can use workload identity federation. apx's app principal
    path requires the injected ``DATABRICKS_CLIENT_ID`` +
    ``DATABRICKS_CLIENT_SECRET`` pair and will not silently treat federation
    without a secret as supported.
    """
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
    if not client_id or client_secret:
        return None
    return Check(
        "Apps M2M identity",
        Status.WARN,
        "DATABRICKS_CLIENT_ID is set without DATABRICKS_CLIENT_SECRET; "
        "workload identity federation without a client secret is unsupported",
        "Use the Databricks Apps injected client id and secret (oauth-m2m). "
        "Do not fall back to DATABRICKS_TOKEN for the app principal.",
    )


def check_apps_enabled(*, auth_ok: bool) -> Check | None:
    """Verify Databricks Apps is enabled in this workspace.

    `apx-agent deploy --target apps` will fail immediately if Apps isn't
    enabled, but nothing warns you until deploy time. This check probes the
    Apps API early so `apx-agent doctor` can catch it before any code is
    bundled or uploaded. Returns None when auth isn't available.
    """
    if not auth_ok:
        return None
    try:
        from databricks.sdk import WorkspaceClient
        ws = WorkspaceClient()
        # Iterate once — lightest possible probe, no results needed.
        # A 404 or FEATURE_DISABLED means Apps is off.
        next(iter(ws.apps.list()), None)
        return Check("Databricks Apps", Status.OK, "enabled in this workspace", None)
    except Exception as e:
        msg = str(e)
        lower = msg.lower()
        if (
            "404" in msg
            or "feature" in lower
            or "disabled" in lower
            or "not enabled" in lower
            or "not available" in lower
        ):
            return Check(
                "Databricks Apps",
                Status.WARN,
                "Apps may not be enabled in this workspace",
                "Ask your workspace admin to enable Databricks Apps: apx deploys agents to Apps.",
            )
        # Any other error (network, auth) — don't fail the check hard
        return None


def check_deploy_provenance(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Compare the latest deployed version's recorded git provenance to HEAD.

    Every deploy stamps ``apx.apps.git_sha`` / ``apx.git_dirty`` on the UC
    version it registers (issue #403); this check answers "does what's
    deployed match my working tree?". WARNs on drift (different commit) or a
    dirty-tree deploy; degrades to SKIP — never FAIL — when nothing is
    deployed yet, the project isn't a git repo, or UC can't be reached
    (offline). Returns None when cwd isn't an apx project, no UC name
    resolves, or auth isn't available (nothing to compare against).
    """
    if not auth_ok or not _is_apx_project(cwd):
        return None
    from ._apps_registry import (
        GIT_DIRTY_TAG,
        GIT_SHA_TAG,
        _uc_registry_context,
        tag_value,
    )
    from .cli import _git_head_sha, _resolve_project_uc_name

    uc_name = _resolve_project_uc_name(cwd)
    if uc_name is None:
        return None

    label = f"Deploy provenance ({uc_name})"
    head = _git_head_sha(cwd)
    if head is None:
        return Check(
            label, Status.SKIP, "not a git repo — no local commit to compare", None
        )
    try:
        from mlflow.tracking import MlflowClient

        with _uc_registry_context():
            client = MlflowClient()
            versions = client.search_model_versions(f"name='{uc_name}'")
            latest_summary = (
                max(versions, key=lambda v: int(v.version)) if versions else None
            )
            latest = (
                client.get_model_version(uc_name, str(latest_summary.version))
                if latest_summary else None
            )
    except Exception as e:
        # Offline / no UC access must never hard-fail doctor — degrade to a
        # "could not verify" notice.
        return Check(
            label, Status.SKIP, f"could not verify deployed provenance ({e})", None
        )
    if not versions or latest is None:
        return Check(label, Status.SKIP, "no deployed versions recorded yet", None)
    tags: dict = getattr(latest, "tags", None) or {}
    deployed_sha = tag_value(tags, GIT_SHA_TAG)
    if not deployed_sha:
        return Check(
            label, Status.SKIP,
            f"version {latest.version} has no recorded git provenance "
            "(deployed before provenance stamping)",
            "Re-deploy with this apx-agent version to stamp git provenance.",
        )
    if deployed_sha != head:
        return Check(
            label, Status.WARN,
            f"drift: deployed version {latest.version} is commit "
            f"{deployed_sha[:12]}, local HEAD is {head[:12]}",
            "Re-deploy to ship local HEAD, or check out the deployed commit "
            "to match what's live.",
        )
    if tag_value(tags, GIT_DIRTY_TAG) == "true":
        return Check(
            label, Status.WARN,
            f"deployed version {latest.version} matches HEAD {head[:12]} but "
            "was deployed from a tree with uncommitted changes",
            "Commit and re-deploy so the recorded commit is the code that shipped.",
        )
    return Check(
        label, Status.OK,
        f"deployed version {latest.version} matches local HEAD ({head[:12]})",
        None,
    )


def _is_apx_project(cwd: Path) -> bool:
    """True when cwd looks like an apx project: an apx TOML table, or an agent.py using apx_agent."""
    pyproject, agent_py = cwd / "pyproject.toml", cwd / "agent.py"
    try:
        text = pyproject.read_text() if pyproject.exists() else ""
        if "[tool.apx]" in text or "[tool.apx.deploy]" in text:
            return True
        return agent_py.is_file() and "apx_agent" in agent_py.read_text()
    except OSError:
        return False


def check_project_layout(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check(
            "Project layout",
            Status.SKIP,
            f"{cwd} is not an apx project",
            "Run `apx-agent scaffold my-agent` to create one, then cd into it.",
        )
    from apx_agent.cli import _declares_durable, _detect_target

    target = _detect_target(cwd).target
    native = _declares_durable(cwd)
    marker = "agent_server/" if target == "apps" and not native else "agent.py"
    if target == "apps" and not native:
        ok = (cwd / "agent_server").is_dir()
    else:
        ok = (cwd / "agent.py").exists()
    if ok:
        return Check("Project layout", Status.OK, f"{target} layout detected", None)
    return Check(
        "Project layout",
        Status.FAIL,
        f"pyproject declares an agent but {marker} is missing",
        "Re-run `apx-agent scaffold` or restore the agent entrypoint.",
    )


def check_target(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("Target", Status.SKIP, "not in an apx project", None)
    from apx_agent.cli import _detect_target, _read_apx_agent_config

    # _detect_target always returns "apps" (Model Serving was removed); no failure mode to surface.
    detected = _detect_target(cwd)
    from ._inspection import static_agent_setting

    runtime = static_agent_setting(cwd / "agent.py", "target") or _read_apx_agent_config(
        cwd / "pyproject.toml").get("target")
    if isinstance(runtime, str) and runtime:
        detail = (
            f"{detected.target} destination ({detected.reason}); "
            f"runtime {runtime}"
        )
    else:
        detail = f"{detected.target} destination ({detected.reason})"
    return Check("Target", Status.OK, detail, None)


def check_extras(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("Required extra", Status.SKIP, "not in an apx project", None)
    from apx_agent.cli import _declares_durable, _detect_target

    target = _detect_target(cwd).target
    if _declares_durable(cwd):
        module, label, fix = (
            "databricks_agentkit.runtime.app", "AgentKit native runtime",
            "uv add 'apx-agent[agentbricks]'",
        )
    elif target == "apps":
        module, label, fix = (
            "mlflow.genai.agent_server",
            "mlflow.genai (eval extra)",
            "uv add 'apx-agent[eval]'  (or `uv sync --extra eval` in this project)",
        )
    else:
        module, label, fix = (
            "langchain",
            "langchain (required dep)",
            "uv sync  (langgraph/langchain are required deps of apx-agent)",
        )
    try:
        importlib.import_module(module)
        return Check("Required extra", Status.OK, f"{label} installed", None)
    except ImportError:
        return Check(
            "Required extra",
            Status.FAIL,
            f"{label} is not installed (needed for target {target})",
            fix,
        )


def check_databricks_yml(cwd: Path) -> Check:
    if not _is_apx_project(cwd):
        return Check("databricks.yml", Status.SKIP, "not in an apx project", None)
    if (cwd / "databricks.yml").exists():
        return Check("databricks.yml", Status.OK, "present", None)
    from ._inspection import _load_agent_config
    try:
        config = _load_agent_config(pyproject_path=cwd / "pyproject.toml")
    except (ValueError, OSError):
        config = None  # The agent prerequisite check reports invalid declarations.
    apps_api = config is not None and config.deploy is not None and config.deploy.backend == "apps_api"
    if apps_api or (
        config is not None and config.target == "durable_agent_server"
        and (config.deploy is None or config.deploy.space is None)
    ):
        detail = "Apps API deployment does not require a Bundle" if apps_api else "native Agent Bricks deployment does not require a Bundle"
        return Check("databricks.yml", Status.SKIP, detail, None)
    return Check(
        "databricks.yml",
        Status.WARN,
        "missing — `apx-agent deploy --target apps` needs it",
        "Re-run `apx-agent scaffold <name> --target apps`.",
    )


def check_agent_prerequisites(
    config: AgentConfig | None, *, online: bool, ws: Any = None, provision_stores: bool = False,
) -> list[Check]:
    """Read-only checks; native deployment may defer missing stores to its provisioner."""
    if config is None:
        return []
    durable = config.target == "durable_agent_server"
    managed_memory = config.memory is not None and config.memory.type == "managed"
    if not durable and not managed_memory:
        return []
    fix_sdk = "Install/sync apx-agent[agentbricks] in the build environment and generated App dependencies."
    try:
        from databricks_agentkit import AgentKitClient
        if durable:
            from databricks_agentkit.runtime.app import DurableAgentServer
            from databricks_agentkit.runtime.auth import InvocationAuthPolicy
            from databricks_agentkit.runtime.tool_manifest import parse_user_auth
            from databricks_agentkit.langgraph.session_store import DatabricksSessionStoreSaver

            if not hasattr(InvocationAuthPolicy(), "user_required") or not callable(DurableAgentServer.recover) or not callable(DatabricksSessionStoreSaver):
                raise ImportError("incompatible AgentKit runtime")
            parse_user_auth({"auth": {"user": {"required": True}}})
    except (ImportError, AttributeError):
        return [Check("AgentKit SDK", Status.FAIL, "Required AgentKit APIs are missing or incompatible", fix_sdk)]
    checks = [Check("AgentKit SDK", Status.OK, "Required local SDK APIs are installed")]
    probes: list[tuple[str, str, str | None]] = []
    if managed_memory:
        assert config.memory is not None
        probes.append(("Managed memory", "memory_stores", config.memory.store_name))
    if config.session is not None and config.session.type == "managed":
        probes.append(("Managed sessions", "session_stores", config.session.store_name))
    if config.deploy is not None and config.deploy.space is not None:
        probes.append(("App Space", "apps", config.deploy.space))
    if not probes:
        return checks
    if not online:
        checks.extend(Check(label, Status.SKIP, f"{name!r}: workspace availability unverified (offline or authentication unavailable)",
                            "Run doctor online with the intended workspace profile.") for label, _, name in probes)
        return checks
    try:
        if ws is None:
            from ._defaults import _make_workspace_client

            ws = _make_workspace_client()
        client = AgentKitClient(workspace_client=ws)
    except Exception:
        checks.append(Check("Agent workspace access", Status.FAIL, "Workspace availability unverified: authentication/client initialization failed",
                            "Authenticate to the intended workspace and rerun doctor."))
        return checks
    from databricks.sdk.errors import NotFound

    for label, resource, name in probes:
        if not name:
            checks.append(Check(label, Status.FAIL, "No store_name is declared", "Declare a managed store_name before deploying."))
            continue
        try:
            if resource == "apps":
                result = ws.apps.get_space(name)
            else:
                result = getattr(client, resource).get(name)
            if not isinstance(result.name, str) or not result.name:
                raise ValueError("resource response has no name")
        except Exception as exc:
            code = getattr(exc, "error_code", None)
            if code in {"FEATURE_DISABLED", "NOT_IMPLEMENTED", "UNIMPLEMENTED"}:
                detail = "Required feature is disabled or unavailable in this workspace"
                fix = "Ask the workspace administrator to verify preview enablement and regional availability for this capability."
            elif code in {"PERMISSION_DENIED", "UNAUTHENTICATED"}:
                detail = "Access denied; feature enablement is unverified"
                fix = "Verify the selected profile and permissions on the declared resource."
            elif isinstance(exc, NotFound) or isinstance(exc.__cause__, NotFound) or code in {"NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"}:
                detail = "Declared resource or API route was not found; feature enablement is unverified"
                if provision_stores and resource in {"memory_stores", "session_stores"}:
                    checks.append(Check(label, Status.WARN, f"{name!r}: {detail}",
                                        "Agent Bricks deployment will create or resolve the declared store; provisioning must succeed before the app can run."))
                    continue
                fix = "Verify the store/space name and workspace. If the API is unavailable, ask the administrator to check preview availability."
            else:
                detail = "Workspace availability unverified: read-only probe failed"
                fix = "Check workspace connectivity and credentials, then rerun doctor."
            checks.append(Check(label, Status.FAIL, f"{name!r}: {detail}", fix))
        else:
            checks.append(Check(label, Status.OK, f"{name!r}: declared resource is readable with the checking identity; runtime write access is not proven"))
    return checks


def check_agent_image(cwd: Path, config: AgentConfig | None) -> Check | None:
    """Check the dependencies that will be installed in the App image."""
    if config is None or (config.target != "durable_agent_server" and not (config.memory is not None and config.memory.type == "managed")):
        return None
    import tomllib
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    label = "Agent image dependencies"
    fix = "Add apx-agent[agentbricks] to project.dependencies, regenerate uv.lock, and rebuild the App."
    try:
        document = tomllib.loads((cwd / "pyproject.toml").read_text())
        requirements = [Requirement(raw) for raw in document.get("project", {}).get("dependencies", [])]
        declared = any(
            req.marker is None and (
                (canonicalize_name(req.name) == "apx-agent" and "agentbricks" in req.extras)
                or canonicalize_name(req.name) == "databricks-agentbricks"
            ) for req in requirements
        )
        if not declared:
            return Check(label, Status.FAIL, "Image dependencies do not unconditionally include AgentKit", fix)
        lock = cwd / "uv.lock"
        if not lock.exists():
            return Check(label, Status.WARN, "AgentKit is declared; resolved image version is unverified without uv.lock", fix)
        packages = tomllib.loads(lock.read_text()).get("package", [])
        versions = [p.get("version") for p in packages if canonicalize_name(p["name"]) == "databricks-agentbricks"]
        sdk_requirement = next(Requirement(raw) for raw in importlib.metadata.requires("apx-agent") or []
                               if canonicalize_name(Requirement(raw).name) == "databricks-agentbricks")
        if not versions or any(not version or version not in sdk_requirement.specifier for version in versions):
            return Check(label, Status.FAIL, "Image lock is missing a compatible databricks-agentbricks package", fix)
    except (OSError, ValueError, KeyError, StopIteration):
        return Check(label, Status.FAIL, "Could not verify the image dependency manifest/lock", fix)
    return Check(label, Status.OK, "Image declares AgentKit and locks a compatible version; installed image has not been executed")


def check_memory_backend(cwd: Path, *, auth_ok: bool) -> Check | None:
    """Check that the configured memory backend is reachable.

    Returns ``None`` when no memory config is present (nothing to check).
    Skips the live probe when auth is not available.
    """
    if not _is_apx_project(cwd):
        return None
    try:
        from ._doctor_durable import effective_config_isolated  # noqa: PLC0415

        cfg = effective_config_isolated(cwd)  # settings declared on the agent count (3d)
    except Exception:
        return None
    if cfg is None or cfg.memory is None:
        return None

    mem = cfg.memory
    if mem.type == "managed":
        return None  # Shared AgentKit prerequisite checks own this backend.
    label = "Memory backend"

    if not auth_ok:
        return Check(label, Status.SKIP, f"type={mem.type} — skipped (auth unavailable)", None)

    if mem.type == "inmemory":
        return Check(label, Status.OK, "inmemory (no external backend)", None)

    if mem.type == "lakebase":
        # Resolve ${ENV_VAR} the same way the runtime does — a config that only
        # *mentions* a host isn't reachable if the env var is unset (the scaffold
        # default is host="${LAKEBASE_HOST}").
        from ._wiring import _resolve_env_var  # noqa: PLC0415
        host = _resolve_env_var(mem.host) if mem.host else ""
        database = _resolve_env_var(mem.database) if mem.database else ""
        if not host or not database:
            return Check(
                label, Status.WARN,
                f"type=lakebase but host/database resolve to empty "
                f"(host={mem.host!r} database={mem.database!r}) — memory will "
                "degrade to in-process at runtime",
                "Set host + database in memory= on the agent; if either is a "
                "${ENV_VAR}, make sure the variable is exported.",
            )
        # Live reachability probe: build the same OAuth-token engine the
        # runtime uses and round-trip a
        # trivial query. Dispose the probe pool so the check doesn't leak it.
        engine = None
        try:
            from sqlalchemy import text  # noqa: PLC0415

            from ._defaults import _make_workspace_client  # noqa: PLC0415
            from ._lakebase_engine import build_lakebase_engine  # noqa: PLC0415
            ws = _make_workspace_client()
            engine = build_lakebase_engine(ws=ws, database=database, host=host)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return Check(
                label, Status.OK,
                f"lakebase reachable: host={host} database={database}", None,
            )
        except Exception as exc:
            short = str(exc)[:120]
            return Check(
                label, Status.WARN,
                f"lakebase not reachable ({short})",
                "Check host/database and that this principal has Lakebase access.",
            )
        finally:
            if engine is not None:
                engine.dispose()

    return Check(label, Status.SKIP, f"unknown type {mem.type!r}", None)


def check_abac_compute_floor(cwd: Path, *, auth_ok: bool) -> Check | None:
    """ABAC runtime-floor check: when data= on the agent declares governed
    tags or policies, the SQL compute that will apply + evaluate them must be
    serverless or track a current DBR (>= 16.4). Protected tables fail closed
    on older runtimes — better surfaced here than mid-conversation.

    Returns ``None`` when no data-governance declaration exists.
    """
    label = "ABAC compute floor"
    if not _is_apx_project(cwd):
        return None
    try:
        from ._doctor_durable import effective_config_isolated  # noqa: PLC0415

        cfg = effective_config_isolated(cwd)  # settings declared on the agent count (3d)
    except Exception:
        return None
    if cfg is None or cfg.data is None:
        return None

    data = cfg.data
    n_tags = len(data.governed_tags)
    n_tables = len(data.tables)
    n_policies = sum(len(t.policies) for t in data.tables)
    declared = f"{n_tags} governed tags, {n_tables} tables, {n_policies} policies"

    if not auth_ok:
        return Check(label, Status.SKIP, f"{declared} — skipped (auth unavailable)", None)

    try:
        from ._defaults import _make_workspace_client  # noqa: PLC0415
        from ._sql import get_warehouse_id  # noqa: PLC0415
        ws = _make_workspace_client()
        # Mirror run_sql's resolution: session warehouse if declared, else
        # auto-discovery (which already prefers serverless).
        wh_id: str | None = None
        if cfg.session is not None and cfg.session.warehouse_id:
            wh_id = cfg.session.warehouse_id
        if wh_id is None:
            wh_id = get_warehouse_id(ws)
        wh = ws.warehouses.get(wh_id)
    except Exception as exc:
        return Check(
            label, Status.WARN,
            f"{declared} — could not resolve a SQL warehouse to verify "
            f"compute floor ({str(exc)[:120]})",
            "ABAC needs serverless or DBR 16.4+. Create/select a serverless "
            "warehouse (`apx-agent doctor` again after), or set warehouse_id "
            "in session= on the agent.",
        )

    if getattr(wh, "enable_serverless_compute", False):
        return Check(
            label, Status.OK,
            f"{declared} — warehouse {wh.name!r} is serverless (current DBR)", None,
        )

    channel = getattr(getattr(wh, "channel", None), "name", None)
    if channel is None:
        channel = str(getattr(getattr(wh, "channel", None), "value", "unknown"))
    if channel in ("CHANNEL_NAME_CURRENT", "CHANNEL_NAME_PREVIEW"):
        return Check(
            label, Status.OK,
            f"{declared} — warehouse {wh.name!r} is pro/classic on channel "
            f"{channel} (tracks current DBR ≥ 16.4)", None,
        )
    return Check(
        label, Status.WARN,
        f"{declared} — warehouse {wh.name!r} is pro/classic on channel "
        f"{channel}, which may pin a DBR below the ABAC floor (16.4). "
        "Protected tables fail closed on sub-floor runtimes.",
        "Switch the warehouse channel to Current, use a serverless warehouse, "
        "or confirm the pinned runtime is ≥ DBR 16.4 before deploying "
        "ABAC-protected tables.",
    )


_WORKSPACE_BOUND_KINDS = frozenset({
    "sql_warehouse",
    "genie_space",
    "vector_search_index",
    "serving_endpoint",
    "lakebase_instance",
})


def _resource_missing_in_workspace(kind: str, identifier: str, exc: Exception) -> str:
    """Doctor detail when a host-bound resource is not readable here."""
    short = str(exc)[:120]
    if kind in _WORKSPACE_BOUND_KINDS:
        return (
            f"{identifier} not found or not accessible in this workspace ({short}). "
            "The forwarded OBO token cannot leave the app workspace"
        )
    return f"not found or not accessible ({short})"


def check_apps_service_principal(app_name: str, ws: Any) -> Check:
    """Show the dedicated app SP application id for UC grants (#973)."""
    try:
        app = ws.apps.get(app_name)
    except Exception as exc:
        code = getattr(exc, "error_code", None)
        if code in {"NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"} or "404" in str(exc):
            return Check(
                "App service principal",
                Status.SKIP,
                f"{app_name!r} is not deployed yet; copy the application id from the Apps Authorization tab after first deploy",
                None,
            )
        return Check(
            "App service principal",
            Status.WARN,
            f"could not read {app_name!r}",
            "Grant Unity Catalog on the app's dedicated service principal, not a personal SP.",
        )
    sp_id = app.service_principal_client_id
    if not isinstance(sp_id, str) or not sp_id:
        return Check(
            "App service principal",
            Status.WARN,
            f"{app_name!r} has no service_principal_client_id",
            "Copy the application id from the Databricks Apps Authorization tab.",
        )
    return Check(
        "App service principal",
        Status.OK,
        f"grant Unity Catalog to application id {sp_id} (Apps Authorization tab)",
        None,
    )


def check_app_acl(app_name: str, ws: Any) -> Check:
    """Warn when the App ACL is all CAN_MANAGE and has no CAN_USE runners (#971)."""
    try:
        perms = ws.apps.get_permissions(app_name)
    except Exception as exc:
        code = getattr(exc, "error_code", None)
        if code in {"NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"} or "404" in str(exc):
            return Check(
                "App ACL",
                Status.SKIP,
                f"{app_name!r} is not deployed yet",
                None,
            )
        return Check(
            "App ACL",
            Status.SKIP,
            "App permissions unread",
            None,
        )
    acl = perms.access_control_list
    if not isinstance(acl, list):
        return Check(
            "App ACL",
            Status.SKIP,
            "App permissions unread",
            None,
        )
    can_manage = 0
    can_use = 0
    for entry in acl:
        perms_list = entry.all_permissions
        if not isinstance(perms_list, list):
            continue
        for perm in perms_list:
            level = perm.permission_level
            value = level.value if hasattr(level, "value") else level
            if value == "CAN_MANAGE":
                can_manage += 1
            elif value == "CAN_USE":
                can_use += 1
    if can_manage > 0 and can_use == 0:
        return Check(
            "App ACL",
            Status.WARN,
            f"{app_name!r} has CAN_MANAGE principals and no CAN_USE runners",
            "Keep CAN MANAGE small; grant CAN USE to users who should run the app.",
        )
    return Check(
        "App ACL",
        Status.OK,
        f"{app_name!r}: CAN_USE={can_use} CAN_MANAGE={can_manage}",
        None,
    )


def check_workspace_scope_allowlist(plan: Any, ws: Any) -> Check:
    """Compare the plan's user_api_scopes to workspace allowedAppsUserApiScopes."""
    from ._apps_authorization import read_workspace_apps_scope_allowlist

    read = read_workspace_apps_scope_allowlist(ws)
    if not read.readable:
        return Check(
            "Workspace OAuth scopes",
            Status.SKIP,
            "allowedAppsUserApiScopes unread",
            "Workspace admin Restrict OAuth scopes could not be read; deploy may still fail at app start.",
        )
    if read.allowed_scopes is None:
        return Check(
            "Workspace OAuth scopes",
            Status.OK,
            "workspace does not restrict Apps OAuth scopes",
            None,
        )
    missing = sorted(set(plan.user_api_scopes) - read.allowed_scopes)
    if missing:
        return Check(
            "Workspace OAuth scopes",
            Status.FAIL,
            "blocked by allowedAppsUserApiScopes: " + ", ".join(missing),
            "Ask the workspace admin to allow these scopes, or drop the tools that need them.",
        )
    return Check(
        "Workspace OAuth scopes",
        Status.OK,
        "plan scopes are within allowedAppsUserApiScopes",
        None,
    )


def check_declared_tools(cwd: Path, *, auth_ok: bool, online: bool = True) -> list[Check]:
    """Probe resources on the resolved Python agent, using the checking identity.

    Import/finalization failures are diagnostics, never an empty resource list.
    Without auth, skip imports because Python tool factories can perform I/O.
    """
    if not (cwd / "pyproject.toml").exists():
        return []

    from contextlib import chdir  # noqa: PLC0415

    from ._apps_authorization import compile_authorization_plan  # noqa: PLC0415
    from ._defaults import _make_workspace_client  # noqa: PLC0415
    from ._inspection import _load_agent_config  # noqa: PLC0415
    from ._tool_config import ToolConfigError, reject_legacy_tool_config  # noqa: PLC0415
    from ._wiring import finalize_agent, resolve_agent  # noqa: PLC0415
    from .cli import _detect_module_spec  # noqa: PLC0415

    pyproject = str(cwd.resolve() / "pyproject.toml")
    try:
        reject_legacy_tool_config(pyproject)
        if not _is_apx_project(cwd):
            return []
        cfg = _load_agent_config(pyproject_path=pyproject)
    except ToolConfigError as exc:
        return [Check(
            "Declared tools", Status.FAIL, str(exc),
            "Move tool declarations into Python tools/ modules and explicitly import them in agent.py; remove [[tool.apx.tools]].",
        )]
    except Exception as exc:
        return [Check(
            "Declared tools", Status.FAIL, f"Could not load agent config: {exc}",
            "Fix the agent configuration in pyproject.toml.",
        )]

    session_warehouse = cfg.session.warehouse_id if cfg and cfg.session else None
    checks: list[Check] = []
    if not auth_ok or not online:
        reason = "fix Databricks auth first" if not auth_ok else "run doctor --online to verify resources"
        checks.append(Check("Declared tools", Status.SKIP, f"Python resource inspection skipped — {reason}"))
        if session_warehouse:
            checks.append(Check("Session warehouse", Status.SKIP, f"skipped — {reason}"))
        return checks

    try:
        ws = _make_workspace_client()
    except Exception as exc:
        return [Check("Declared tools", Status.FAIL, f"Workspace client unavailable: {exc}", "Fix Databricks auth first.")]

    try:
        # Use the same module detection, finalization and authorization walker as
        # the CLI, not a second parser or a tool registry scan.
        with chdir(cwd):
            agent = resolve_agent(_detect_module_spec(), cfg, ws=ws)
            finalize_agent(agent, cfg, pyproject_path=pyproject, ws=ws)
            plan = compile_authorization_plan(
                agent, model=cfg.model if cfg else "",
                agent_identity=cfg.identity if cfg else None,
            )
        resources = sorted(set(plan.user_resources + plan.service_resources), key=lambda spec: (spec.kind, spec.identifier))
    except Exception as exc:
        return [Check(
            "Declared tools", Status.FAIL, f"Could not inspect Python agent resources: {exc}",
            "Fix the agent module imports, tool declarations, or authorization configuration.",
        )]

    for spec in resources:
        typ = spec.kind

        if typ == "genie_space":
            space_id = spec.identifier
            try:
                ws.genie.get_space(space_id=space_id)
                checks.append(Check(f"Genie space ({space_id[:20]})", Status.OK, "readable with the checking identity; runtime access is not proven", None))
            except Exception as exc:
                checks.append(Check(
                    f"Genie space ({space_id[:20]})",
                    Status.WARN,
                    _resource_missing_in_workspace("genie_space", space_id, exc),
                    f"Confirm space_id {space_id!r} exists in this workspace. The forwarded OBO token cannot leave the app workspace.",
                ))

        elif typ == "vector_search_index":
            index_name = spec.identifier
            try:
                ws.vector_search_indexes.get_index(index_name=index_name)
                checks.append(Check(f"VS index ({index_name})", Status.OK, "readable with the checking identity; runtime access is not proven", None))
            except Exception as exc:
                checks.append(Check(
                    f"VS index ({index_name})",
                    Status.WARN,
                    _resource_missing_in_workspace("vector_search_index", index_name, exc),
                    "Confirm the index exists in this workspace. The forwarded OBO token cannot leave the app workspace.",
                ))

        elif typ == "uc_function":
            function_name = spec.identifier
            try:
                ws.functions.get(name=function_name)
                checks.append(Check(f"UC function ({function_name})", Status.OK, "readable with the checking identity; runtime access is not proven", None))
            except Exception as exc:
                short = str(exc)[:120]
                checks.append(Check(
                    f"UC function ({function_name})",
                    Status.WARN,
                    f"not found or not accessible ({short})",
                    f"Confirm {function_name!r} exists in Unity Catalog.",
                ))

        elif typ == "sql_warehouse":
            warehouse_id = spec.identifier
            try:
                ws.warehouses.get(id=warehouse_id)
                checks.append(Check(f"SQL warehouse ({warehouse_id})", Status.OK, "readable with the checking identity; runtime access is not proven", None))
            except Exception as exc:
                checks.append(Check(
                    f"SQL warehouse ({warehouse_id})",
                    Status.WARN,
                    _resource_missing_in_workspace("sql_warehouse", warehouse_id, exc),
                    f"Confirm warehouse {warehouse_id!r} exists in this workspace. The forwarded OBO token cannot leave the app workspace.",
                ))

        elif typ == "serving_endpoint":
            endpoint_name = spec.identifier
            try:
                ws.serving_endpoints.get(endpoint_name)
                checks.append(Check(f"Serving endpoint ({endpoint_name})", Status.OK, "readable with the checking identity; runtime access is not proven", None))
            except Exception as exc:
                checks.append(Check(
                    f"Serving endpoint ({endpoint_name})",
                    Status.WARN,
                    _resource_missing_in_workspace("serving_endpoint", endpoint_name, exc),
                    f"Confirm endpoint {endpoint_name!r} exists in this workspace. The forwarded OBO token cannot leave the app workspace.",
                ))

        elif typ == "lakebase_instance":
            instance_name = spec.identifier
            try:
                ws.database.get_database_instance(name=instance_name)
                checks.append(Check(f"Lakebase ({instance_name})", Status.OK, "readable with the checking identity; runtime access is not proven", None))
            except Exception as exc:
                checks.append(Check(
                    f"Lakebase ({instance_name})",
                    Status.WARN,
                    _resource_missing_in_workspace("lakebase_instance", instance_name, exc),
                    f"Confirm Lakebase instance {instance_name!r} exists in this workspace. The forwarded OBO token cannot leave the app workspace.",
                ))
        else:
            checks.append(Check(
                f"Resource ({typ}: {spec.identifier})", Status.SKIP,
                "Declared by Python agent; doctor has no presence probe for this resource kind.",
            ))

    if session_warehouse:
        try:
            ws.warehouses.get(id=session_warehouse)
            checks.append(Check(f"Session warehouse ({session_warehouse})", Status.OK, "readable with the checking identity; runtime access is not proven", None))
        except Exception as exc:
            checks.append(Check(
                f"Session warehouse ({session_warehouse})",
                Status.WARN,
                _resource_missing_in_workspace("sql_warehouse", session_warehouse, exc),
                f"Confirm warehouse {session_warehouse!r} exists in this workspace. The forwarded OBO token cannot leave the app workspace.",
            ))

    checks.append(check_workspace_scope_allowlist(plan, ws))
    if cfg is not None:
        checks.append(check_apps_service_principal(cfg.name, ws))
        checks.append(check_app_acl(cfg.name, ws))
    return checks
