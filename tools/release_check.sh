#!/bin/sh
# release_check.sh -- release-readiness checker.
#
# Usage: tools/release_check.sh <version>
#   <version> must be a bare semantic version, e.g. 0.3.0
#
# Places checked for version agreement with <version>:
#   (a) pyproject.toml       -- the `version = "X.Y.Z"` declaration.
#   (b) README.md            -- the status line's `vX.Y` (major.minor only).
#   (f) compose.yaml         -- the default image tag `ghcr.io/.../patent-checker:X.Y`
#                               (major.minor only) in the `image:` line.
# Places intentionally NOT checked:
#   - patent_checker/__init__.py: `__version__` is derived at import time
#     from installed package metadata (importlib.metadata), it is not a
#     declaration and always follows pyproject.toml automatically.
#   - patent_checker/consent.py: NOTICE_VERSION is an independent consent
#     notice revision counter, unrelated to the release version number.
#   - any future OPERATOR_NOTICE_VERSION (server settings): same as above,
#     an independent notice revision counter, not a release version.
#
# When a new place declaring the version is added, add a check here.
#
# In addition to version agreement, this script also checks that:
#   (c) tools/check.sh (lint + format check + tests) passes.
#   (d) the working tree is clean (no uncommitted changes).
#   (e) all local commits are pushed to the tracking branch on origin.
#
# On success, prints "release_check: OK (<version>)" and exits 0.
# On failure, prints "release_check: FAILED (<version>)" followed by one
# line per failed check, in the form:
#   <file>:<line> -> <current value> -> <expected value>
# and exits 1.

set -u
cd "$(dirname "$0")/.."

if [ "$#" -ne 1 ]; then
    echo "Usage: tools/release_check.sh <version>" >&2
    echo "  <version> must match X.Y.Z, e.g. 0.3.0" >&2
    exit 2
fi

VERSION="$1"

# Reject anything that does not strictly match digits.digits.digits.
if ! echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "Usage: tools/release_check.sh <version>" >&2
    echo "  <version> must match X.Y.Z, e.g. 0.3.0" >&2
    exit 2
fi

# Derive MAJOR.MINOR (e.g. 0.3.0 -> 0.3).
MAJOR_MINOR=$(echo "$VERSION" | sed -E 's/^([0-9]+\.[0-9]+)\.[0-9]+$/\1/')

FAILURES_FILE=$(mktemp)
trap 'rm -f "$FAILURES_FILE"' EXIT

# --- (a) pyproject.toml: version = "X.Y.Z" -------------------------------
PYPROJECT_LINE=$(grep -nE '^version = "' pyproject.toml | head -n 1)
if [ -z "$PYPROJECT_LINE" ]; then
    echo "pyproject.toml:? -> (version line not found) -> version = \"$VERSION\"" >>"$FAILURES_FILE"
else
    PYPROJECT_LINENO=$(echo "$PYPROJECT_LINE" | cut -d: -f1)
    PYPROJECT_CURRENT=$(echo "$PYPROJECT_LINE" | cut -d: -f2- | sed -E 's/^version = "([^"]*)"$/\1/')
    if [ "$PYPROJECT_CURRENT" != "$VERSION" ]; then
        echo "pyproject.toml:$PYPROJECT_LINENO -> version = \"$PYPROJECT_CURRENT\" -> version = \"$VERSION\"" >>"$FAILURES_FILE"
    fi
fi

# --- (b) README.md: status line with vX.Y ---------------------------------
# The status line is the first line in README.md that mentions a version as
# "vX.Y" (major.minor only) alongside a status marker (e.g. "Status:").
# Pattern: a line containing "Status" (case-insensitive) and "vN.N".
README_LINE=$(grep -nE '[Ss]tatus.*v[0-9]+\.[0-9]+' README.md | head -n 1)
if [ -z "$README_LINE" ]; then
    echo "README.md:? -> (status line not found) -> v$MAJOR_MINOR" >>"$FAILURES_FILE"
else
    README_LINENO=$(echo "$README_LINE" | cut -d: -f1)
    README_CURRENT=$(echo "$README_LINE" | sed -E 's/^[0-9]+://' | grep -oE 'v[0-9]+\.[0-9]+' | head -n 1)
    if [ "$README_CURRENT" != "v$MAJOR_MINOR" ]; then
        echo "README.md:$README_LINENO -> $README_CURRENT -> v$MAJOR_MINOR" >>"$FAILURES_FILE"
    fi
fi

# --- (f) compose.yaml: default image tag X.Y --------------------------------
# The image line reads `image: ${PATENT_CHECKER_IMAGE:-ghcr.io/<owner>/patent-checker:X.Y}`;
# the default tag tracks major.minor so that `docker compose pull` follows
# patch releases of the same minor.
COMPOSE_LINE=$(grep -nE '^\s*image:.*patent-checker:[0-9]+\.[0-9]+' compose.yaml | head -n 1)
if [ -z "$COMPOSE_LINE" ]; then
    echo "compose.yaml:? -> (image line not found) -> patent-checker:$MAJOR_MINOR" >>"$FAILURES_FILE"
else
    COMPOSE_LINENO=$(echo "$COMPOSE_LINE" | cut -d: -f1)
    COMPOSE_CURRENT=$(echo "$COMPOSE_LINE" | sed -E 's/^[0-9]+://' | grep -oE 'patent-checker:[0-9]+\.[0-9]+' | head -n 1)
    if [ "$COMPOSE_CURRENT" != "patent-checker:$MAJOR_MINOR" ]; then
        echo "compose.yaml:$COMPOSE_LINENO -> $COMPOSE_CURRENT -> patent-checker:$MAJOR_MINOR" >>"$FAILURES_FILE"
    fi
fi

# --- (d) working tree is clean ---------------------------------------------
GIT_STATUS=$(git status --porcelain)
if [ -n "$GIT_STATUS" ]; then
    GIT_STATUS_COUNT=$(echo "$GIT_STATUS" | wc -l | tr -d ' ')
    echo "git status -> $GIT_STATUS_COUNT uncommitted change(s) -> clean working tree" >>"$FAILURES_FILE"
fi

# --- (e) all commits pushed -------------------------------------------------
if ! git rev-parse --abbrev-ref --symbolic-full-name @{u} >/dev/null 2>&1; then
    echo "git upstream -> (none) -> tracking branch on origin" >>"$FAILURES_FILE"
else
    UNPUSHED=$(git log @{u}..HEAD --oneline)
    if [ -n "$UNPUSHED" ]; then
        UNPUSHED_COUNT=$(echo "$UNPUSHED" | wc -l | tr -d ' ')
        echo "git push -> $UNPUSHED_COUNT unpushed commit(s) -> 0" >>"$FAILURES_FILE"
    fi
fi

# --- (c) lint + format + tests ----------------------------------------------
# Output from tools/check.sh is left to stream normally (not captured) so
# that lint/test failures remain readable; only the exit code is recorded.
./tools/check.sh
CHECK_EXIT=$?
if [ "$CHECK_EXIT" -ne 0 ]; then
    echo "tools/check.sh -> exit $CHECK_EXIT -> exit 0" >>"$FAILURES_FILE"
fi

if [ -s "$FAILURES_FILE" ]; then
    echo "release_check: FAILED ($VERSION)"
    cat "$FAILURES_FILE"
    exit 1
fi

echo "release_check: OK ($VERSION)"
exit 0
