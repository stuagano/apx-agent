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
	cd python && python3 -m hatchling build --target wheel
	cp python/dist/apx_agent-*.whl python/hello-world/
	cp python/dist/apx_agent-*.whl python/hello-world/.build/
	@HASH=$$(python3 -c "import hashlib,glob; f=sorted(glob.glob('python/hello-world/apx_agent-*.whl'))[-1]; h=hashlib.sha256(); h.update(open(f,'rb').read()); print(h.hexdigest())"); \
	LOCK=python/hello-world/.build/uv.lock; \
	BEFORE=$$(grep -oE "sha256:[a-f0-9]{64}" $$LOCK | sort -u | wc -l | tr -d ' '); \
	sed -i '' "/apx_agent-.*\.whl/ s/sha256:[a-f0-9]\{64\}/sha256:$$HASH/" $$LOCK; \
	AFTER=$$(grep -oE "sha256:[a-f0-9]{64}" $$LOCK | sort -u | wc -l | tr -d ' '); \
	if [ "$$BEFORE" != "$$AFTER" ]; then \
		echo "ERROR: distinct sha256 count changed ($$BEFORE -> $$AFTER) — sed clobbered non-apx hashes" >&2; \
		exit 1; \
	fi; \
	if ! grep -qE "apx_agent-.*sha256:$$HASH" $$LOCK; then \
		echo "ERROR: apx_agent wheel hash not patched to $$HASH — wheel filename format may have drifted" >&2; \
		exit 1; \
	fi; \
	echo "uv.lock hash updated: $$HASH"
