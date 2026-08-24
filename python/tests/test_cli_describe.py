"""Safe Service Policy metadata in agents describe."""

import json
from pathlib import Path

from click.testing import CliRunner

from apx_agent.cli import main


def test_describe_service_policies_omits_prompt_and_secret_values(tmp_path: Path) -> None:
    spec = tmp_path / "agent.yaml"
    spec.write_text(
        """name: describe-agent
service_policies:
  attachments:
    - name: judge-attachment
      target_type: mcp_service
      target: main.tools.github
      policies:
        - name: judge
          kind: llm_judge
          classifier: judge-model
          prompt: do not print this classifier prompt
          phase: on_call
          rank: 100
"""
    )

    result = CliRunner().invoke(main, ["agents", "describe", str(spec), "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    policy = payload["service_policies"]["attachments"][0]["policies"][0]
    assert policy["name"] == "judge"
    assert policy["classifier"] == "judge-model"
    assert "prompt" not in policy
    assert "do not print this classifier prompt" not in result.output
