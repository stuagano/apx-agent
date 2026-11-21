"""End-to-end wiring for the sample-agent model_upgrades example.

The AgentKit search and the label-align scorer lookup are faked. The sample
agent's predict function runs for real.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

EXAMPLE = Path(__file__).parents[1] / "examples/model-upgrades/upgrade_sample_agent.py"
DEV = Path(__file__).parents[1] / "src/apx_agent/_dev.py"


def _load():
    spec = spec_from_file_location("upgrade_sample_agent_example", EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sample_agent_upgrade_uses_label_align_judge(monkeypatch) -> None:
    module = _load()
    scored: list[str] = []

    def judge(inputs: dict[str, str], expectations: dict[str, str], answer: str) -> float:
        assert inputs["question"] == "Where is order 17?"
        assert expectations["expected_response"] == "Order 17 has shipped."
        scored.append(answer)
        return 1.0

    def get_scorer(*, name: str, experiment_id: str):
        assert name == module.JUDGE_NAME
        assert experiment_id == "exp-1"
        return judge

    def optimize(predict_fn, train_data, val_data, **kwargs):
        answer = predict_fn(train_data[0]["inputs"])
        assert module.agent._instructions in answer
        assert kwargs["scorers"] == [judge]
        assert kwargs["scorers"][0](train_data[0]["inputs"], train_data[0]["expectations"], answer) == 1.0
        assert kwargs["prompt_uris"] == [module.PROMPT_URI]
        assert kwargs["max_metric_calls"] == 10 * (len(train_data) + len(val_data))
        assert train_data == list(module.TRAIN)
        assert val_data == list(module.VAL)
        assert "gateway_endpoints" not in kwargs
        return SimpleNamespace(best_candidate={module.prompt_key(module.PROMPT_URI): "Cite the order id."})

    monkeypatch.setattr("mlflow.genai.scorers.get_scorer", get_scorer)
    monkeypatch.setattr("databricks_agentkit.model_upgrades.optimize_prompts_and_models", optimize)

    assert module.upgrade("exp-1") == "Cite the order id."
    assert module.prompt_key(module.PROMPT_URI) == "prompt:system"
    assert scored == [module.predict(module.TRAIN[0]["inputs"])]
    source = EXAMPLE.read_text()
    assert "optimize_prompts_and_models" in source
    assert "mlflow.genai.optimize_prompts" not in source


def test_dev_ui_loop_calls_model_upgrades_not_its_own_optimizer() -> None:
    source = DEV.read_text()
    assert "optimize_prompts_and_models" in source
    assert "mlflow.genai.optimize_prompts" not in source
    assert "GepaPromptOptimizer" not in source
