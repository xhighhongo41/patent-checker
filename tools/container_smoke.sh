#!/bin/sh
# container_smoke.sh -- end-to-end smoke test of the container image.
#
# Written for CI (GitHub Actions, ubuntu-latest). It brings up the shipped
# compose stack against a locally built image and checks the behaviour that
# only appears once the server runs in a container: the unauthenticated
# health endpoint, the bearer gate on /mcp, that the token never reaches the
# logs, and the operator-consent start gate.
#
# Usage:
#   PATENT_CHECKER_IMAGE=patent-checker:ci tools/container_smoke.sh
#
# Environment:
#   PATENT_CHECKER_IMAGE  Required. The image tag to exercise. It must exist
#                         locally: the stack is started without --build, so
#                         Compose reuses it instead of building.
#   SMOKE_PORT            Host port to publish on (default 8642). The server
#                         inside the container always listens on 8642 and
#                         accepts the Host header only as `127.0.0.1`,
#                         `localhost`, or either of those with port 8642, so
#                         another value also needs
#                         PATENT_CHECKER_SERVER_ALLOWED_HOSTS widened.
#
# The generated bearer token is never printed: not on success, and not in the
# diagnostic log dump on failure, which has it redacted.
#
# Exit codes: 0 all checks passed, 1 a check failed or docker is missing,
# 2 usage error.

set -eu

PROJECT=patent-checker-smoke
PORT=${SMOKE_PORT:-8642}
REPO=$(cd -- "$(dirname -- "$0")/.." && pwd)

if [ -z "${PATENT_CHECKER_IMAGE:-}" ]; then
    echo "Usage: PATENT_CHECKER_IMAGE=<tag> tools/container_smoke.sh" >&2
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required" >&2
    exit 1
fi

# Checked separately from the docker CLI: the compose plugin is a distinct
# install, and its absence otherwise surfaces as an unhelpful "'compose' is
# not a docker command".
if ! docker compose version >/dev/null 2>&1; then
    echo "docker compose v2 is required" >&2
    exit 1
fi

# --- helpers ---------------------------------------------------------------

# Every Compose call needs the same three options: the compose file lives in
# the repository, but the project directory is the throwaway one, so that
# `./secrets/*.txt` and `.env` resolve to the fixtures built below instead of
# to the developer's real ones.
compose() {
    docker compose \
        --project-name "$PROJECT" \
        --project-directory "$TMP" \
        --file "$REPO/compose.yaml" \
        "$@"
}

# Filter for anything that might have seen the token. Base64url and hex
# tokens contain no sed metacharacters, so the pattern needs no escaping.
redact() {
    if [ -n "${TOKEN:-}" ]; then
        sed "s|${TOKEN}|<token redacted>|g"
    else
        cat
    fi
}

TMP=$(mktemp -d)
STACK_UP=0

cleanup() {
    status=$?
    trap - EXIT
    set +e
    if [ "$STACK_UP" -eq 1 ]; then
        # Logs are only worth printing when something went wrong, and only
        # with the token stripped -- CI output is not a private place.
        if [ "$status" -ne 0 ]; then
            echo "--- docker compose logs (token redacted) ---" >&2
            compose logs --no-color 2>&1 | redact >&2
        fi
        compose down --volumes --remove-orphans >/dev/null 2>&1
    fi
    rm -rf "$TMP"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

# --- 1. the image runs at all ----------------------------------------------

docker run --rm "$PATENT_CHECKER_IMAGE" --version
echo "PASS: the image runs its entrypoint and reports a version"

# --- 2. fixtures ------------------------------------------------------------

mkdir -p "$TMP/secrets"

# Empty OPS credentials on purpose: the server must come up in degraded mode
# rather than refuse to start.
: > "$TMP/secrets/ops_key.txt"
: > "$TMP/secrets/ops_secret.txt"

if command -v python3 >/dev/null 2>&1; then
    TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
elif command -v openssl >/dev/null 2>&1; then
    TOKEN=$(openssl rand -hex 32)
else
    fail "python3 or openssl is required to generate a bearer token"
fi
printf '%s\n' "$TOKEN" > "$TMP/secrets/server_token.txt"

# The secret files keep their default mode. Compose bind-mounts them as they
# are and the container runs as UID 1000, which is not the CI runner's UID,
# so tightening them to 600 here would only make them unreadable inside. The
# directory is a throwaway one under $TMPDIR and is removed on exit.

# Compose reads .env from the project directory to fill compose.yaml's
# placeholders.
{
    echo "PATENT_CHECKER_OPERATOR_CONSENT=1.0"
    echo "PATENT_CHECKER_IMAGE=$PATENT_CHECKER_IMAGE"
    echo "PATENT_CHECKER_PORT=$PORT"
} > "$TMP/.env"

# Exported as well, so the run does not depend on how Compose locates .env
# when --project-directory is given. Same values either way.
export PATENT_CHECKER_OPERATOR_CONSENT=1.0
export PATENT_CHECKER_IMAGE
export PATENT_CHECKER_PORT="$PORT"

echo "PASS: fixtures prepared (empty OPS credentials, generated bearer token)"

# --- 3. bring the stack up --------------------------------------------------

STACK_UP=1
compose up --detach --wait --wait-timeout 120
echo "PASS: the stack started and its healthcheck reports healthy"

# --- 4. GET /health is unauthenticated --------------------------------------

HEALTH=$(curl -fsS "http://127.0.0.1:$PORT/health") \
    || fail "GET /health did not answer"
case "$HEALTH" in
    *'"status"'*'"ok"'*) ;;
    *) fail "unexpected /health response: $HEALTH" ;;
esac
echo "PASS: GET /health answers ok without a token"

# --- 5. POST /mcp without a token is refused --------------------------------

CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST \
    -H 'Content-Type: application/json' \
    -d '{}' \
    "http://127.0.0.1:$PORT/mcp") || fail "POST /mcp without a token did not answer"
[ "$CODE" = "401" ] || fail "expected 401 from POST /mcp without a token, got $CODE"
echo "PASS: POST /mcp without a bearer token is refused with 401"

# --- 6. POST /mcp with the token gets past the auth gate --------------------

# The Authorization header is handed to curl in a file (-H @file) instead of
# on the command line, where every account on the machine could read it out
# of the process list. The file is created empty, tightened to 600, and only
# then filled; it lives in $TMP, which cleanup() removes.
HDR="$TMP/auth_header"
: > "$HDR"
chmod 600 "$HDR"
printf 'Authorization: Bearer %s\n' "$TOKEN" > "$HDR"

# Anything but 401 means the token was accepted; the body is deliberately not
# a valid JSON-RPC request, so the exact status is the transport's business.
CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST \
    -H @"$HDR" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{}' \
    "http://127.0.0.1:$PORT/mcp") || fail "POST /mcp with a token did not answer"
rm -f "$HDR"
[ "$CODE" != "401" ] || fail "the bearer token was rejected on POST /mcp"
echo "PASS: POST /mcp with the bearer token passes the auth gate (HTTP $CODE)"

# --- 7. the token stays out of the logs -------------------------------------

if compose logs --no-color 2>&1 | grep -qF -- "$TOKEN"; then
    fail "the bearer token appeared in the container logs"
fi
echo "PASS: the bearer token does not appear in the container logs"

# --- 8. the operator notice can be read from the container ------------------

NOTICE=$(compose run --rm -T patent-checker serve --show-operator-notice)
case "$NOTICE" in
    *"Operator Notice"*) ;;
    *) fail "serve --show-operator-notice did not print the operator notice" ;;
esac
echo "PASS: serve --show-operator-notice prints the operator notice"

# --- 9. a stale acknowledgement stops the server with exit code 4 -----------

set +e
CONSENT_OUT=$(compose run --rm -T \
    -e PATENT_CHECKER_OPERATOR_CONSENT=0.9 \
    patent-checker serve 2>&1)
CONSENT_EXIT=$?
set -e
if [ "$CONSENT_EXIT" -ne 4 ]; then
    printf '%s\n' "$CONSENT_OUT" | redact >&2
    fail "expected exit code 4 for an unacknowledged operator notice, got $CONSENT_EXIT"
fi
echo "PASS: an unacknowledged operator notice stops the server with exit code 4"

# --- 10. the hardening in compose.yaml is in effect --------------------------

CID=$(compose ps --quiet patent-checker)
[ -n "$CID" ] || fail "could not find the running patent-checker container"
INSPECT=$(docker inspect --format \
    '{{.HostConfig.ReadonlyRootfs}} {{join .HostConfig.CapDrop ","}} {{join .HostConfig.SecurityOpt ","}}' \
    "$CID")
case "$INSPECT" in
    true*ALL*no-new-privileges*) ;;
    *) fail "hardening not applied (ReadonlyRootfs CapDrop SecurityOpt): $INSPECT" ;;
esac
echo "PASS: root filesystem read-only, capabilities dropped, no-new-privileges set"

# --- 11. the LAN/TLS example is a valid compose file ------------------------

LAN="$TMP/lan"
mkdir -p "$LAN/secrets"
cp "$REPO/examples/lan-tls/Caddyfile" "$LAN/Caddyfile"
: > "$LAN/secrets/ops_key.txt"
: > "$LAN/secrets/ops_secret.txt"
printf '%s\n' "$TOKEN" > "$LAN/secrets/server_token.txt"
PATENT_CHECKER_LAN_HOST=patents.test docker compose \
    --project-name "$PROJECT-lan" \
    --project-directory "$LAN" \
    --file "$REPO/examples/lan-tls/compose.yaml" \
    config --quiet || fail "examples/lan-tls/compose.yaml does not validate"
echo "PASS: examples/lan-tls/compose.yaml validates with docker compose config"

echo "container smoke: OK"
