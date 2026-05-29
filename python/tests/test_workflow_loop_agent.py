"""Tests for the evolutionary workflow LoopAgent (workflow/loop_agent.py).

Covers the pure scoring functions first (composite_fitness, the local
statistical proxy, and the fitness-response merge), then the conversational
control surfaces (pause/resume/force_escalate and the status/summary getters).

These exercise only in-process logic — no Delta, no sub-agent dispatch — so the
fan-out paths (which require a live WorkspaceClient / Databricks Apps) are out
of scope here.
"""
from __future__ import annotations

import pytest

from apx_agent.workflow.loop_agent import (
    CipherType,
    GenerationResult,
    Hypothesis,
    LoopAgent,
    LoopConfig,
    _local_statistical_fitness,
)
from apx_agent.workflow.population_store import _esc_sql_str


# ---------------------------------------------------------------------------
# _esc_sql_str — SQL string-literal escaping (M21: must escape backslashes too)
# ---------------------------------------------------------------------------


class TestEscSqlStr:
    def test_doubles_single_quotes(self):
        assert _esc_sql_str("o'brien") == "o''brien"

    def test_escapes_trailing_backslash(self):
        # A bare backslash-doubling fix is the whole point of M21 — a trailing
        # backslash must not be able to escape the closing quote of the literal.
        assert _esc_sql_str("C:\\path\\") == "C:\\\\path\\\\"

    def test_backslash_before_quote_order(self):
        # Backslashes are escaped before quotes, matching engine_delta._esc.
        assert _esc_sql_str("a\\'b") == "a\\\\''b"

    def test_coerces_non_str(self):
        assert _esc_sql_str(123) == "123"


def _config(**overrides) -> LoopConfig:
    base = dict(
        population_table="cat.sch.pop",
        fitness_agents=["https://hist.databricksapps.com", "https://crit.databricksapps.com"],
        mutation_agent="https://mut.databricksapps.com",
        judge_agent="https://judge.databricksapps.com",
    )
    base.update(overrides)
    return LoopConfig(**base)


# ---------------------------------------------------------------------------
# composite_fitness — weights renormalized to exclude (unwired) perplexity
# ---------------------------------------------------------------------------


class TestCompositeFitness:
    def test_all_zero_is_zero(self):
        assert Hypothesis().composite_fitness() == 0.0

    def test_perplexity_does_not_contribute(self):
        # fitness_perplexity is excluded from the weighted sum until wired, so
        # varying it must not change the composite score.
        without = Hypothesis(fitness_statistical=0.5).composite_fitness()
        with_perp = Hypothesis(
            fitness_statistical=0.5, fitness_perplexity=1.0
        ).composite_fitness()
        assert without == with_perp

    def test_weights_sum_to_one_when_all_signals_max(self):
        # All four contributing signals at 1.0 must reach 1.0 (within rounding),
        # otherwise the 0.85 escalation gate would be unreachable.
        h = Hypothesis(
            fitness_statistical=1.0,
            fitness_semantic=1.0,
            fitness_consistency=1.0,
            fitness_adversarial=1.0,
        )
        assert h.composite_fitness() == pytest.approx(1.0, abs=1e-3)

    def test_escalation_threshold_is_reachable(self):
        # A strong-but-not-perfect candidate clears the default 0.85 gate — the
        # whole point of the H12 renormalization.
        h = Hypothesis(
            fitness_statistical=0.9,
            fitness_semantic=0.9,
            fitness_consistency=0.9,
            fitness_adversarial=0.9,
        )
        assert h.composite_fitness() >= 0.85

    def test_proportions_preserved(self):
        # semantic (weight 0.40) must outrank statistical (0.3333) for equal input.
        sem = Hypothesis(fitness_semantic=1.0).composite_fitness()
        stat = Hypothesis(fitness_statistical=1.0).composite_fitness()
        assert sem > stat

    def test_to_dict_uses_composite(self):
        h = Hypothesis(fitness_statistical=1.0, fitness_semantic=1.0,
                       fitness_consistency=1.0, fitness_adversarial=1.0)
        assert h.to_dict()["fitness_composite"] == pytest.approx(h.composite_fitness())


# ---------------------------------------------------------------------------
# _local_statistical_fitness
# ---------------------------------------------------------------------------


class TestLocalStatisticalFitness:
    def test_empty_map_is_zero(self):
        assert _local_statistical_fitness(Hypothesis(symbol_map={})) == 0.0

    def test_full_coverage_is_one(self):
        # Every mapped value is a high-frequency Latin letter → full coverage.
        common = list("etaoinshrdlu")
        h = Hypothesis(symbol_map={chr(ord("a") + i): common[i] for i in range(len(common))})
        assert _local_statistical_fitness(h) == 1.0

    def test_partial_coverage_is_fractional(self):
        h = Hypothesis(symbol_map={"x": "e", "y": "t", "z": "q"})
        score = _local_statistical_fitness(h)
        assert 0.0 < score < 1.0
        # 2 of the 12 common letters covered.
        assert score == pytest.approx(2 / 12)

    def test_clamped_to_one(self):
        # More than 12 covered values still clamps at 1.0.
        h = Hypothesis(symbol_map={str(i): "e" for i in range(50)})
        assert _local_statistical_fitness(h) == 1.0

    def test_non_string_values_ignored(self):
        h = Hypothesis(symbol_map={"x": 123})  # type: ignore[dict-item]
        assert _local_statistical_fitness(h) == 0.0


# ---------------------------------------------------------------------------
# _merge_fitness_response — agent output merged back onto the hypothesis
# ---------------------------------------------------------------------------


class TestMergeFitnessResponse:
    def test_merges_canonical_key(self):
        h = Hypothesis()
        LoopAgent._merge_fitness_response(h, "historian", {"fitness_semantic": 0.7})
        assert h.fitness_semantic == 0.7

    def test_merges_domain_named_key(self):
        h = Hypothesis()
        LoopAgent._merge_fitness_response(h, "critic", {"fitness_critic": 0.4})
        assert h.fitness_consistency == 0.4

    def test_adversarial_maps_to_adversarial_field(self):
        h = Hypothesis()
        LoopAgent._merge_fitness_response(h, "adversarial", {"fitness_adversarial": 0.9})
        assert h.fitness_adversarial == 0.9

    def test_unknown_agent_type_is_noop(self):
        h = Hypothesis(fitness_semantic=0.1)
        LoopAgent._merge_fitness_response(h, "mystery", {"fitness_semantic": 0.9})
        assert h.fitness_semantic == 0.1

    def test_non_numeric_value_is_ignored(self):
        h = Hypothesis()
        LoopAgent._merge_fitness_response(h, "historian", {"fitness_semantic": "n/a"})
        assert h.fitness_semantic == 0.0

    def test_coerces_numeric_string(self):
        h = Hypothesis()
        LoopAgent._merge_fitness_response(h, "historian", {"fitness_semantic": "0.5"})
        assert h.fitness_semantic == 0.5


# ---------------------------------------------------------------------------
# Control surfaces
# ---------------------------------------------------------------------------


def _agent() -> LoopAgent:
    return LoopAgent(config=_config())


class TestControlSurfaces:
    def test_pause_stops_running_flag(self):
        agent = _agent()
        agent._running = True
        agent._current_generation = 7
        out = agent.pause_loop()
        assert agent._running is False
        assert out == {"paused": True, "at_generation": 7}

    def test_resume_reports_resume_point(self):
        agent = _agent()
        agent._current_generation = 12
        out = agent.resume_loop()
        assert out["from_generation"] == 12

    def test_force_escalate_flags_matching_hypothesis(self):
        agent = _agent()
        target = Hypothesis(id="abc123")
        agent._results = [
            GenerationResult(
                generation=0, population_size=1, best_fitness=0.5,
                pareto_frontier_size=1, survivors=[target], escalated=[],
                wall_time_s=0.0,
            )
        ]
        out = agent.force_escalate("abc123")
        assert out == {"flagged": True, "hypothesis_id": "abc123"}
        assert target.flagged_for_review is True

    def test_force_escalate_unknown_id_returns_error(self):
        agent = _agent()
        out = agent.force_escalate("missing")
        assert "error" in out

    def test_get_status_aggregates_results(self):
        agent = _agent()
        agent._running = True
        agent._current_generation = 2
        agent._results = [
            GenerationResult(
                generation=0, population_size=3, best_fitness=0.6,
                pareto_frontier_size=2, survivors=[], escalated=[Hypothesis()],
                wall_time_s=1.0,
            ),
            GenerationResult(
                generation=1, population_size=3, best_fitness=0.9,
                pareto_frontier_size=2, survivors=[],
                escalated=[Hypothesis(), Hypothesis()], wall_time_s=1.0,
            ),
        ]
        status = agent.get_status()
        assert status["running"] is True
        assert status["current_generation"] == 2
        assert status["generations_completed"] == 2
        assert status["best_fitness"] == 0.9
        assert status["total_escalated"] == 3

    def test_get_status_empty(self):
        status = _agent().get_status()
        assert status["generations_completed"] == 0
        assert status["best_fitness"] == 0.0
        assert status["total_escalated"] == 0

    def test_get_best_hypothesis_empty(self):
        assert "error" in _agent().get_best_hypothesis()

    def test_get_best_hypothesis_returns_top_by_composite(self):
        agent = _agent()
        low = Hypothesis(id="low", fitness_semantic=0.1)
        high = Hypothesis(id="high", fitness_semantic=0.9)
        agent._results = [
            GenerationResult(
                generation=0, population_size=2, best_fitness=0.9,
                pareto_frontier_size=2, survivors=[low, high], escalated=[],
                wall_time_s=0.0,
            )
        ]
        best = agent.get_best_hypothesis()
        assert best["id"] == "high"

    def test_get_generation_summary_empty(self):
        assert "error" in _agent().get_generation_summary()

    def test_get_generation_summary_reports_fields(self):
        agent = _agent()
        agent._results = [
            GenerationResult(
                generation=4, population_size=10, best_fitness=0.75,
                pareto_frontier_size=5, survivors=[], escalated=[Hypothesis()],
                wall_time_s=3.14159, converged=True,
            )
        ]
        summary = agent.get_generation_summary()
        assert summary["generation"] == 4
        assert summary["best_fitness"] == 0.75
        assert summary["pareto_frontier_size"] == 5
        assert summary["escalated"] == 1
        assert summary["converged"] is True
        assert summary["wall_time_s"] == pytest.approx(3.14, abs=0.01)

    def test_tools_surface_includes_control_methods(self):
        names = {t.__name__ for t in _agent().tools()}
        assert {"pause_loop", "resume_loop", "force_escalate", "get_status"} <= names


# ---------------------------------------------------------------------------
# Sanity: the seed-fallback CipherType constants used by cli.py are valid
# ---------------------------------------------------------------------------


def test_cipher_type_constants_are_plain_strings():
    constants = [
        CipherType.SUBSTITUTION, CipherType.TRANSPOSITION,
        CipherType.POLYALPHABETIC, CipherType.NULL_BEARING,
        CipherType.COMPOSITE, CipherType.STEGANOGRAPHIC,
    ]
    assert all(isinstance(c, str) and c.isidentifier() for c in constants)
    # No dunder leakage (the L8 bug wrote "__module__" as a cipher_type).
    assert not any(c.startswith("__") for c in constants)
