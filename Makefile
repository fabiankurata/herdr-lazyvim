.PHONY: audit test test-isolated test-operations live-isolation perf-compare

audit:
	bash scripts/check-public.sh

test:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/live/isolated_test.py $(MAKE) test-isolated

test-isolated:
	@test -n "$$HERDR_TEST_STATE_ROOT" || { echo 'use make test for an isolated environment' >&2; exit 1; }
	bash tests/plugin_test.sh
	bash -n scripts/setup-macos.sh
	bash scripts/setup-macos.sh plan >/dev/null
	HERDR_NVIM_TEST_SCRIPT="$(CURDIR)/tests/nvim_smoke.lua" nvim --headless -u NONE -i NONE -l tests/nvim/run_test.lua
	HERDR_TEST_FIXTURE_ROOT="$$HERDR_TEST_STATE_ROOT/async-fixture" HERDR_TEST_REPO="$(CURDIR)" HERDR_NVIM_TEST_SCRIPT="$(CURDIR)/tests/nvim/async_root_baseline.lua" nvim --headless -u NONE -i NONE -l tests/nvim/run_test.lua
	$(MAKE) test-operations
	python3 tests/public_scan.py
	python3 tests/contracts/check.py
	bash tests/live/test-isolation
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/perf -p 'test_*.py' -v
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/profiles -p 'test_*.py' -v
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/native -p 'test_*.py' -v
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/prototypes -p 'test_*.py' -v

test-operations:
	@test -n "$$HERDR_TEST_STATE_ROOT" || { echo 'use make test-operations through the isolated test runner' >&2; exit 1; }
	HERDR_TEST_FIXTURE_ROOT="$$HERDR_TEST_STATE_ROOT/operations-fixture" HERDR_TEST_REPO="$(CURDIR)" HERDR_NVIM_TEST_SCRIPT="$(CURDIR)/tests/nvim/operations.lua" nvim --headless -u NONE -i NONE -l tests/nvim/run_test.lua
	PYTHONDONTWRITEBYTECODE=1 python3 tests/nvim/test_review_concurrency.py -v

live-isolation:
	PYTHONDONTWRITEBYTECODE=1 python3 tests/live/isolated_test.py bash $(CURDIR)/tests/live/test-isolation

perf-compare:
	tests/perf/compare --scenario controller-choice --baseline $$(git rev-parse HEAD) --candidate $$(git rev-parse HEAD) --samples 30
