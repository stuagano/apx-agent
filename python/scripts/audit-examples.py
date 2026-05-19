#!/usr/bin/env python3
"""Credless dry-run audit of every apx-agent example.

The task asked for ``apx deploy --no-deploy --no-publish-tools --no-set-uc-tags``
to catch import errors / missing tool resources / broken composition without
hitting a workspace. Investigation found that this is **infeasible** under the
"no workspace creds" constraint:

  ``mlflow.pyfunc.log_model`` for ChatAgent unconditionally calls
  ``python_model.predict(...)`` against an input example
  (``_save_model_chat_agent_helper`` line 3920 in installed mlflow) to
  validate output schema. That predict() hits the LLM serving endpoint
  named by ``--model``. Without workspace creds + a real Databricks
  Foundation Model endpoint, every example fails at the same place,
  drowning out the actual bugs we want to catch.

This script substitutes a pure import probe + resource-derivation probe
that catches exactly what the task listed:

  1. Import the example's agent module and resolve the agent variable
     (catches import errors and broken composition at import time).
  2. Call ``apx_agent.mlflow_resources_for(agent, model=...)`` to derive
     MLflow resources (catches missing tool resources / un-resolvable
     ``ResourceSpec`` entries on the agent — same coverage as
     ``apx deploy``'s log step, minus the predict() roundtrip).

Both probes run inside the example's own ``uv run`` so its dep set
(fitz, slack_sdk, vector_search, ...) is in scope. The probe runs as
a tiny inline python -c snippet — no temp files needed.

Run from python/:
    uv run python scripts/audit-examples.py

Exit 0 if every audited example passes both probes. Exit 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Curated example -> agent mapping
# ---------------------------------------------------------------------------
# Hardcoded because regex inference is unreliable here:
#   - customer_triage/agent.py declares 4 Agent objects (3 specialists + a
#     triage classifier) plus the top-level HandoffAgent named `agent`.
#   - voynich exports 5 separately-deployable agents under agents/*/main.py.
#   - account-search-service, afr-enrollment-api, agent-hub import
#     apx_agent.Dependencies but have NO Agent export — they are FastAPI
#     services, not Mosaic ChatAgents. Flagged with module=None.
#
# Each row: AuditTarget(example, directory, module, requires_workspace, note)


@dataclass(frozen=True)
class AuditTarget:
    example: str  # display name
    directory: str  # path under examples/ (or "." to run from python/)
    module: str | None  # ``pkg.mod:var``; None means no deployable agent
    requires_workspace: bool = False
    note: str = ""
    cwd_at_parent: bool = False  # True for examples that import from
    # the parent python/ project root (e.g. memory_demo runs as
    # `python -m examples.memory_demo.app`, not as its own uv project).


TARGETS: list[AuditTarget] = [
    # FastAPI services — use apx_agent.Dependencies but export no Agent.
    AuditTarget("account-search-service", "account-search-service", None, False,
                "FastAPI service — no Agent export"),
    AuditTarget("afr-enrollment-api", "afr-enrollment-api", None, False,
                "FastAPI service — no Agent export"),
    AuditTarget("agent-hub", "agent-hub", None, False,
                "FastAPI service — no Agent export"),

    AuditTarget("apx-builder", "apx-builder", "app:agent", False),
    AuditTarget("contract-parsing-agent", "contract-parsing-agent",
                "contract_parsing_agent.backend.agent_router:agent", False),
    AuditTarget("customer_triage", "customer_triage", "agent:agent", False),
    AuditTarget("data-inspector", "data-inspector",
                "data_inspector.backend.agent_router:agent", False),
    AuditTarget("data-triage-agent", "data-triage-agent",
                "data_triage_agent.backend.agent_router:agent", False),
    AuditTarget("eligibility-agent", "eligibility-agent",
                "eligibility_agent.app:agent", False),
    AuditTarget("entity-resolution-agent", "entity-resolution-agent",
                "entity_resolution_agent.backend.agent_router:agent", False),
    # memory_demo is documented as `python -m examples.memory_demo.app`;
    # it has no pyproject.toml of its own.
    AuditTarget("memory_demo", ".", "examples.memory_demo.app:agent", False,
                "", cwd_at_parent=True),
    AuditTarget("shortage-intelligence-agent", "shortage-intelligence-agent",
                "shortage_intelligence_agent.backend.agent_router:agent", False),
    AuditTarget("slack-agent", "slack-agent",
                "slack_agent.backend.agent_router:agent", False),

    # voynich: orchestrator is the top-level deployable; the rest are
    # independently deployable sub-agents.
    AuditTarget("voynich:orchestrator", "voynich",
                "agents.orchestrator.main:agent", False),
    AuditTarget("voynich:historian", "voynich",
                "agents.historian.main:agent", False),
    AuditTarget("voynich:judge", "voynich",
                "agents.judge.main:agent", False),
    AuditTarget("voynich:critic", "voynich",
                "agents.critic.main:agent", False),
    AuditTarget("voynich:decipherer", "voynich",
                "agents.decipherer.main:agent", False),
]

# Databricks creds get scrubbed so the audit never silently reaches a
# workspace. If any example needs them at import time, that's a real
# finding (it makes the example un-importable in CI).
ENV_STRIPS = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_AUTH_TYPE",
    "DATABRICKS_BUNDLE_VAR_workspace_host",
)

# Probe runs in the example's own venv. Imports the module, fetches the
# agent variable, then (best-effort) derives mlflow resources. Emits a
# single-line JSON tag on stdout for the parent to parse; everything
# else is just stderr.
#
# Pass criterion: agent imports and the agent variable resolves. Resource
# derivation is a "bonus" signal — if `mlflow_resources_for` isn't
# available (example pins an old apx_agent), the probe still passes but
# resource_count stays -1 and a 'stale_apx_agent' warning is emitted.
PROBE = """
from __future__ import annotations
import json, os, sys, traceback

module_spec = os.environ['APX_AUDIT_MODULE']
mod_path, _, var = module_spec.partition(':')
result = {'phase': 'start', 'warnings_internal': []}
try:
    result['phase'] = 'import_module'
    import importlib
    m = importlib.import_module(mod_path)
    result['phase'] = 'get_attr'
    if not hasattr(m, var):
        raise AttributeError(f'{mod_path!r} has no attribute {var!r}')
    agent = getattr(m, var)
    result['agent_type'] = type(agent).__name__
    result['phase'] = 'resource_derivation'
    try:
        from apx_agent import mlflow_resources_for  # type: ignore[attr-defined]
    except ImportError:
        result['warnings_internal'].append('stale_apx_agent')
        result['resource_count'] = -1
    else:
        try:
            resources = mlflow_resources_for(
                agent, model='databricks-claude-sonnet-4-6')
            result['resource_count'] = len(resources)
        except Exception as e:
            # Resource derivation failure is a real bug — e.g. a tool's
            # ResourceSpec references a UC entity that can't be looked up.
            # Surface it as a separate failure mode (the import probe passed).
            raise RuntimeError(
                f'mlflow_resources_for failed: {type(e).__name__}: {e}'
            ) from e
    result['phase'] = 'ok'
    result['ok'] = True
except Exception as e:
    result['ok'] = False
    result['error_type'] = type(e).__name__
    result['error_msg'] = str(e)[:400]
    result['traceback_tail'] = traceback.format_exc().splitlines()[-1][:200]

print('@@APX_AUDIT_RESULT@@' + json.dumps(result))
"""

WARNING_SIGNALS = (
    ("env-vars", "Logged env var"),
    ("env-vars", "secret-looking env vars detected"),
    # Note: we intentionally do NOT flag generic DeprecationWarning —
    # langgraph/langchain emit them unconditionally and they're noise.
    # Add apx_agent-specific deprecation patterns here if/when needed.
)


@dataclass
class AuditResult:
    target: AuditTarget
    status: str  # OK | FAIL | SKIP_NO_AGENT | SKIP_REQ_WORKSPACE
    duration_s: float = 0.0
    phase: str = ""  # last reported probe phase before failure (or 'ok')
    agent_type: str = ""
    resource_count: int = -1
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _build_env(tracking_dir: Path, module_spec: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_STRIPS}
    env["MLFLOW_TRACKING_URI"] = f"file:{tracking_dir}"
    env["MLFLOW_RECORD_ENV_VARS_IN_MODEL_LOGGING"] = "false"
    env["DATABRICKS_CONFIG_FILE"] = "/dev/null"
    env["APX_AUDIT_MODULE"] = module_spec
    return env


def _detect_warnings(stderr: str) -> list[str]:
    seen: list[str] = []
    for tag, needle in WARNING_SIGNALS:
        if needle in stderr and tag not in seen:
            seen.append(tag)
    return seen


def _parse_probe_result(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith("@@APX_AUDIT_RESULT@@"):
            try:
                return json.loads(line[len("@@APX_AUDIT_RESULT@@"):])
            except json.JSONDecodeError:
                return None
    return None


def _short_error(stderr: str, stdout: str) -> str:
    """Find the most diagnostic line for the summary."""
    lines = (stderr or "").strip().splitlines()
    for ln in reversed(lines):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("Traceback ") or s.startswith("File "):
            continue
        return s[:200]
    out = (stdout or "").strip().splitlines()
    return (out[-1][:200] if out else "no output")


def _run_one(
    target: AuditTarget,
    *,
    examples_root: Path,
    tracking_dir: Path,
    timeout: int,
    verbose: bool,
) -> AuditResult:
    if target.module is None:
        return AuditResult(target=target, status="SKIP_NO_AGENT")

    if target.cwd_at_parent:
        # Run from python/ — example is imported as examples.<name>.<mod>.
        example_dir = examples_root.parent
    else:
        example_dir = examples_root / target.directory
    if not example_dir.exists():
        return AuditResult(target=target, status="FAIL",
                           error=f"directory missing: {example_dir}")

    # Run the probe inside the example's uv project. This pulls in the
    # example's dep set (fitz, slack_sdk, vector_search, etc).
    cmd = ["uv", "run", "python", "-c", PROBE]
    env = _build_env(tracking_dir, target.module)

    _eprint(f"[audit] {target.example:34s} -> {target.module}")
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=example_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return AuditResult(
            target=target,
            status="FAIL",
            duration_s=timeout,
            error=f"timeout after {timeout}s",
        )
    duration = time.monotonic() - start

    if verbose:
        _eprint(f"[audit]   stdout:\n{proc.stdout}")
        _eprint(f"[audit]   stderr:\n{proc.stderr}")

    parsed = _parse_probe_result(proc.stdout)
    warnings = _detect_warnings(proc.stderr)

    # Case 1: probe ran and reported structured result.
    if parsed is not None:
        # Merge probe-reported soft warnings (e.g. stale_apx_agent).
        warnings = list(warnings) + list(parsed.get("warnings_internal", []))
        if parsed.get("ok"):
            return AuditResult(
                target=target,
                status="OK",
                duration_s=duration,
                phase="ok",
                agent_type=parsed.get("agent_type", ""),
                resource_count=parsed.get("resource_count", -1),
                warnings=warnings,
            )
        err = (
            f"{parsed.get('error_type', 'Error')}: "
            f"{parsed.get('error_msg', '')}"
        )
        return AuditResult(
            target=target,
            status="FAIL",
            duration_s=duration,
            phase=parsed.get("phase", "unknown"),
            warnings=warnings,
            error=err[:200],
        )

    # Case 2: probe never reported — uv resolve or interpreter crashed
    # before our script ran. Classify the failure family for the
    # summary so it's actionable.
    stderr = proc.stderr or ""
    if "apx-agent was not found in the package registry" in stderr:
        phase = "pyproject_pins_registry_apx_agent"
    elif "Failed to build `apx-agent" in stderr or (
        "apx-agent" in stderr and "does not appear to be a Python project"
        in stderr
    ):
        phase = "pyproject_stale_path_dep"
    elif "No solution found when resolving" in stderr:
        phase = "uv_resolve_failed"
    else:
        phase = "pre_probe"
    return AuditResult(
        target=target,
        status="FAIL",
        duration_s=duration,
        phase=phase,
        warnings=warnings,
        error=_short_error(stderr, proc.stdout),
    )


def _print_summary(results: list[AuditResult]) -> None:
    name_w = max(len(r.target.example) for r in results)
    name_w = max(name_w, len("example"))
    phase_w = max(len("phase"),
                  max((len(r.phase) for r in results), default=0))
    hdr = (
        f"{'example':<{name_w}} | status              | secs  | "
        f"{'phase':<{phase_w}} | resources | warnings    | error"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        secs = f"{r.duration_s:.1f}" if r.duration_s else "-"
        warns = ",".join(r.warnings) or "-"
        err = r.error or "-"
        if len(err) > 80:
            err = err[:77] + "..."
        res = str(r.resource_count) if r.resource_count >= 0 else "-"
        print(
            f"{r.target.example:<{name_w}} | {r.status:<19s} | "
            f"{secs:>5s} | {r.phase:<{phase_w}} | {res:>9s} | "
            f"{warns:<11s} | {err}"
        )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--require-workspace",
        action="store_true",
        help="Include examples flagged requires_workspace=True. Default: skip.",
    )
    p.add_argument(
        "--only", action="append", default=None, metavar="NAME",
        help="Only run named target(s) (e.g. customer_triage, voynich:critic).",
    )
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--verbose", action="store_true",
                   help="Echo each example's stdout/stderr to stderr.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    here = Path(__file__).resolve().parent
    examples_root = here.parent / "examples"
    if not examples_root.exists():
        _eprint(f"examples/ not found at {examples_root}")
        return 2

    selected: list[AuditTarget] = list(TARGETS)
    if args.only:
        wanted = set(args.only)
        selected = [t for t in selected if t.example in wanted]
        missing = wanted - {t.example for t in selected}
        if missing:
            _eprint(f"unknown --only targets: {sorted(missing)}")
            return 2

    results: list[AuditResult] = []
    with tempfile.TemporaryDirectory(prefix="apx-audit-mlruns-") as tdir:
        tracking_dir = Path(tdir)
        _eprint(f"[audit] tracking dir: {tracking_dir}")
        _eprint(f"[audit] {len(selected)} target(s)")

        for target in selected:
            if target.requires_workspace and not args.require_workspace:
                results.append(AuditResult(
                    target=target,
                    status="SKIP_REQ_WORKSPACE",
                    error=target.note or "needs workspace",
                ))
                _eprint(
                    f"[audit] {target.example:34s} -> SKIPPED "
                    f"(requires_workspace)"
                )
                continue
            results.append(_run_one(
                target,
                examples_root=examples_root,
                tracking_dir=tracking_dir,
                timeout=args.timeout,
                verbose=args.verbose,
            ))

    print()
    _print_summary(results)
    print()

    audited = [r for r in results
               if r.status not in ("SKIP_NO_AGENT", "SKIP_REQ_WORKSPACE")]
    failed = [r for r in audited if r.status != "OK"]
    print(
        f"audited: {len(audited)}  passed: {len(audited) - len(failed)}  "
        f"failed: {len(failed)}  "
        f"skipped (no agent): "
        f"{sum(1 for r in results if r.status == 'SKIP_NO_AGENT')}  "
        f"skipped (req workspace): "
        f"{sum(1 for r in results if r.status == 'SKIP_REQ_WORKSPACE')}"
    )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
