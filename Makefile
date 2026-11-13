.DEFAULT_GOAL := help
.PHONY: help install test test-affected lint contracts typecheck check pre-commit

UV ?= uv
export UV_PYTHON ?= 3.13
TEST_ARGS ?= -x
BASE ?= origin/main

help:
	@printf '%s\n' \
	  'make install        Sync the locked developer environment' \
	  'make test           Run isolated tests (override TEST_ARGS for a file)' \
	  'make test-affected  Select tests affected by changes against BASE' \
	  'make check          Check lint, formatting, import contracts and strict types' \
	  'make pre-commit     Run the pre-commit checks against staged files'

install:
	$(UV) sync --locked --group dev

test:
	$(UV) run --locked python scripts/run_tests.py $(TEST_ARGS)

test-affected:
	$(UV) run --locked python scripts/run_tests.py --affected $(BASE)

lint:
	$(UV) run --locked ruff check src/ tests/ scripts/
	$(UV) run --locked ruff format --check src/ tests/ scripts/

contracts:
	$(UV) run --locked lint-imports

typecheck:
	$(UV) run --locked pyright --project pyrightconfig.strict.json

check: lint contracts typecheck

pre-commit:
	$(UV) tool run --python $(UV_PYTHON) pre-commit run
