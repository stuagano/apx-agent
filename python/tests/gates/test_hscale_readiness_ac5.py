"""AC-5: emitted databricks.yml carries native Apps scaling fields and APX_DECLARED_INSTANCES."""
from __future__ import annotations

from ctk import Artifact, verify

from apx_agent import AgentConfig
from apx_agent._models import DeployConfig, SessionBackendConfig
from apx_agent._project_gen import _build_databricks_yml


def test_bundle_emits_declared_instances_env_and_native_scaling_fields(tmp_path) -> None:
    cfg = AgentConfig(
        name="t",
        deploy=DeployConfig(instances=3),
        session=SessionBackendConfig(type="lakebase", host="${LAKEBASE_HOST}", database="apx"),
    )
    path = tmp_path / "databricks.yml"
    path.write_text(_build_databricks_yml(cfg))

    verify(
        Artifact(
            str(path),
            min_bytes=1,
            must_contain='compute_min_instances: 3\n      compute_max_instances: 3',
        ),
    )
    verify(
        Artifact(
            str(path),
            min_bytes=1,
            must_contain='- name: APX_DECLARED_INSTANCES\n            value: "3"',
        ),
    )


def test_bundle_emits_autoscale_bounds(tmp_path) -> None:
    cfg = AgentConfig(
        name="t",
        deploy=DeployConfig(autoscale={"min": 2, "max": 5}),
        session=SessionBackendConfig(type="lakebase", host="${LAKEBASE_HOST}", database="apx"),
    )
    path = tmp_path / "databricks.yml"
    path.write_text(_build_databricks_yml(cfg))

    verify(
        Artifact(
            str(path),
            min_bytes=1,
            must_contain='compute_min_instances: 2\n      compute_max_instances: 5',
        ),
    )
    verify(
        Artifact(
            str(path),
            min_bytes=1,
            must_contain='- name: APX_DECLARED_INSTANCES\n            value: "5"',
        ),
    )


def test_no_deploy_block_emits_no_env_or_scaling_fields() -> None:
    # NFR-2: an app with no [tool.apx.agent.deploy] behaves as before (no env, no scaling fields).
    yml = _build_databricks_yml(AgentConfig(name="t"))
    assert "APX_DECLARED_INSTANCES" not in yml
    assert "compute_min_instances" not in yml
    assert "compute_max_instances" not in yml
