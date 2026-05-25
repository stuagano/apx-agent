.PHONY: builder-ui wheel

builder-ui:
	cd python/builder-ui && npm ci && npm run build:dist

wheel: builder-ui
	cd python && python3 -m hatchling build --target wheel
	cp python/dist/apx_agent-*.whl python/hello-world/
	cp python/dist/apx_agent-*.whl python/hello-world/.build/
	@HASH=$$(python3 -c "import hashlib,glob; f=sorted(glob.glob('python/hello-world/apx_agent-*.whl'))[-1]; h=hashlib.sha256(); h.update(open(f,'rb').read()); print(h.hexdigest())"); \
	sed -i '' "s/sha256:[a-f0-9]\{64\}/sha256:$$HASH/g" python/hello-world/.build/uv.lock && \
	echo "uv.lock hash updated: $$HASH"
