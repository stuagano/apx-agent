"""AC-5: emitted databricks.yml carries APX_DECLARED_INSTANCES env for a declared block."""
from __future__ import annotations

from ctk import Artifact, verify

from apx_agent import AgentConfig
from apx_agent._models import DeployConfig, SessionBackendConfig
from apx_agent._project_gen import _build_databricks_yml


def test_bundle_emits_declared_instances_env(tmp_path) -> None:
    cfg = AgentConfig(
        name="t",
        deploy=DeployConfig(instances=3),
        session=SessionBackendConfig(type="lakebase", host="${LAKEBASE_HOST}", database="apx"),
    )
    path = tmp_path / "databricks.yml"
    path.write_text(_build_databricks_yml(cfg))

    verify(
        Artifact(str(path), min_bytes=1, must_contain="APX_DECLARED_INSTANCES"),
        Artifact(str(path), must_contain='value: "3"'),
    )


def test_no_deploy_block_emits_no_env() -> None:
    # NFR-2: an app with no [tool.apx.agent.deploy] behaves as before (no env).
    yml = _build_databricks_yml(AgentConfig(name="t"))
    assert "APX_DECLARED_INSTANCES" not in yml
