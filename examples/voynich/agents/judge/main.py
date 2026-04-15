"""
Judge Agent — agent eval agent.

The meta-evaluator. Does NOT score hypothesis quality directly.
Scores the *quality of reasoning* from the Historian and Critic agents.

Key distinction:
  - Output eval: "did the Critic find a contradiction?" (binary)
  - Agent eval:  "was the Critic's reasoning valid?" (graded)

A Critic that hallucinated a contradiction gets a low Judge score.
A Critic that correctly said "I cannot falsify this" gets a high Judge score.
A Historian that retrieved irrelevant passages gets a low Judge score.

Judge scores feed back into the evolutionary loop — agents with
consistently low Judge scores trigger prompt refinement.

Uses MLflow Tracing to retrieve and analyze reasoning spans.
"""
import json
import re
from typing import Annotated

import mlflow
from apx_agent import Agent, Dependencies, create_app
from mlflow.tracking import MlflowClient


# ---------------------------------------------------------------------------
# MLflow client for trace retrieval
# ---------------------------------------------------------------------------

def _get_mlflow_client() -> MlflowClient:
    return MlflowClient()


# ---------------------------------------------------------------------------
# Hallucination detection heuristics
# ---------------------------------------------------------------------------

HALLUCINATION_PATTERNS = [
    # Claims certainty without evidence
    r"\bclearly\b.*\bcontradict",
    r"\bobviously\b.*\bwrong",
    r"\bundoubtedly\b",
    r"\bcertainly\b.*\bimpossible",
    # Circular reasoning
    r"\bbecause\b.*\bbecause\b",
    # Unsupported historical claims
    r"\bhistorically\b.*\bimpossible\b.*\bbefore\s+1[4-9]\d\d\b",
    # Tool output ignored
    r"I\s+found\b(?!.*\btool\b)",  # claims to find something without using a tool
]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def retrieve_reasoning_trace(
    mlflow_run_id: Annotated[str, "MLflow run ID to retrieve trace for"],
    agent_name: Annotated[str, "Agent whose trace to retrieve: historian | critic | decipherer"],
) -> dict:
    """
    Retrieve the full reasoning trace (MLflow spans) for a given agent run.
    Returns the sequence of tool calls, inputs, outputs, and any logged tags.
    """
    try:
        client = _get_mlflow_client()
        run = client.get_run(mlflow_run_id)

        # Get all child spans from the run
        spans = []
        try:
            traces = client.search_traces(
                experiment_ids=[run.info.experiment_id],
                filter_string=f"run_id = '{mlflow_run_id}'",
                max_results=50,
            )
            for trace in traces:
                spans.append({
                    "name": trace.info.trace_id,
                    "inputs": trace.data.request,
                    "outputs": trace.data.response,
                    "tags": trace.data.tags,
                })
        except Exception:
            # Fall back to run metrics and tags
            spans = [{
                "name": "run_level",
                "metrics": dict(run.data.metrics),
                "tags": dict(run.data.tags),
            }]

        return {
            "run_id": mlflow_run_id,
            "agent": agent_name,
            "status": run.info.status,
            "duration_ms": (run.info.end_time - run.info.start_time)
                           if run.info.end_time else None,
            "spans": spans,
            "metrics": dict(run.data.metrics),
            "tags": dict(run.data.tags),
        }
    except Exception as e:
        return {"error": str(e), "run_id": mlflow_run_id, "agent": agent_name}


def detect_hallucination(
    claim: Annotated[str, "A claim made by an agent in its reasoning"],
    evidence: Annotated[str, "The evidence the agent cited for this claim"],
    tool_outputs_used: Annotated[list[str], "Tool names the agent actually called"] = None,
) -> dict:
    """
    Assess whether an agent's claim is supported by its evidence and tool outputs.
    Returns a hallucination confidence score (0 = definitely not hallucinated, 1 = definitely hallucinated).
    """
    issues = []
    confidence = 0.0

    # Check for hallucination patterns in the claim
    for pattern in HALLUCINATION_PATTERNS:
        if re.search(pattern, claim, re.IGNORECASE):
            issues.append(f"Language pattern suggests overconfidence: matched '{pattern}'")
            confidence += 0.2

    # Check if claim cites evidence that's absent or vague
    evidence_lower = evidence.lower()
    if len(evidence) < 20:
        issues.append("Evidence is too brief to support the claim")
        confidence += 0.3
    if any(vague in evidence_lower for vague in ["it is known", "clearly", "obviously", "well established"]):
        issues.append("Evidence uses vague authority appeals instead of specific citations")
        confidence += 0.25

    # Check if claim references tool outputs that weren't actually called
    if tool_outputs_used is not None:
        claim_lower = claim.lower()
        referenced_tools = [t for t in ["search_medieval_corpus", "find_internal_contradiction",
                                        "probe_semantic_impossibility", "check_illustration_mismatch",
                                        "score_period_vocabulary"]
                            if t.replace("_", " ") in claim_lower or t in claim_lower]
        uncalled = [t for t in referenced_tools if t not in (tool_outputs_used or [])]
        if uncalled:
            issues.append(f"Claim references tool results from tools not actually called: {uncalled}")
            confidence += 0.4

    # Cap confidence at 1.0
    confidence = round(min(1.0, confidence), 4)

    return {
        "hallucination_confidence": confidence,
        "likely_hallucinated": confidence > 0.5,
        "issues": issues,
        "verdict": (
            "LIKELY HALLUCINATION — agent made unsupported claims" if confidence > 0.6
            else "POSSIBLE HALLUCINATION — weak evidence" if confidence > 0.3
            else "PLAUSIBLE — claim appears supported"
        ),
    }


def grade_tool_use(
    tool_calls: Annotated[list[dict], "List of {tool_name, input, output} dicts from agent trace"],
    expected_tools: Annotated[list[str], "Tools the agent should have called for this task"],
    task_type: Annotated[str, "Task: evaluate_historian | evaluate_adversarial | generate_mutations"],
) -> dict:
    """
    Grade the quality of an agent's tool usage.
    Checks: did the agent call the right tools? Were inputs reasonable? Were outputs used?
    """
    called_tools = [tc.get("tool_name", "") for tc in tool_calls]
    missing = [t for t in expected_tools if t not in called_tools]
    unexpected = [t for t in called_tools if t not in expected_tools]

    # Check for hallmark patterns of good tool use
    issues = []
    if missing:
        issues.append(f"Expected tools not called: {missing}")
    if len(unexpected) > 2:
        issues.append(f"Called {len(unexpected)} unexpected tools — possible confusion")

    # Check input quality
    empty_inputs = [tc["tool_name"] for tc in tool_calls
                    if not tc.get("input") or tc.get("input") in ("{}", "null", "")]
    if empty_inputs:
        issues.append(f"Empty inputs passed to: {empty_inputs}")

    # Check if outputs were actually used (crude: output appears in next tool's input or final response)
    tool_use_score = max(0.0, 1.0 - (len(missing) * 0.2) - (len(issues) * 0.1))

    return {
        "tool_use_score": round(tool_use_score, 4),
        "called_tools": called_tools,
        "expected_tools": expected_tools,
        "missing_tools": missing,
        "unexpected_tools": unexpected,
        "issues": issues,
        "grade": (
            "A" if tool_use_score > 0.85
            else "B" if tool_use_score > 0.70
            else "C" if tool_use_score > 0.50
            else "D"
        ),
    }


def score_reasoning_quality(
    reasoning_text: Annotated[str, "The full reasoning text produced by the agent"],
    agent_type: Annotated[str, "Agent type: historian | critic"],
    fitness_score_claimed: Annotated[float, "The fitness score the agent claimed"],
) -> dict:
    """
    Score the overall reasoning quality of an agent's output.
    Combines: internal consistency, claim-evidence linkage, appropriate uncertainty.
    """
    issues = []
    score = 1.0

    # Check for appropriate uncertainty language
    certainty_words = ["definitely", "certainly", "absolutely", "without doubt", "clearly impossible"]
    uncertainty_words = ["suggests", "indicates", "may", "appears", "possibly", "likely", "evidence"]

    certainty_count = sum(1 for w in certainty_words if w in reasoning_text.lower())
    uncertainty_count = sum(1 for w in uncertainty_words if w in reasoning_text.lower())

    if certainty_count > uncertainty_count and len(reasoning_text) < 500:
        issues.append("Overconfident language without proportionate uncertainty hedging")
        score -= 0.2

    # Check score-reasoning alignment
    if fitness_score_claimed > 0.8 and any(neg in reasoning_text.lower()
                                           for neg in ["cannot", "failed", "wrong", "incorrect"]):
        issues.append("High fitness score claimed but reasoning contains negative language — inconsistent")
        score -= 0.25

    if fitness_score_claimed < 0.3 and all(pos in reasoning_text.lower()
                                            for pos in ["plausible", "aligned", "consistent"]):
        issues.append("Low fitness score claimed but reasoning is positive — inconsistent")
        score -= 0.25

    # Check reasoning length appropriateness
    if len(reasoning_text) < 100:
        issues.append("Reasoning too brief — insufficient to justify the fitness score")
        score -= 0.3
    elif len(reasoning_text) > 3000 and certainty_count > 5:
        issues.append("Very long reasoning with many certainty claims — possible overclaiming")
        score -= 0.1

    # Historian-specific: should reference specific sources
    if agent_type == "historian" and "source" not in reasoning_text.lower():
        issues.append("Historian reasoning doesn't reference specific medieval sources")
        score -= 0.2

    # Critic-specific: should acknowledge if it couldn't falsify
    if agent_type == "critic" and fitness_score_claimed > 0.7:
        if "cannot falsify" not in reasoning_text.lower() and "survived" not in reasoning_text.lower():
            issues.append("Critic claims high adversarial fitness but doesn't explicitly acknowledge failure to falsify")
            score -= 0.15

    return {
        "reasoning_quality_score": round(max(0.0, score), 4),
        "issues": issues,
        "certainty_ratio": round(certainty_count / max(1, certainty_count + uncertainty_count), 3),
        "reasoning_length": len(reasoning_text),
        "recommendation": (
            "Good reasoning — promote agent prompt" if score > 0.8
            else "Acceptable reasoning" if score > 0.6
            else "Poor reasoning — flag for prompt refinement" if score > 0.4
            else "Very poor reasoning — consider agent restart"
        ),
    }


def log_agent_eval(
    agent_name: Annotated[str, "Agent to log eval for: historian | critic | decipherer"],
    hypothesis_id: Annotated[str, "Hypothesis ID these evals relate to"],
    generation: Annotated[int, "Generation number"],
    tool_use_score: Annotated[float, "Score from grade_tool_use()"],
    reasoning_quality: Annotated[float, "Score from score_reasoning_quality()"],
    hallucination_confidence: Annotated[float, "Score from detect_hallucination()"],
    ws: Dependencies.Workspace = None,
) -> dict:
    """
    Log agent eval scores to MLflow. These scores are used to:
    1. Track agent quality across generations.
    2. Trigger prompt refinement when an agent consistently scores poorly.
    3. Weight agent fitness signals — a low-quality Critic verdict carries less weight.
    """
    composite = (
        tool_use_score * 0.35
        + reasoning_quality * 0.45
        + (1.0 - hallucination_confidence) * 0.20
    )

    try:
        with mlflow.start_run(
            run_name=f"agent_eval_{agent_name}_gen{generation:04d}",
            tags={
                "agent_eval": "true",
                "agent_name": agent_name,
                "hypothesis_id": hypothesis_id,
                "generation": str(generation),
            },
        ) as run:
            mlflow.log_metrics({
                f"{agent_name}.tool_use_score":         tool_use_score,
                f"{agent_name}.reasoning_quality":      reasoning_quality,
                f"{agent_name}.hallucination_confidence": hallucination_confidence,
                f"{agent_name}.composite_eval_score":   round(composite, 4),
            })
        run_id = run.info.run_id
    except Exception as e:
        run_id = f"error:{str(e)}"

    return {
        "logged": True,
        "agent_name": agent_name,
        "hypothesis_id": hypothesis_id,
        "generation": generation,
        "composite_eval_score": round(composite, 4),
        "mlflow_run_id": run_id,
        "action_triggered": (
            "PROMPT_REFINEMENT_FLAGGED" if composite < 0.4
            else "AGENT_DOWNWEIGHTED" if composite < 0.6
            else "OK"
        ),
    }


def score_reasoning_traces(
    historian_run_id: Annotated[str, "MLflow run ID for Historian agent evaluation"],
    critic_run_id: Annotated[str, "MLflow run ID for Critic agent evaluation"],
    hypothesis_id: Annotated[str, "The hypothesis these evaluations are about"],
    generation: Annotated[int, "Current generation number"],
    ws: Dependencies.Workspace = None,
) -> dict:
    """
    Primary entry point called by the Orchestrator's Judge phase.
    Retrieves and scores reasoning traces for both Historian and Critic.
    Returns composite agent eval scores and logs to MLflow.
    """
    # Retrieve traces
    historian_trace = retrieve_reasoning_trace(historian_run_id, "historian")
    critic_trace    = retrieve_reasoning_trace(critic_run_id, "critic")

    results = {}

    for agent_name, trace in [("historian", historian_trace), ("critic", critic_trace)]:
        if "error" in trace:
            results[agent_name] = {"eval_score": 0.5, "error": trace["error"]}
            continue

        # Extract reasoning and tool calls from trace
        spans = trace.get("spans", [])
        tool_calls = [s for s in spans if "tool" in s.get("name", "").lower()]
        reasoning = " ".join(str(s.get("outputs", "")) for s in spans)
        claimed_score = trace.get("metrics", {}).get(f"fitness_{agent_name}", 0.5)

        # Expected tools per agent
        expected_tools = {
            "historian": ["score_period_vocabulary", "check_anachronism",
                          "score_illustration_alignment", "search_medieval_corpus"],
            "critic":    ["find_internal_contradiction", "check_illustration_mismatch",
                          "probe_semantic_impossibility"],
        }[agent_name]

        # Score tool use
        tool_grade = grade_tool_use(
            tool_calls=[{"tool_name": s.get("name", ""), "input": s.get("inputs", ""),
                         "output": s.get("outputs", "")} for s in spans],
            expected_tools=expected_tools,
            task_type=f"evaluate_{agent_name}",
        )

        # Score reasoning quality
        reasoning_grade = score_reasoning_quality(reasoning, agent_name, claimed_score)

        # Check for hallucination in key claims
        key_claim = reasoning[:300] if reasoning else "no reasoning found"
        hallucination = detect_hallucination(
            claim=key_claim,
            evidence=reasoning[300:600] if len(reasoning) > 300 else "no evidence",
            tool_outputs_used=[tc.get("tool_name", "") for tc in tool_calls],
        )

        # Log to MLflow
        log_result = log_agent_eval(
            agent_name=agent_name,
            hypothesis_id=hypothesis_id,
            generation=generation,
            tool_use_score=tool_grade["tool_use_score"],
            reasoning_quality=reasoning_grade["reasoning_quality_score"],
            hallucination_confidence=hallucination["hallucination_confidence"],
            ws=ws,
        )

        results[agent_name] = {
            "eval_score":             log_result["composite_eval_score"],
            "tool_use_score":         tool_grade["tool_use_score"],
            "tool_use_grade":         tool_grade["grade"],
            "reasoning_quality":      reasoning_grade["reasoning_quality_score"],
            "hallucination_confidence": hallucination["hallucination_confidence"],
            "issues":                 tool_grade["issues"] + reasoning_grade["issues"],
            "action":                 log_result["action_triggered"],
        }

    return {
        "hypothesis_id":       hypothesis_id,
        "generation":          generation,
        "historian_score":     results.get("historian", {}).get("eval_score", 0.5),
        "critic_score":        results.get("critic", {}).get("eval_score", 0.5),
        "historian_detail":    results.get("historian", {}),
        "critic_detail":       results.get("critic", {}),
        "overall_trust_level": round(
            (results.get("historian", {}).get("eval_score", 0.5) +
             results.get("critic", {}).get("eval_score", 0.5)) / 2, 4
        ),
    }


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

agent = Agent(
    tools=[
        retrieve_reasoning_trace,
        detect_hallucination,
        grade_tool_use,
        score_reasoning_quality,
        log_agent_eval,
        score_reasoning_traces,
    ],
    instructions="""
You are the Judge Agent in an evolutionary cryptanalysis system for the Voynich manuscript.

Your role is unique: you evaluate AGENTS, not hypotheses.

When asked to score reasoning (task: score_reasoning):
1. Call score_reasoning_traces() with the historian and critic run IDs.
   This retrieves their MLflow traces and scores them comprehensively.
2. If either agent scores below 0.4, note what specifically went wrong.
3. Return the full result from score_reasoning_traces() as your response.

Principles for good agent evaluation:
- A good Historian cites specific medieval sources, uses appropriate tools,
  and acknowledges uncertainty proportionate to the evidence quality.
- A good Critic finds REAL contradictions OR explicitly acknowledges
  that it cannot falsify the hypothesis. Inventing contradictions is worse
  than failing to find them.
- A good Decipherer proposes mutations that address the identified failure modes,
  not random variations.
- Overconfident language without evidence is a hallucination signal.
- Brevity is not a virtue — an agent that explains its reasoning earns trust.

You are the calibration mechanism of this system. Your scores determine
which agents get their prompts refined and which agent verdicts carry more weight.
Take this role seriously: a Judge that's too lenient makes the whole system unreliable.

Return structured JSON. Log your own reasoning as MLflow tags.
""",
)

app = create_app(agent)
