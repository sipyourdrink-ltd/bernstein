#!/usr/bin/env bash
# Quick local lint cycle for backend changes.
set -euo pipefail

uv run ruff check src/
uv run ruff format --check src/
uv run pyright src/
