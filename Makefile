.PHONY: builder-ui

builder-ui:
	cd python/builder-ui && npm ci && npm run build:dist
