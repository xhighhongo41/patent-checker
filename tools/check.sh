#!/bin/sh
# Run lint and tests in one pass.
# pytest exit code 5 (no tests collected) is treated as success so this
# script stays green before the first test file exists.
set -e
cd "$(dirname "$0")/.."
uv run ruff check .
uv run ruff format --check .
uv run pytest -q || { rc=$?; [ "$rc" -eq 5 ] || exit "$rc"; }
