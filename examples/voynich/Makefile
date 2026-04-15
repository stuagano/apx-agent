.PHONY: install test lint typecheck clean build deploy-wheel help

# ── Config ─────────────────────────────────────────────────────────────────

PYTHON        ?= python3
UV            ?= uv
PYTEST        ?= $(PYTHON) -m pytest
PYRIGHT       ?= pyright
RUFF          ?= ruff
WHEEL_OUT     ?= dist/
DBFS_WHEEL    ?= dbfs:/voynich/wheels/
DATABRICKS    ?= databricks

# ── Help ───────────────────────────────────────────────────────────────────

help:
	@echo "voynich-agents development targets"
	@echo ""
	@echo "  install        Install loop_agent package + dev deps"
	@echo "  test           Run full test suite (54 tests)"
	@echo "  test-fast      Run only loop_agent tests (no agent tool tests)"
	@echo "  lint           Ruff lint check"
	@echo "  typecheck      Pyright type check"
	@echo "  clean          Remove build artifacts and caches"
	@echo "  build          Build wheel for loop_agent package"
	@echo "  upload-wheel   Build and upload wheel to DBFS"
	@echo "  deploy-agents  Deploy all five agents as Databricks Apps"
	@echo "  seed           Seed generation 0 via voynich-seed CLI"
	@echo "  run-batch      Run 10 test generations via voynich-run CLI"

# ── Install ────────────────────────────────────────────────────────────────

install:
	$(UV) pip install -e ".[dev]"
	@echo "✓ loop_agent installed in editable mode"

install-agents:
	@for agent in decipherer historian critic judge orchestrator; do \
		echo "Installing agents/$$agent..."; \
		$(UV) pip install -e "agents/$$agent"; \
	done
	@echo "✓ All agents installed"

# ── Test ───────────────────────────────────────────────────────────────────

test:
	$(PYTEST) tests/ -v --tb=short

test-fast:
	$(PYTEST) tests/test_loop_agent.py -v --tb=short

test-agents:
	$(PYTEST) tests/agents/ -v --tb=short

test-ci:
	$(PYTEST) tests/ -v --tb=short --junit-xml=test-results/results.xml

coverage:
	$(PYTEST) tests/ --cov=loop_agent --cov-report=term-missing --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# ── Lint & Type ────────────────────────────────────────────────────────────

lint:
	$(RUFF) check loop_agent/ agents/ tests/ --select E,F,W --ignore E501
	@echo "✓ Lint passed"

lint-fix:
	$(RUFF) check loop_agent/ agents/ tests/ --select E,F,W --ignore E501 --fix

typecheck:
	$(PYRIGHT) loop_agent/ --pythonversion 3.11
	@echo "✓ Type check passed"

# ── Build ──────────────────────────────────────────────────────────────────

clean:
	rm -rf dist/ build/ *.egg-info .coverage htmlcov/ test-results/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "✓ Clean"

build: clean
	$(PYTHON) -m build
	@echo "✓ Wheel built: $$(ls dist/*.whl)"

# ── Databricks deploy ──────────────────────────────────────────────────────

upload-wheel: build
	$(DATABRICKS) fs cp dist/*.whl $(DBFS_WHEEL) --overwrite
	@echo "✓ Wheel uploaded to $(DBFS_WHEEL)"

deploy-agents:
	@echo "Deploying agents as Databricks Apps..."
	@for agent in decipherer historian critic judge orchestrator; do \
		echo "  Deploying $$agent..."; \
		cd agents/$$agent && $(DATABRICKS) apps deploy --name voynich-$$agent && cd ../..; \
	done
	@echo "✓ All agents deployed"

deploy-schema:
	$(DATABRICKS) sql execute --warehouse-id $$DATABRICKS_WAREHOUSE_ID < schema.sql
	@echo "✓ Schema deployed"

deploy-workflow:
	$(DATABRICKS) jobs create --json @voynich_workflow.yml
	@echo "✓ Workflow created"

update-workflow:
	$(DATABRICKS) jobs reset --job-id $$VOYNICH_JOB_ID --json @voynich_workflow.yml
	@echo "✓ Workflow updated"

# ── Local CLI testing ──────────────────────────────────────────────────────

seed: install
	VOYNICH_POPULATION_TABLE=voynich.evolution.population \
	DECIPHERER_AGENT_URL=$$DECIPHERER_AGENT_URL \
	JUDGE_AGENT_URL=$$JUDGE_AGENT_URL \
	DATABRICKS_WAREHOUSE_ID=$$DATABRICKS_WAREHOUSE_ID \
	voynich-seed --n 10
	@echo "✓ Seeded 10 test hypotheses"

run-batch: install
	VOYNICH_POPULATION_TABLE=voynich.evolution.population \
	DECIPHERER_AGENT_URL=$$DECIPHERER_AGENT_URL \
	JUDGE_AGENT_URL=$$JUDGE_AGENT_URL \
	FITNESS_AGENT_URLS=$$HISTORIAN_AGENT_URL,$$CRITIC_AGENT_URL \
	DATABRICKS_WAREHOUSE_ID=$$DATABRICKS_WAREHOUSE_ID \
	voynich-run --n_generations 10
	@echo "✓ Ran 10 test generations"

# ── Dev server (single agent) ──────────────────────────────────────────────

dev-%:
	@echo "Starting $* agent dev server on port 8001..."
	cd agents/$* && uvicorn main:app --reload --port 8001

# e.g. make dev-decipherer, make dev-historian, make dev-critic
