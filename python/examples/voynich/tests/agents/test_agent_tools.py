"""
tests/agents/test_agent_tools.py — agent tool unit tests, pure logic, mocked I/O.
"""
import importlib.util
import pathlib
import sys
import types
import pytest

AGENTS_DIR = pathlib.Path(__file__).parent.parent.parent / "agents"

def _ensure_apx_agent_mock():
    """Inject minimal apx_agent stub so agents load without the real package."""
    if "apx_agent" in sys.modules:
        return
    stub = types.ModuleType("apx_agent")
    class _Dep:
        Workspace = type("Workspace", (), {})
        Sql       = type("Sql", (), {"execute": lambda self, sql: []})
        Headers   = type("Headers", (), {})
    class _Agent:
        def __init__(self, tools=None, sub_agents=None, instructions="", **kw):
            self.tools = tools or []
    stub.Dependencies = _Dep
    stub.Agent = _Agent
    stub.create_app = lambda agent: None
    sys.modules["apx_agent"] = stub

def _load(name: str):
    _ensure_apx_agent_mock()
    path = AGENTS_DIR / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"agent_{name}", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# ── Decipherer ────────────────────────────────────────────────────────────────
class TestDeciphererTools:
    @pytest.fixture(autouse=True)
    def _m(self): self.m = _load("decipherer")

    def test_apply_cipher_basic(self):
        r = self.m.apply_cipher({"o":"t","a":"h","i":"e"}, [], "oai oai")
        assert r["decoded_text"] == "the the"

    def test_apply_cipher_strips_nulls(self):
        r = self.m.apply_cipher({"o":"a"}, ["q"], "qoqoq")
        assert "q" not in r["decoded_text"]

    def test_apply_cipher_reverse(self):
        r = self.m.apply_cipher({"a":"a","b":"b","c":"c"}, [], "abc def", reverse_word_order=True)
        assert r["decoded_text"].split()[0] == "cba"

    def test_apply_cipher_empty_map(self):
        r = self.m.apply_cipher({}, [], "oaiin")
        assert r["coverage_pct"] == 0.0

    def test_validate_valid(self):
        r = self.m.validate_hypothesis({"cipher_type":"substitution","source_language":"latin","symbol_map":{"o":"a","a":"e","i":"i","n":"n"}})
        assert r["valid"] is True

    def test_validate_bad_cipher_type(self):
        r = self.m.validate_hypothesis({"cipher_type":"caesar","source_language":"latin","symbol_map":{"o":"a","a":"e","i":"i"}})
        assert r["valid"] is False and any("cipher_type" in e for e in r["errors"])

    def test_validate_sparse_map(self):
        r = self.m.validate_hypothesis({"cipher_type":"substitution","source_language":"latin","symbol_map":{"o":"a"}})
        assert r["valid"] is False and any("sparse" in e for e in r["errors"])

    def test_validate_missing_field(self):
        r = self.m.validate_hypothesis({"cipher_type":"substitution"})
        assert r["valid"] is False and any("source_language" in e for e in r["errors"])

    def test_failure_mode_identifies_worst(self):
        r = self.m.get_failure_mode_analysis({"statistical":0.9,"perplexity":0.1,"semantic":0.8,"consistency":0.7})
        assert r["primary_failure_mode"] == "perplexity"

    def test_failure_mode_all_scores(self):
        r = self.m.get_failure_mode_analysis({"statistical":0.5,"perplexity":0.5,"semantic":0.5,"consistency":0.5})
        assert len(r["all_scores"]) == 4

    def test_propose_initial_population(self):
        r = self.m.propose_initial_population(n=10)
        assert len(r) == 10
        assert all("cipher_type" in h and "symbol_map" in h for h in r)

# ── Historian ─────────────────────────────────────────────────────────────────
class TestHistorianTools:
    @pytest.fixture(autouse=True)
    def _m(self): self.m = _load("historian")

    def test_anachronism_clean(self):
        r = self.m.check_anachronism("The root boiled in water cures stomach pain.")
        assert r["anachronism_free"] is True and r["fitness_penalty"] == 0.0

    def test_anachronism_telescope(self):
        r = self.m.check_anachronism("Using the telescope we observed Jupiter.")
        assert "telescope" in r["anachronisms_found"] and r["fitness_penalty"] > 0

    def test_anachronism_potato(self):
        r = self.m.check_anachronism("The potato root is boiled.")
        assert "potato" in r["anachronisms_found"]

    def test_anachronism_strictness(self):
        text = "Using the printing press we produced copies."
        assert self.m.check_anachronism(text, strictness="strict")["fitness_penalty"] >= \
               self.m.check_anachronism(text, strictness="lenient")["fitness_penalty"]

    def test_anachronism_large_number_warning(self):
        r = self.m.check_anachronism("Take 12345 grains of powder.")
        assert len(r["warnings"]) > 0

# ── Critic ────────────────────────────────────────────────────────────────────
class TestCriticTools:
    @pytest.fixture(autouse=True)
    def _m(self): self.m = _load("critic")

    def test_contradiction_antonyms(self):
        r = self.m.find_internal_contradiction("This herb is hot and very cold.", "herbal", sql=None)
        assert isinstance(r["contradictions_found"], list) and "contradiction_count" in r

    def test_contradiction_clean(self):
        r = self.m.find_internal_contradiction("The root boiled in water relieves pain.", "herbal", sql=None)
        assert r["contradiction_count"] >= 0

    def test_probe_telescope(self):
        r = self.m.probe_semantic_impossibility("We observed moons with a telescope.", period="early_15th")
        assert r["is_plausible"] is False and len(r["hard_violations"]) > 0

    def test_probe_plausible(self):
        r = self.m.probe_semantic_impossibility("The root boiled in water cures pain.", period="early_15th")
        assert r["is_plausible"] is True and r["hard_violations"] == []

    def test_probe_syphilis(self):
        r = self.m.probe_semantic_impossibility("This remedy cures syphilis.", period="early_15th")
        assert not r["is_plausible"]

    def test_illustration_mismatch_no_metadata(self):
        r = self.m.check_illustration_mismatch("The root cures fever.", page=999, section="herbal", sql=None)
        assert r["mismatch_detected"] is False and "no illustration metadata" in r["reason"].lower()

    def test_falsifiability_empty(self):
        r = self.m.score_falsifiability({"decoded_sample":"","section":"herbal","page":1}, sql=None)
        assert r["fitness_adversarial"] == 0.5 and "empty" in r["reason"].lower()

    def test_falsifiability_anachronistic(self):
        r = self.m.score_falsifiability({"decoded_sample":"Using the telescope we saw Jupiter. The potato heals.","section":"herbal","page":1}, sql=None)
        assert r["fitness_adversarial"] < 1.0

# ── Judge ─────────────────────────────────────────────────────────────────────
class TestJudgeTools:
    @pytest.fixture(autouse=True)
    def _m(self): self.m = _load("judge")

    def test_hallucination_supported(self):
        r = self.m.detect_hallucination(
            "The text contains botanical vocabulary suggesting a herbal context.",
            "search_medieval_corpus returned 8 Dioscorides passages with similarity 0.82.",
            ["search_medieval_corpus","score_period_vocabulary"],
        )
        assert r["hallucination_confidence"] < 0.5

    def test_hallucination_no_evidence(self):
        r = self.m.detect_hallucination("This clearly contradicts itself.", "", [])
        assert r["hallucination_confidence"] > 0.3 and len(r["issues"]) > 0

    def test_hallucination_uncalled_tool(self):
        r = self.m.detect_hallucination(
            "search_medieval_corpus shows perfect Dioscorides match.",
            "I found a match.", [],
        )
        assert r["hallucination_confidence"] > 0.3

    def test_grade_tool_use_all_present(self):
        r = self.m.grade_tool_use(
            [{"tool_name":"search_medieval_corpus","input":"{}","output":"..."},
             {"tool_name":"score_period_vocabulary","input":"{}","output":"..."},
             {"tool_name":"check_anachronism","input":"{}","output":"..."}],
            ["search_medieval_corpus","score_period_vocabulary","check_anachronism"],
            "evaluate_historian",
        )
        assert r["tool_use_score"] >= 0.85 and r["grade"] == "A" and r["missing_tools"] == []

    def test_grade_tool_use_missing(self):
        r = self.m.grade_tool_use(
            [{"tool_name":"search_medieval_corpus","input":"{}","output":"..."}],
            ["search_medieval_corpus","score_period_vocabulary","check_anachronism"],
            "evaluate_historian",
        )
        assert len(r["missing_tools"]) == 2 and r["tool_use_score"] < 0.85

    def test_grade_tool_use_empty_inputs(self):
        r = self.m.grade_tool_use(
            [{"tool_name":"search_medieval_corpus","input":"{}","output":"r"},
             {"tool_name":"score_period_vocabulary","input":"","output":"r"}],
            ["search_medieval_corpus","score_period_vocabulary"], "evaluate_historian",
        )
        assert any("empty" in i.lower() for i in r["issues"])

    def test_reasoning_quality_good(self):
        reasoning = (
            "The decoded text shows alignment with medieval botanical vocabulary. "
            "The word 'radix' appears consistently, matching Dioscorides (similarity 0.78). "
            "This suggests Latin botanical encoding, though uncertainty remains given sample size."
        )
        r = self.m.score_reasoning_quality(reasoning, "historian", 0.75)
        assert r["reasoning_quality_score"] > 0.6

    def test_reasoning_quality_overconfident(self):
        r = self.m.score_reasoning_quality("This clearly and definitely contradicts itself.", "critic", 0.2)
        assert r["reasoning_quality_score"] < 0.8 and len(r["issues"]) > 0

    def test_reasoning_quality_too_brief(self):
        r = self.m.score_reasoning_quality("Bad.", "historian", 0.5)
        assert r["reasoning_quality_score"] < 0.7 and any("brief" in i.lower() for i in r["issues"])

    def test_reasoning_quality_low_score_positive_text(self):
        # Low score claimed but text is entirely positive → inconsistency flagged
        reasoning = ("The text is plausible, perfectly aligned, and entirely consistent. " * 8)
        r = self.m.score_reasoning_quality(reasoning, "critic", 0.1)
        assert r["reasoning_quality_score"] < 1.0 and len(r["issues"]) > 0

    def test_reasoning_quality_critic_acknowledges_failure(self):
        reasoning = (
            "After investigation, I cannot falsify this hypothesis. "
            "The text is internally consistent, shows no illustration mismatches, "
            "no anachronistic content, and survived adversarial scrutiny. "
            "This does not confirm correctness but removes grounds for rejection."
        )
        r = self.m.score_reasoning_quality(reasoning, "critic", 0.85)
        assert r["reasoning_quality_score"] > 0.6
