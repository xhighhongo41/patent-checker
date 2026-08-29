#!/bin/sh
# Run lint and tests in one pass.
set -e
cd "$(dirname "$0")/.."
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
