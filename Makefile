# Convenience targets — safe defaults (no heavy build/E2E in parallel).
.PHONY: reader-dev reader-verify reader-smoke reader-test-unit reader-test-realtime-api reader-vapid

reader-dev:
	bash docker/scripts/dev-story-reader.sh

reader-vapid:
	bash docker/scripts/generate-vapid-keys.sh

reader-verify:
	bash docker/scripts/verify-reader-dev.sh

reader-smoke:
	bash docker/scripts/smoke-reader-realtime.sh

reader-test-unit:
	cd story_reader && npm run test:unit

reader-test-realtime-api:
	cd story_reader && npm run test:e2e:realtime:api
