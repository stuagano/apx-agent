#!/usr/bin/env python3
"""End-to-end deploy + verify smoke-test for apx-agent examples.

Drives the full apx-agent deploy pipeline against a real Databricks workspace
and asserts that the resulting Agent Playground endpoint is functional. Built
so CI can catch regressions before they surface in a customer deploy.

Pipeline:

  1. Pre-flight       — profile auth, catalog/schema exist, agent module loads.
  2. apx deploy        — subprocess into ``apx deploy`` (so we exercise the real
                         CLI path, not a re-implementation).
  3. Wait              — poll ``serving-endpoints get`` with exponential backoff
                         until READY (or timeout).
  4. Invoke            — POST ``messages`` to /serving-endpoints/<name>/invocations,
                         assert HTTP 200 + non-empty assistant content.
  5. Trace             — best-effort MLflow check that an apx.* trace landed.
  6. Cleanup           — delete endpoint unless ``--keep`` (or ``--delete-on-failure``
                         on the failure path).

Usage::

    # dry-run (no workspace contact — verifies plan only)
    python scripts/agent-playground-smoke.py \\
        --module examples.memory_demo.app:agent \\
        --profile fe-stable \\
        --catalog serverless_stable_qh44kx_catalog \\
        --schema apx_agents \\
        --llm-endpoint databricks-claude-sonnet-4-6 \\
        --experiment /Users/me@databricks.com/apx-smoke \\
        --dry-run

    # CI smoke (real deploy, JSON output, keep endpoint for inspection)
    python scripts/agent-playground-smoke.py \\
        --module examples.memory_demo.app:agent \\
        --profile fe-stable \\
        --catalog serverless_stable_qh44kx_catalog \\
        --schema apx_agents \\
        --llm-endpoint databricks-claude-sonnet-4-6 \\
        --experiment /Users/me@databricks.com/apx-smoke \\
        --keep --json-output

Exit codes:
  0  success
  1  deploy failed (pre-flight, subprocess error, or readiness timeout)
  2  invocations call failed or response shape unexpected
  3  trace assertions failed (only if ``--strict-traces`` is set; default warns)
  4  cleanup failed (post-success). Note: a deploy/invoke failure code dominates
     a follow-on cleanup failure — exit 1/2 wins over exit 4.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_DIR = REPO_ROOT / "python"

# Exit codes — single source of truth.
EXIT_OK = 0
EXIT_DEPLOY = 1
EXIT_INVOKE = 2
EXIT_TRACE = 3
EXIT_CLEANUP = 4


# ---------------------------------------------------------------------------
# Output helpers — colored progress on stderr; clean results on stdout.
# ---------------------------------------------------------------------------


_ANSI = {
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "reset": "\x1b[0m",
}


class Logger:
    """Stderr logger. Silenced under --json-output so stdout stays clean."""

    def __init__(self, *, quiet: bool, color: bool) -> None:
        self.quiet = quiet
        self.color = color and sys.stderr.isatty()

    def _wrap(self, text: str, color: str) -> str:
        if not self.color:
            return text
        return f"{_ANSI[color]}{text}{_ANSI['reset']}"

    def info(self, msg: str) -> None:
        if self.quiet:
            return
        print(self._wrap(f"-> {msg}", "blue"), file=sys.stderr, flush=True)

    def ok(self, msg: str) -> None:
        if self.quiet:
            return
        print(self._wrap(f"OK {msg}", "green"), file=sys.stderr, flush=True)

    def warn(self, msg: str) -> None:
        # Warnings shown even in quiet mode — they signal degraded checks.
        print(self._wrap(f"WARN {msg}", "yellow"), file=sys.stderr, flush=True)

    def err(self, msg: str) -> None:
        print(self._wrap(f"ERR  {msg}", "red"), file=sys.stderr, flush=True)

    def step(self, msg: str) -> None:
        if self.quiet:
            return
        print(self._wrap(f"\n== {msg} ==", "bold"), file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Plan dataclass — resolved configuration, also used by --dry-run.
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    """All resolved values for one smoke-test run."""

    module_spec: str
    profile: str
    catalog: str
    schema: str
    model_name: str
    registered_model_name: str
    llm_endpoint: str
    experiment: str
    test_prompt: str
    deploy_timeout_seconds: int
    keep: bool
    delete_on_failure: bool
    strict_traces: bool
    cwd: Path
    deploy_argv: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["cwd"] = str(d["cwd"])
        return d


# ---------------------------------------------------------------------------
# Result dataclass — accumulates state for JSON output.
# ---------------------------------------------------------------------------


@dataclass
class Result:
    ok: bool = False
    endpoint: str | None = None
    endpoint_url: str | None = None
    review_app_url: str | None = None
    query_endpoint: str | None = None
    response_text: str | None = None
    trace_count: int = 0
    apx_span_attrs_found: list[str] = field(default_factory=list)
    deploy_seconds: float | None = None
    invoke_seconds: float | None = None
    error: str | None = None
    exit_code: int = EXIT_OK


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def _derive_model_name(module_spec: str) -> str:
    """Derive a UC model name from a MODULE:VAR spec.

    ``examples.memory_demo.app:agent`` -> ``memory_demo``
    ``examples.customer_triage.agent:agent`` -> ``customer_triage``
    Falls back to the last segment of the module path.
    """
    module_path, _, _ = module_spec.partition(":")
    parts = [p for p in module_path.split(".") if p]
    # Strip the conventional 'examples.' prefix and trailing module file
    # ('.app', '.agent') so we get the example's directory name.
    if parts and parts[0] == "examples":
        parts = parts[1:]
    if parts and parts[-1] in {"app", "agent"} and len(parts) > 1:
        parts = parts[:-1]
    if not parts:
        raise ValueError(f"Cannot derive model name from {module_spec!r}")
    # Use just the leaf name; underscores already legal in UC identifiers.
    return parts[-1]


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-playground-smoke",
        description="End-to-end deploy + verify for apx-agent examples.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--module", required=True,
        help='Agent module spec, e.g. "examples.memory_demo.app:agent".',
    )
    p.add_argument("--profile", required=True, help="Databricks CLI profile name.")
    p.add_argument(
        "--catalog", required=True,
        help="UC catalog to register the model under.",
    )
    p.add_argument(
        "--schema", required=True,
        help="UC schema to register the model under.",
    )
    p.add_argument(
        "--model-name", default=None,
        help="UC model name (default: derived from --module).",
    )
    p.add_argument(
        "--llm-endpoint", required=True,
        help='LLM serving endpoint (passed as --model to apx deploy).',
    )
    p.add_argument(
        "--experiment", required=True,
        help="MLflow experiment workspace path.",
    )
    p.add_argument(
        "--test-prompt",
        default="What does alice prefer to eat?",
        help="Prompt sent to the deployed endpoint for shape assertions.",
    )
    p.add_argument(
        "--deploy-timeout-seconds", type=int, default=1800,
        help="Wall-clock cap on the deploy+readiness wait. Default 1800 (30 min).",
    )
    p.add_argument(
        "--keep", action="store_true",
        help="Skip endpoint deletion on success (default: delete).",
    )
    p.add_argument(
        "--delete-on-failure", action="store_true",
        help="Delete the endpoint even if deploy/invoke fails.",
    )
    p.add_argument(
        "--strict-traces", action="store_true",
        help=("Exit code 3 if trace assertions fail. Default: warn-only "
              "(trace queries are best-effort)."),
    )
    p.add_argument(
        "--json-output", action="store_true",
        help="Suppress progress; emit a single JSON object on stdout at the end.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print the resolved plan and exit without touching the workspace.",
    )
    p.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color in stderr output.",
    )
    return p


def _build_plan(args: argparse.Namespace) -> Plan:
    model_name = args.model_name or _derive_model_name(args.module)
    registered_model_name = f"{args.catalog}.{args.schema}.{model_name}"
    deploy_argv = [
        "uv", "run", "--no-sync", "apx", "deploy",
        "--module", args.module,
        "--model", args.llm_endpoint,
        "--name", registered_model_name,
        "--experiment", args.experiment,
    ]
    return Plan(
        module_spec=args.module,
        profile=args.profile,
        catalog=args.catalog,
        schema=args.schema,
        model_name=model_name,
        registered_model_name=registered_model_name,
        llm_endpoint=args.llm_endpoint,
        experiment=args.experiment,
        test_prompt=args.test_prompt,
        deploy_timeout_seconds=args.deploy_timeout_seconds,
        keep=args.keep,
        delete_on_failure=args.delete_on_failure,
        strict_traces=args.strict_traces,
        cwd=PYTHON_DIR,
        deploy_argv=deploy_argv,
    )


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


def _check_profile(profile: str, log: Logger) -> None:
    """Verify the Databricks CLI profile authenticates."""
    log.info(f"checking profile {profile!r} via `databricks current-user me`")
    proc = subprocess.run(
        ["databricks", "--profile", profile, "current-user", "me"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"databricks CLI auth failed for profile {profile!r}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _check_or_create_schema(plan: Plan, log: Logger) -> None:
    """Ensure catalog.schema exists; create schema if missing."""
    log.info(f"checking UC schema {plan.catalog}.{plan.schema}")
    from databricks.sdk import WorkspaceClient

    ws = WorkspaceClient(profile=plan.profile)
    # Catalog must exist — we don't auto-create catalogs.
    try:
        ws.catalogs.get(name=plan.catalog)
    except Exception as e:
        raise RuntimeError(
            f"Catalog {plan.catalog!r} not found or not accessible: {e}"
        ) from e

    full = f"{plan.catalog}.{plan.schema}"
    try:
        ws.schemas.get(full_name=full)
        log.ok(f"schema {full} exists")
    except Exception:
        log.info(f"schema {full} missing — creating")
        ws.schemas.create(name=plan.schema, catalog_name=plan.catalog)
        log.ok(f"created schema {full}")


def _check_module_imports(plan: Plan, log: Logger) -> None:
    """Verify the agent module imports cleanly (from python/ cwd)."""
    log.info(f"importing {plan.module_spec} (cwd={plan.cwd})")
    module_path, _, variable = plan.module_spec.partition(":")
    if not module_path or not variable:
        raise RuntimeError(
            f"Bad --module spec {plan.module_spec!r}; expected MODULE:VARIABLE."
        )
    # Run as a subprocess so import side-effects don't leak into our process.
    proc = subprocess.run(
        ["uv", "run", "--no-sync", "python", "-c",
         f"import importlib, sys; m = importlib.import_module({module_path!r}); "
         f"assert hasattr(m, {variable!r}), 'no attr {variable}'; "
         f"print('ok')"],
        cwd=str(plan.cwd),
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"agent module {plan.module_spec!r} failed to import:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    log.ok(f"{plan.module_spec} imports cleanly")


# ---------------------------------------------------------------------------
# Deploy via apx CLI subprocess
# ---------------------------------------------------------------------------


def _run_apx_deploy(plan: Plan, log: Logger) -> subprocess.CompletedProcess[str]:
    """Run ``apx deploy`` as a subprocess from python/ cwd."""
    env = os.environ.copy()
    env.setdefault("DATABRICKS_CONFIG_PROFILE", plan.profile)
    # Defensive — keep this off even if apx deploy sets it internally.
    env["MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING"] = "false"
    log.info("running: " + " ".join(plan.deploy_argv))
    log.info(f"  cwd={plan.cwd}")
    proc = subprocess.run(
        plan.deploy_argv,
        cwd=str(plan.cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # Stream the tail to stderr so the user sees the failure context.
        log.err("apx deploy failed — last 50 lines of combined output:")
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in combined.splitlines()[-50:]:
            print(line, file=sys.stderr)
        raise RuntimeError(
            f"apx deploy exited {proc.returncode} for {plan.registered_model_name}"
        )
    return proc


def _resolve_deployment(plan: Plan, log: Logger) -> Any:
    """Look up the Deployment record for the just-logged model.

    Calls ``databricks.agents.get_deployments`` and returns the latest
    deployment object (has endpoint_name, endpoint_url, review_app_url).
    """
    from databricks import agents

    log.info(f"looking up deployment for {plan.registered_model_name}")
    deployments = agents.get_deployments(plan.registered_model_name)
    if not deployments:
        raise RuntimeError(
            f"No deployments returned for {plan.registered_model_name}. "
            f"Did databricks.agents.deploy run?"
        )
    # Sort by model_version (string) — newest last; pick newest.
    def _v(d: Any) -> int:
        try:
            return int(d.model_version)
        except (TypeError, ValueError):
            return -1
    deployments = sorted(deployments, key=_v)
    return deployments[-1]


# ---------------------------------------------------------------------------
# Readiness wait
# ---------------------------------------------------------------------------


def _wait_for_ready(endpoint: str, profile: str, timeout: int, log: Logger) -> None:
    """Poll ``serving-endpoints get`` with exponential backoff until READY."""
    from databricks.sdk import WorkspaceClient

    ws = WorkspaceClient(profile=profile)
    start = time.monotonic()
    sleep_s = 15.0
    cap_s = 120.0
    last_state = "UNKNOWN"
    last_config_state = "UNKNOWN"
    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            raise RuntimeError(
                f"Endpoint {endpoint} not READY after {elapsed:.0f}s "
                f"(last state={last_state}, config={last_config_state})."
            )
        try:
            ep = ws.serving_endpoints.get(endpoint)
            state = getattr(ep.state, "ready", None)
            last_state = str(state) if state is not None else "UNKNOWN"
            config_update = getattr(ep.state, "config_update", None)
            last_config_state = str(config_update) if config_update is not None else "UNKNOWN"
        except Exception as e:  # pragma: no cover — network blip
            log.warn(f"poll error (will retry): {e}")
            last_state = "ERROR"

        log.info(
            f"endpoint {endpoint}: ready={last_state} "
            f"config_update={last_config_state} elapsed={elapsed:.0f}s"
        )
        # ServingEndpointStateReady.READY is the terminal good state.
        if "READY" in last_state and "NOT_UPDATING" in last_config_state:
            log.ok(f"endpoint {endpoint} is READY")
            return
        if "READY" in last_state and "UPDATE_FAILED" in last_config_state:
            raise RuntimeError(
                f"Endpoint {endpoint}: config update failed ({last_config_state})."
            )

        time.sleep(sleep_s)
        sleep_s = min(sleep_s * 1.5, cap_s)


# ---------------------------------------------------------------------------
# Invoke + assert
# ---------------------------------------------------------------------------


def _invoke_endpoint(
    endpoint: str, profile: str, prompt: str, log: Logger,
) -> tuple[dict[str, Any], str]:
    """POST to /serving-endpoints/<name>/invocations with the ChatAgent shape."""
    from databricks.sdk import WorkspaceClient

    ws = WorkspaceClient(profile=profile)
    host = ws.config.host.rstrip("/")
    url = f"{host}/serving-endpoints/{endpoint}/invocations"
    # Prefer a PAT if the profile has one; fall back to OAuth.
    token = ws.config.token
    if not token:
        try:
            token = ws.config.oauth_token().access_token
        except Exception as e:
            raise RuntimeError(f"Failed to mint OAuth token for invocation: {e}") from e

    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "custom_inputs": {},
    }
    log.info(f"POST {url}")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} from {url}")
            body_raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} from invocations: {body[:500]}") from e

    try:
        body = json.loads(body_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invocations returned non-JSON: {body_raw[:500]}") from e

    return body, body_raw


def _assert_response_shape(body: dict[str, Any]) -> str:
    """Assert the body looks like a ChatAgent response. Return assistant text."""
    if not isinstance(body, dict):
        raise RuntimeError(f"response is not a dict: {type(body).__name__}")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RuntimeError(f"response has no `messages` list: keys={list(body.keys())}")
    # Find at least one assistant message with non-empty content.
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        # ChatAgent responses occasionally wrap content as list-of-parts.
        if isinstance(content, list):
            joined = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            ).strip()
            if joined:
                return joined
    raise RuntimeError(
        "response had no assistant message with non-empty content; "
        f"messages={messages!r}"
    )


# ---------------------------------------------------------------------------
# Trace assertions — best-effort
# ---------------------------------------------------------------------------


def _check_traces(experiment: str, profile: str, log: Logger) -> tuple[int, list[str]]:
    """Best-effort check that a trace landed with apx.* span attrs."""
    try:
        import mlflow
        from mlflow.tracking import MlflowClient
    except ImportError:
        log.warn("mlflow not installed — skipping trace assertions")
        return 0, []
    # Point MLflow at the workspace using the same profile.
    os.environ.setdefault("DATABRICKS_CONFIG_PROFILE", profile)
    try:
        mlflow.set_tracking_uri("databricks")
    except Exception as e:
        log.warn(f"mlflow.set_tracking_uri failed: {e}")
        return 0, []

    client = MlflowClient()
    try:
        exp = client.get_experiment_by_name(experiment)
    except Exception as e:
        log.warn(f"could not fetch experiment {experiment!r}: {e}")
        return 0, []
    if exp is None:
        log.warn(f"experiment {experiment!r} not found")
        return 0, []

    # Look back 10 minutes — covers slow trace flushes.
    since_ms = int((time.time() - 600) * 1000)
    try:
        traces = client.search_traces(
            experiment_ids=[exp.experiment_id],
            filter_string=f"timestamp_ms > {since_ms}",
            max_results=10,
        )
    except Exception as e:
        log.warn(f"search_traces failed: {e}")
        return 0, []

    if not traces:
        log.warn(f"no traces found in {experiment!r} since {since_ms}")
        return 0, []

    apx_attrs: set[str] = set()
    for t in traces:
        spans = getattr(t.data, "spans", None) or []
        for span in spans:
            attrs = getattr(span, "attributes", None) or {}
            for key in attrs:
                if isinstance(key, str) and key.startswith("apx."):
                    apx_attrs.add(key)

    return len(traces), sorted(apx_attrs)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _delete_endpoint(endpoint: str, profile: str, log: Logger) -> None:
    """Delete the serving endpoint via databricks-agents."""
    from databricks import agents

    log.info(f"deleting endpoint {endpoint}")
    agents.delete_deployment(endpoint_name=endpoint)
    log.ok(f"endpoint {endpoint} deleted")


# ---------------------------------------------------------------------------
# Dry-run printer
# ---------------------------------------------------------------------------


def _print_dry_run(plan: Plan, log: Logger) -> None:
    """Print the resolved plan, including derived names and subprocess argv."""
    # Endpoint name is set by databricks-agents; we predict the conventional
    # form (catalog-schema-model with dots->dashes). The actual name is read
    # from get_deployments after deploy, not computed.
    predicted_endpoint = (
        "agents_"
        + plan.registered_model_name.replace(".", "-")
    )
    log.step("DRY RUN — plan only, no workspace contact")
    summary = {
        "module": plan.module_spec,
        "profile": plan.profile,
        "registered_model_name": plan.registered_model_name,
        "llm_endpoint": plan.llm_endpoint,
        "experiment": plan.experiment,
        "test_prompt": plan.test_prompt,
        "deploy_timeout_seconds": plan.deploy_timeout_seconds,
        "keep": plan.keep,
        "delete_on_failure": plan.delete_on_failure,
        "predicted_endpoint_name": predicted_endpoint + "  (resolved from get_deployments after deploy)",
        "subprocess_cwd": str(plan.cwd),
        "subprocess_argv": plan.deploy_argv,
        "invocations_url_template": (
            f"https://<host>/serving-endpoints/{predicted_endpoint}/invocations"
        ),
    }
    for k, v in summary.items():
        if isinstance(v, list):
            print(f"  {k}: {' '.join(v)}", file=sys.stderr)
        else:
            print(f"  {k}: {v}", file=sys.stderr)
    log.step("Pipeline that WOULD run (if not --dry-run)")
    for i, step in enumerate(
        [
            "Pre-flight: databricks current-user me",
            f"Pre-flight: get/create UC schema {plan.catalog}.{plan.schema}",
            f"Pre-flight: import {plan.module_spec} from {plan.cwd}",
            "Deploy: subprocess apx deploy (env MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING=false)",
            "Resolve: databricks.agents.get_deployments(<model>) -> endpoint_name",
            f"Wait: poll serving-endpoints.get until READY (timeout {plan.deploy_timeout_seconds}s)",
            "Invoke: POST /serving-endpoints/<ep>/invocations with --test-prompt",
            "Assert: HTTP 200 + messages[*].role=='assistant' with non-empty content",
            f"Trace: MLflow search_traces in {plan.experiment} for apx.* span attrs",
            "Cleanup: delete endpoint (skipped if --keep)",
        ],
        start=1,
    ):
        print(f"  {i}. {step}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------


def _run_pipeline(plan: Plan, log: Logger, result: Result) -> int:
    """Run the full pipeline. Mutates ``result``. Returns exit code."""
    # ---- Pre-flight ----
    log.step("Pre-flight")
    try:
        _check_profile(plan.profile, log)
        _check_or_create_schema(plan, log)
        _check_module_imports(plan, log)
    except Exception as e:
        result.error = f"pre-flight: {e}"
        log.err(str(e))
        return EXIT_DEPLOY

    # ---- Deploy ----
    log.step("Deploy")
    deploy_start = time.monotonic()
    try:
        _run_apx_deploy(plan, log)
    except Exception as e:
        result.error = f"deploy: {e}"
        result.deploy_seconds = time.monotonic() - deploy_start
        log.err(str(e))
        return EXIT_DEPLOY

    # ---- Resolve deployment ----
    try:
        deployment = _resolve_deployment(plan, log)
    except Exception as e:
        result.error = f"resolve_deployment: {e}"
        result.deploy_seconds = time.monotonic() - deploy_start
        log.err(str(e))
        return EXIT_DEPLOY

    result.endpoint = deployment.endpoint_name
    result.endpoint_url = deployment.endpoint_url
    result.review_app_url = deployment.review_app_url
    result.query_endpoint = deployment.query_endpoint
    log.ok(f"endpoint: {result.endpoint}")
    log.ok(f"review app: {result.review_app_url}")

    # ---- Wait for ready ----
    try:
        _wait_for_ready(
            result.endpoint, plan.profile, plan.deploy_timeout_seconds, log,
        )
    except Exception as e:
        result.error = f"wait_for_ready: {e}"
        result.deploy_seconds = time.monotonic() - deploy_start
        log.err(str(e))
        # Try to dump build logs if the SDK exposes them
        try:
            from databricks.sdk import WorkspaceClient
            ws = WorkspaceClient(profile=plan.profile)
            logs_resp = ws.serving_endpoints.get_logs(
                name=result.endpoint, served_model_name=result.endpoint,
            )
            tail = (getattr(logs_resp, "logs", "") or "").splitlines()[-50:]
            if tail:
                log.err("Last 50 lines of build logs:")
                for line in tail:
                    print(line, file=sys.stderr)
        except Exception:
            pass
        return EXIT_DEPLOY

    result.deploy_seconds = time.monotonic() - deploy_start

    # ---- Invoke ----
    log.step("Invoke")
    invoke_start = time.monotonic()
    try:
        body, _ = _invoke_endpoint(
            result.endpoint, plan.profile, plan.test_prompt, log,
        )
        text = _assert_response_shape(body)
    except Exception as e:
        result.error = f"invoke: {e}"
        result.invoke_seconds = time.monotonic() - invoke_start
        log.err(str(e))
        return EXIT_INVOKE
    result.invoke_seconds = time.monotonic() - invoke_start
    result.response_text = text
    log.ok(f"assistant reply ({len(text)} chars): {text[:120]}")

    # ---- Traces ----
    log.step("Trace assertions (best-effort)")
    n_traces, apx_attrs = _check_traces(plan.experiment, plan.profile, log)
    result.trace_count = n_traces
    result.apx_span_attrs_found = apx_attrs
    if n_traces == 0:
        log.warn("no traces found within the last 10 minutes")
    else:
        log.ok(f"found {n_traces} trace(s); apx.* attrs: {apx_attrs or '(none)'}")
    if plan.strict_traces and (n_traces == 0 or not apx_attrs):
        result.error = "strict trace assertions failed"
        return EXIT_TRACE

    # Pipeline succeeded.
    result.ok = True
    return EXIT_OK


def _maybe_cleanup(plan: Plan, log: Logger, result: Result, primary_exit: int) -> int:
    """Delete endpoint based on flags + primary_exit. Returns final exit code.

    Rule: a deploy/invoke failure code dominates a follow-on cleanup failure.
    Cleanup errors are logged but do not overwrite the primary failure code.
    """
    if result.endpoint is None:
        return primary_exit

    should_cleanup = False
    if primary_exit == EXIT_OK and not plan.keep:
        should_cleanup = True
    elif primary_exit != EXIT_OK and plan.delete_on_failure:
        should_cleanup = True

    if not should_cleanup:
        if primary_exit == EXIT_OK:
            log.info(f"--keep set; leaving endpoint {result.endpoint} up")
        return primary_exit

    log.step("Cleanup")
    try:
        _delete_endpoint(result.endpoint, plan.profile, log)
    except Exception as e:
        log.err(f"cleanup failed: {e}")
        if primary_exit == EXIT_OK:
            # Cleanup-only failure: surface exit 4.
            result.error = f"cleanup: {e}"
            return EXIT_CLEANUP
        # Else: keep the dominant failure code.
    return primary_exit


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    plan = _build_plan(args)
    log = Logger(quiet=args.json_output, color=not args.no_color)

    if args.dry_run:
        _print_dry_run(plan, log)
        if args.json_output:
            print(json.dumps({"ok": True, "dry_run": True, "plan": plan.to_dict()}, indent=2))
        return EXIT_OK

    result = Result()
    primary_exit = _run_pipeline(plan, log, result)
    final_exit = _maybe_cleanup(plan, log, result, primary_exit)
    result.exit_code = final_exit
    result.ok = final_exit == EXIT_OK

    if args.json_output:
        print(json.dumps(
            {
                "ok": result.ok,
                "endpoint": result.endpoint,
                "endpoint_url": result.endpoint_url,
                "review_app_url": result.review_app_url,
                "query_endpoint": result.query_endpoint,
                "response_text": result.response_text,
                "trace_count": result.trace_count,
                "apx_span_attrs_found": result.apx_span_attrs_found,
                "deploy_seconds": result.deploy_seconds,
                "invoke_seconds": result.invoke_seconds,
                "exit_code": result.exit_code,
                "error": result.error,
            },
            indent=2,
        ))
    elif result.ok:
        log.step("DONE")
        log.ok(f"endpoint: {result.endpoint}")
        log.ok(f"review app: {result.review_app_url}")
    return final_exit


if __name__ == "__main__":
    sys.exit(main())
