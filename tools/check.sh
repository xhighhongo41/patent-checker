#!/bin/sh
# Run lint and tests in one pass.
set -e
cd "$(dirname "$0")/.."
if [ ! -d tests/fixtures ]; then
	echo "tests/fixtures/ is missing: this developer check requires the" >&2
	echo "(untracked) real fixtures; see tests/_fixtures.py." >&2
	exit 1
fi
uv run ruff check .
uv run ruff format --check .
sh -n install.sh
uv run pytest -q --fixtures-required
