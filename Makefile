.PHONY: wheel check lint

# Read-after-write verify gate (Ctk). Runs the full pytest suite, which includes
# the *_reality_ctk.py claim-vs-reality tests. Run this before claiming a change
# works — a green exit code here is the read-back, not just "it ran".
check:
	cd python && uv run pytest

# Lint suite from .pre-commit-config.yaml (enforces the Ponytail smells). Uses
# uvx so it works without pre-commit installed in the project env; needs network
# the first time to fetch the hooks.
lint:
	cd python && uvx pre-commit run --all-files --config ../.pre-commit-config.yaml


wheel:
	# Clean first: apx-agent's version is vcs-derived, so every build stamps a
	# new filename. Left to accumulate, `sorted(glob)[-1]` picks the
	# lexicographically-last wheel (0.4.10 sorts BEFORE 0.4.2) — silently
	# hashing/deploying a STALE wheel. Remove old wheels so exactly one exists.
	rm -f python/dist/apx_agent-*.whl python/hello-world/apx_agent-*.whl python/hello-world/.build/apx_agent-*.whl
	cd python && python3 -m hatchling build --target wheel
	cp python/dist/apx_agent-*.whl python/hello-world/
	cp python/dist/apx_agent-*.whl python/hello-world/.build/ 2>/dev/null || true
	@WHEEL=$$(ls python/dist/apx_agent-*.whl 2>/dev/null); \
	COUNT=$$(printf '%s\n' $$WHEEL | grep -c .); \
	if [ "$$COUNT" != "1" ]; then \
		echo "ERROR: expected exactly one built wheel, found $$COUNT: $$WHEEL" >&2; exit 1; \
	fi; \
	BASENAME=$$(basename $$WHEEL); \
	HASH=$$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$$WHEEL"); \
	LOCK=python/hello-world/.build/uv.lock; \
	if [ ! -f "$$LOCK" ]; then \
		echo "ERROR: $$LOCK not found — run 'apx-agent deploy' to generate the wheel-pinned lock" >&2; exit 1; \
	fi; \
	if ! grep -q "$$BASENAME" $$LOCK; then \
		echo "ERROR: $$LOCK pins a different apx_agent wheel than the one just built ($$BASENAME)." >&2; \
		echo "  The version bumped (vcs-derived), so patching only the hash would leave the lock" >&2; \
		echo "  pointing at a stale filename. Regenerate it with 'apx-agent deploy' (which wheel-pins" >&2; \
		echo "  .build/pyproject.toml + uv.lock together)." >&2; \
		exit 1; \
	fi; \
	BEFORE=$$(grep -oE "sha256:[a-f0-9]{64}" $$LOCK | sort -u | wc -l | tr -d ' '); \
	sed -i '' "/$$BASENAME/ s/sha256:[a-f0-9]\{64\}/sha256:$$HASH/" $$LOCK; \
	AFTER=$$(grep -oE "sha256:[a-f0-9]{64}" $$LOCK | sort -u | wc -l | tr -d ' '); \
	if [ "$$BEFORE" != "$$AFTER" ]; then \
		echo "ERROR: distinct sha256 count changed ($$BEFORE -> $$AFTER) — sed clobbered non-apx hashes" >&2; \
		exit 1; \
	fi; \
	echo "uv.lock hash updated for $$BASENAME: $$HASH"
