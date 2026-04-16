"""
tests/test_loop_agent.py

Unit tests for:
  - Hypothesis dataclass (to_dict / from_dict round-trip)
  - Pareto selection (pure functions, no I/O)
  - PopulationStore SQL write path (mocked warehouse)
  - LoopAgent loop control tools (get_status, pause, etc.)

Run with:
    pytest tests/ -v
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Adjust path for local dev (not needed when installed as package)
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from apx_agent.workflow import (
    Hypothesis, LoopConfig, GenerationResult,
    pareto_frontier, pareto_dominates,
    CipherType, SourceLanguage,
)


# ---------------------------------------------------------------------------
# Hypothesis
# ---------------------------------------------------------------------------

class TestHypothesis:
    def _make(self, **kwargs) -> Hypothesis:
        h = Hypothesis(
            id="abc123",
            generation=0,
            cipher_type=CipherType.SUBSTITUTION,
            source_language=SourceLanguage.LATIN,
            symbol_map={"o": "a", "a": "e", "i": "i"},
            null_chars=["q"],
            transformation_rules=[],
        )
        for k, v in kwargs.items():
            setattr(h, k, v)
        return h

    def test_to_dict_roundtrip(self):
        h = self._make()
        d = h.to_dict()

        assert d["id"] == "abc123"
        assert isinstance(d["symbol_map"], str)   # JSON-encoded
        assert isinstance(d["null_chars"], str)
        assert d["cipher_type"] == CipherType.SUBSTITUTION
        assert d["source_language"] == SourceLanguage.LATIN

        h2 = Hypothesis.from_dict(d)
        assert h2.id == h.id
        assert h2.symbol_map == h.symbol_map
        assert h2.null_chars == h.null_chars

    def test_composite_fitness(self):
        h = self._make(
            fitness_statistical=0.8,
            fitness_perplexity=0.7,
            fitness_semantic=0.9,
            fitness_consistency=0.6,
            fitness_adversarial=0.5,
        )
        expected = (
            0.25 * 0.8
            + 0.25 * 0.7
            + 0.30 * 0.9
            + 0.15 * 0.6
            + 0.05 * 0.5
        )
        assert abs(h.composite_fitness() - expected) < 1e-6

    def test_zero_fitness_by_default(self):
        h = Hypothesis()
        assert h.composite_fitness() == 0.0

    def test_from_dict_handles_json_strings(self):
        d = {
            "id": "xyz",
            "generation": 1,
            "parent_id": None,
            "cipher_type": "substitution",
            "source_language": "latin",
            "symbol_map": json.dumps({"o": "a"}),  # pre-serialized
            "null_chars": json.dumps(["q"]),
            "transformation_rules": json.dumps([]),
        }
        h = Hypothesis.from_dict(d)
        assert h.symbol_map == {"o": "a"}
        assert h.null_chars == ["q"]


# ---------------------------------------------------------------------------
# Pareto selection
# ---------------------------------------------------------------------------

class TestPareto:
    def _h(self, **fitness_kwargs) -> Hypothesis:
        h = Hypothesis(id=str(uuid.uuid4())[:6])
        for k, v in fitness_kwargs.items():
            setattr(h, k, v)
        return h

    OBJS = ["fitness_statistical", "fitness_semantic", "fitness_consistency"]

    def test_dominates_clear_case(self):
        a = self._h(fitness_statistical=0.9, fitness_semantic=0.8, fitness_consistency=0.7)
        b = self._h(fitness_statistical=0.5, fitness_semantic=0.5, fitness_consistency=0.5)
        assert pareto_dominates(a, b, self.OBJS)
        assert not pareto_dominates(b, a, self.OBJS)

    def test_dominates_equal_on_some(self):
        # a ≥ b on all and > b on at least one
        a = self._h(fitness_statistical=0.9, fitness_semantic=0.5, fitness_consistency=0.5)
        b = self._h(fitness_statistical=0.5, fitness_semantic=0.5, fitness_consistency=0.5)
        assert pareto_dominates(a, b, self.OBJS)

    def test_no_dominance_tradeoff(self):
        # a better on stat, b better on semantic
        a = self._h(fitness_statistical=0.9, fitness_semantic=0.3, fitness_consistency=0.5)
        b = self._h(fitness_statistical=0.3, fitness_semantic=0.9, fitness_consistency=0.5)
        assert not pareto_dominates(a, b, self.OBJS)
        assert not pareto_dominates(b, a, self.OBJS)

    def test_frontier_extracts_nondominated(self):
        dominated = self._h(fitness_statistical=0.3, fitness_semantic=0.3, fitness_consistency=0.3)
        front_a   = self._h(fitness_statistical=0.9, fitness_semantic=0.3, fitness_consistency=0.5)
        front_b   = self._h(fitness_statistical=0.3, fitness_semantic=0.9, fitness_consistency=0.5)

        population = [dominated, front_a, front_b]
        frontier   = pareto_frontier(population, self.OBJS)

        assert dominated not in frontier
        assert front_a in frontier
        assert front_b in frontier
        assert len(frontier) == 2

    def test_frontier_single_element(self):
        h = self._h(fitness_statistical=0.5, fitness_semantic=0.5, fitness_consistency=0.5)
        assert pareto_frontier([h], self.OBJS) == [h]

    def test_frontier_all_equal(self):
        # All equal → none dominates any other → all on frontier
        pop = [
            self._h(fitness_statistical=0.5, fitness_semantic=0.5, fitness_consistency=0.5)
            for _ in range(5)
        ]
        assert len(pareto_frontier(pop, self.OBJS)) == 5


# ---------------------------------------------------------------------------
# PopulationStore (mocked SQL)
# ---------------------------------------------------------------------------

class TestPopulationStoreSqlFallback:
    def _make_store(self):
        from apx_agent.workflow.population_store import PopulationStore
        config = LoopConfig(
            population_table  = "voynich.evolution.population",
            fitness_agents    = [],
            mutation_agent    = "",
            judge_agent       = "",
            warehouse_id      = "test-warehouse-id",
        )
        ws     = MagicMock()
        store  = PopulationStore(ws, config)
        store._spark = None  # force SQL fallback
        return store, ws

    def _mock_sql_resp(self, ws, rows: list[dict]):
        from databricks.sdk.service.sql import StatementState
        resp = MagicMock()
        resp.status.state = StatementState.SUCCEEDED
        if rows:
            cols = list(rows[0].keys())
            # Use real SimpleNamespace so .name returns an actual string
            from types import SimpleNamespace
            resp.manifest.schema.columns = [SimpleNamespace(name=c) for c in cols]
            resp.result.data_array = [[r[c] for c in cols] for r in rows]
        else:
            resp.result = None
        ws.statement_execution.execute_statement.return_value = resp
        return resp

    def test_write_hypotheses_sql_chunked(self):
        store, ws = self._make_store()
        self._mock_sql_resp(ws, [])

        hypotheses = [
            Hypothesis(
                id=f"h{i:03d}",
                generation=1,
                cipher_type=CipherType.SUBSTITUTION,
                source_language=SourceLanguage.LATIN,
                symbol_map={"o": "a"},
                null_chars=[],
            )
            for i in range(30)  # >chunk_size to test chunking
        ]

        store.write_hypotheses_sql(hypotheses, chunk_size=10)
        # 30 hypotheses in chunks of 10 → 3 SQL calls
        assert ws.statement_execution.execute_statement.call_count == 3

    def test_load_pareto_survivors_parses_rows(self):
        store, ws = self._make_store()
        self._mock_sql_resp(ws, [
            {
                "id": "abc", "generation": 1, "parent_id": None,
                "cipher_type": "substitution", "source_language": "latin",
                "symbol_map": '{"o":"a"}', "null_chars": "[]",
                "transformation_rules": "[]",
                "fitness_statistical": 0.8, "fitness_perplexity": 0.7,
                "fitness_semantic": 0.9, "fitness_consistency": 0.6,
                "fitness_adversarial": 0.0, "fitness_composite": 0.775,
                "agent_eval_historian": 0.8, "agent_eval_critic": 0.7,
                "decoded_sample": "test text", "mlflow_run_id": "",
                "flagged_for_review": False,
            }
        ])
        results = store.load_pareto_survivors(generation=1, top_n=5)
        assert len(results) == 1
        assert results[0].id == "abc"
        assert results[0].symbol_map == {"o": "a"}

    def test_build_insert_escapes_quotes(self):
        store, _ = self._make_store()
        h = Hypothesis(
            id="esc01",
            generation=0,
            symbol_map={"o": "it's"},   # apostrophe in value
            decoded_sample="it's a test",
        )
        sql = store._build_insert([h])
        # Should not raise and should double-escape apostrophes
        assert "it''s" in sql or "it's" not in sql.split("VALUES")[1].count("'") % 2 == 0


# ---------------------------------------------------------------------------
# LoopAgent control tools
# ---------------------------------------------------------------------------

class TestLoopAgentTools:
    def _make_agent(self) -> "LoopAgent":
        from apx_agent.workflow import LoopAgent
        config = LoopConfig(
            population_table="voynich.evolution.population",
            fitness_agents=[],
            mutation_agent="",
            judge_agent="",
        )
        return LoopAgent(config=config)

    def test_get_status_initial(self):
        agent = self._make_agent()
        status = agent.get_status()
        assert status["running"] == False
        assert status["current_generation"] == 0
        assert status["generations_completed"] == 0

    def test_pause_loop(self):
        agent = self._make_agent()
        agent._running = True
        result = agent.pause_loop()
        assert result["paused"] == True
        assert agent._running == False

    def test_get_best_hypothesis_no_results(self):
        agent = self._make_agent()
        result = agent.get_best_hypothesis()
        assert "error" in result

    def test_get_best_hypothesis_with_results(self):
        from apx_agent.workflow.loop_agent import GenerationResult
        agent = self._make_agent()
        h = Hypothesis(
            id="best01", generation=1,
            fitness_statistical=0.9, fitness_semantic=0.9,
            fitness_perplexity=0.8, fitness_consistency=0.7,
        )
        agent._results = [
            GenerationResult(
                generation=1, population_size=1,
                best_fitness=h.composite_fitness(),
                pareto_frontier_size=1,
                survivors=[h], escalated=[],
                wall_time_s=1.5,
            )
        ]
        result = agent.get_best_hypothesis(generation=0)
        assert result["id"] == "best01"

    def test_force_escalate_found(self):
        from apx_agent.workflow.loop_agent import GenerationResult
        agent = self._make_agent()
        h = Hypothesis(id="esc01", generation=0)
        agent._results = [
            GenerationResult(
                generation=0, population_size=1,
                best_fitness=0.5, pareto_frontier_size=1,
                survivors=[h], escalated=[], wall_time_s=1.0,
            )
        ]
        result = agent.force_escalate("esc01")
        assert result["flagged"] == True
        assert h.flagged_for_review == True

    def test_force_escalate_not_found(self):
        agent = self._make_agent()
        result = agent.force_escalate("doesnotexist")
        assert "error" in result
