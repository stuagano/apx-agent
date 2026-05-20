"""Hot-swap an Apps-target agent's LLM endpoint via bundle var re-deploy.

The Model Serving hot-swap (``_hot_swap.py``) rewrites
``environment_vars`` on a served entity. Apps has no equivalent — the
container env is set at deploy time. The Apps analog re-deploys the
bundle with a different ``--var llm_endpoint_name=NEW``, which
restarts the App with the new env.

See ``docs/apps-canary-hotswap-design.md`` for the rationale.

The bundle var name is configurable but defaults to
``llm_endpoint_name`` — the convention used by the scaffold template
and the worked examples (see ``python/examples/memory_demo/databricks.yml``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


DEFAULT_LLM_VAR_NAME = "llm_endpoint_name"


@dataclass(frozen=True)
class AppsHotSwapResult:
    """Outcome of an Apps-target hot-swap.

    Attributes:
        app_name: Workspace App name that was redeployed.
        bundle_target: DAB target the redeploy was scoped to.
        var_name: The bundle variable whose value was swapped.
        new_value: The value the var was set to.
        previous_value: The value the var held before the swap (taken
            from ``variables.<name>.default`` in the YAML). May be None
            if the var had no default.
    """

    app_name: str
    bundle_target: str
    var_name: str
    new_value: str
    previous_value: str | None


# Type alias: subprocess seam — see ``_canary_apps.RunCmd``.
RunCmd = Callable[..., Any]


def read_var_default(doc: dict[str, Any], var_name: str) -> str | None:
    """Return the current default value for ``variables.<var_name>`` in the bundle.

    The bundle var system supports ``variables: { foo: default_value }``
    (short form) and ``variables: { foo: { default: default_value,
    description: "..." } }`` (long form). Handle both.
    """
    variables = doc.get("variables")
    if not isinstance(variables, dict):
        return None
    entry = variables.get(var_name)
    if entry is None:
        return None
    if isinstance(entry, dict):
        default = entry.get("default")
        return str(default) if default is not None else None
    # short form — the value itself is the default
    return str(entry)


def hot_swap_apps(
    *,
    cwd: Path,
    app_name: str,
    new_value: str,
    run_cmd: RunCmd,
    profile: str | None = None,
    bundle_target: str = "prod",
    var_name: str = DEFAULT_LLM_VAR_NAME,
) -> AppsHotSwapResult:
    """Re-deploy the Apps bundle with a different LLM endpoint var.

    Args:
        cwd: Bundle root (contains ``databricks.yml``).
        app_name: Workspace App name (used for the result; the actual
            redeploy targets the whole bundle).
        new_value: New endpoint name (e.g.
            ``databricks-claude-opus-4-7``).
        run_cmd: Seam for ``databricks`` CLI invocations.
        profile: ``--profile`` value, or None for default.
        bundle_target: DAB target to redeploy. Default ``"prod"``.
        var_name: Variable name to override. Default
            ``"llm_endpoint_name"``.

    Returns:
        ``AppsHotSwapResult`` with the previous default value so the
        operator can roll back without re-discovering it.

    Raises:
        RuntimeError: when the bundle has no such variable, or when
            ``bundle deploy`` returns non-zero.
    """
    import yaml

    yml_path = cwd / "databricks.yml"
    if not yml_path.exists():
        raise RuntimeError(
            f"No databricks.yml at {yml_path} — hot-swap --target apps "
            "requires a scaffolded Apps bundle."
        )
    doc = yaml.safe_load(yml_path.read_text()) or {}
    previous = read_var_default(doc, var_name)
    if previous is None and (doc.get("variables") or {}).get(var_name) is None:
        raise RuntimeError(
            f"databricks.yml has no `variables.{var_name}` entry. "
            "Hot-swap relies on a bundle var to pivot the LLM endpoint — "
            f"add `variables: {{ {var_name}: <current-endpoint> }}` to the bundle, "
            "or pass --var-name <other-var-name> if your scaffold uses a different name."
        )

    deploy_proc = run_cmd(
        ["bundle", "deploy", "--target", bundle_target,
         "--var", f"{var_name}={new_value}"],
        profile=profile,
    )
    if getattr(deploy_proc, "returncode", 0) != 0:
        raise RuntimeError(
            f"bundle deploy --target {bundle_target} --var {var_name}=<new> failed "
            f"(exit {deploy_proc.returncode}). stderr: "
            f"{getattr(deploy_proc, 'stderr', '')!r}"
        )

    return AppsHotSwapResult(
        app_name=app_name,
        bundle_target=bundle_target,
        var_name=var_name,
        new_value=new_value,
        previous_value=previous,
    )
