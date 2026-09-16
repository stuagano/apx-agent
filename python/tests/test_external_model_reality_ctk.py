"""Claim-vs-reality (ctk) for declared external-model endpoints (AC-12).

The built endpoint payload + the emitted ``databricks.yml`` resources are
written to disk and read back through ``ctk.verify(Artifact(...))`` — asserting
they carry the wiring that makes them real (``external_model.provider``
lowercase, an ``ai_gateway`` guardrails block, and a ``CAN_QUERY``
``serving_endpoint`` resource), not merely that a file exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".ctk"))
import ctk  # noqa: E402

from apx_agent._external_model import (  # noqa: E402
    build_endpoint_payload,
    parse_model_scheme,
)
from apx_agent._models import GatewayConfig  # noqa: E402
from apx_agent._resources import (  # noqa: E402
    ResourceSpec,
    resources_to_databricks_yml,
)


def test_emitted_external_model_spec_is_real(tmp_path: Path) -> None:
    spec = parse_model_scheme("bedrock:anthropic.claude-3-5-sonnet")
    assert spec is not None
    payload = build_endpoint_payload(spec, GatewayConfig(credential="my_cred"))
    yml = resources_to_databricks_yml(
        [ResourceSpec("serving_endpoint", spec.endpoint_name)]
    )

    payload_path = tmp_path / "endpoint_payload.json"
    payload_path.write_text(json.dumps(payload, indent=2))
    yml_path = tmp_path / "databricks_resources.json"
    yml_path.write_text(json.dumps(yml, indent=2))

    # Read the payload back: provider is present + lowercase, ai_gateway
    # guardrails block present, usage tracking enabled.
    ctk.verify(
        ctk.Artifact(
            str(payload_path),
            min_bytes=len("amazon-bedrock"),
            must_contain="amazon-bedrock",
        ),
        ctk.Artifact(str(payload_path), must_contain='"guardrails"'),
        ctk.Artifact(str(payload_path), must_contain='"usage_tracking_config"'),
        ctk.Artifact(str(payload_path), must_contain=spec.endpoint_name),
        # CAN_QUERY serving_endpoint resource emitted for the declared endpoint.
        ctk.Artifact(str(yml_path), must_contain="CAN_QUERY"),
        ctk.Artifact(str(yml_path), must_contain="serving_endpoint"),
        ctk.Artifact(str(yml_path), must_contain=spec.endpoint_name),
    )

    # provider must be lowercase (serving API requirement) — assert on the
    # read-back object, not just substring presence.
    reread = json.loads(payload_path.read_text())
    provider = reread["config"]["served_entities"][0]["external_model"]["provider"]
    assert provider == provider.lower(), provider
