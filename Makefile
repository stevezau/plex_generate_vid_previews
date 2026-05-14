.PHONY: help test test-e2e test-e2e-serial test-integration lint format

PYTEST := pytest

help:
	@echo "Available targets:"
	@echo "  test            — default unit suite (xdist auto, excludes e2e/integration/gpu)"
	@echo "  test-e2e        — Playwright e2e suite at -n 8 (capped to avoid kernel OOM)"
	@echo "  test-e2e-serial — Playwright e2e suite at -n 0 (safest, slowest)"
	@echo "  test-integration— Live-Docker integration suite (requires servers.env)"
	@echo "  lint            — ruff check"
	@echo "  format          — ruff format"

test:
	$(PYTEST)

# Local e2e cap at -n 8 — verified stable. -n auto (24 workers on a multi-core
# box) spawns ~120 chromium processes whose combined virtual-memory commit
# exceeds the kernel's overcommit ceiling, triggering OOM kills against
# chrome-headless (oom_score_adj=300). See CLAUDE.md for the full diagnosis
# or `journalctl --since 1h | grep "Out of memory"` to verify yourself.
# CI uses its own pattern: 4-shard matrix × -n 0 serial per shard.
test-e2e:
	$(PYTEST) -m e2e -n 8 --no-cov

test-e2e-serial:
	$(PYTEST) -m e2e -n 0 --no-cov

test-integration:
	$(PYTEST) -m integration --no-cov tests/integration/

lint:
	ruff check .

format:
	ruff format .
