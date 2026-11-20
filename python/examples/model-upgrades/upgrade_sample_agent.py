"""Upgrade a sample agent's prompt with AgentKit ``model_upgrades``.

``label align --new-version domain_quality-v2`` registers the judge this calls
as a scorer. The search itself is ``optimize_prompts_and_models``. This module
does not submit a workspace job and does not call a model; pass a test double
by patching that function, or run ``upgrade`` only when you intend the search
to execute.

Prompt-only is a supported ``model_upgrades`` mode (no ``gateway_endpoints``).
Add ``gateway_endpoints`` to the call when the agent invokes AI Gateway model
services you want tuned with the prompt.
"""

from __future__ import annotations

from apx_agent import Agent

# The version `label align --new-version` registers. Not the unaligned base judge.
JUDGE_NAME = "domain_quality-v2"
PROMPT_URI = "prompts:/main.sample_agent.system@production"

agent = Agent(
    name="sample",
    instructions="Answer the question in one sentence. Cite only the given facts.",
)

TRAIN = (
    {
        "inputs": {"question": "Where is order 17?"},
        "expectations": {"expected_response": "Order 17 has shipped."},
    },
)
VAL = (
    {
        "inputs": {"question": "Can I cancel order 18?"},
        "expectations": {"expected_response": "Order 18 can be cancelled before it ships."},
    },
)


def prompt_key(uri: str) -> str:
    """``best_candidate`` key for a ``prompts:/`` URI.

    ``model_upgrades`` keys the winning template as ``prompt:<short name>``,
    where the short name is the last segment of the UC prompt name.
    """
    if not uri.startswith("prompts:/"):
        raise ValueError(f"Invalid prompt URI {uri!r}")
    rest = uri[len("prompts:/") :]
    if "@" in rest:
        name = rest.rsplit("@", 1)[0]
    elif "/" in rest:
        name = rest.rsplit("/", 1)[0]
    else:
        name = rest
    return "prompt:" + name.rsplit(".", 1)[-1]


def predict(inputs: dict[str, str]) -> str:
    """One eval record. The instructions under test are the sample agent's."""
    question = inputs["question"]
    return f"{agent._instructions}\n\nQuestion: {question}"


def aligned_judge(experiment_id: str, judge_name: str = JUDGE_NAME):
    """The scorer ``label align`` registered for this experiment."""
    from mlflow.genai.scorers import get_scorer

    return get_scorer(name=judge_name, experiment_id=experiment_id)


def upgrade(experiment_id: str, judge_name: str = JUDGE_NAME) -> str:
    """Run ``model_upgrades`` on the sample agent and return the winning prompt."""
    from databricks_agentkit.model_upgrades import optimize_prompts_and_models

    judge = aligned_judge(experiment_id, judge_name)
    train = list(TRAIN)
    val = list(VAL)
    result = optimize_prompts_and_models(
        predict,
        train,
        val,
        prompt_uris=[PROMPT_URI],
        scorers=[judge],
        # Same budget default as `agentbricks experimental models upgrade`.
        max_metric_calls=10 * (len(train) + len(val)),
        display_progress_bar=False,
    )
    text = result.best_candidate[prompt_key(PROMPT_URI)]
    if not isinstance(text, str):
        raise TypeError("model_upgrades prompt candidate must be text")
    return text
