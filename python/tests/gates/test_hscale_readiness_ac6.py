"""AC-6: DeployConfig validation — range 1-5, instances/autoscale exclusive, min<=max."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apx_agent._models import AutoscaleConfig, DeployConfig


def test_deploy_config_validation() -> None:
    # Out of range.
    with pytest.raises(ValidationError):
        DeployConfig(instances=6)
    with pytest.raises(ValidationError):
        DeployConfig(instances=0)

    # Mutual exclusivity.
    with pytest.raises(ValidationError):
        DeployConfig(instances=2, autoscale=AutoscaleConfig(min=1, max=3))

    # autoscale min <= max.
    with pytest.raises(ValidationError):
        DeployConfig(autoscale=AutoscaleConfig(min=4, max=2))

    # StrictInt: reject string / float coercion ("1", 1.0).
    with pytest.raises(ValidationError):
        DeployConfig(instances="3")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DeployConfig(instances=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AutoscaleConfig(min="1", max=2)  # type: ignore[arg-type]

    # Valid shapes.
    assert DeployConfig(instances=3).instances == 3
    assert DeployConfig(autoscale=AutoscaleConfig(min=2, max=5)).autoscale.max == 5
