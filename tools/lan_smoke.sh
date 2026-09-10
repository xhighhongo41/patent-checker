#!/bin/sh
# lan_smoke.sh -- bring up examples/lan-tls (server + Caddy) and check that
# the health endpoint answers over TLS through the proxy.
#
# Written for CI. It is informative: Caddy has to be pulled and mints a
# certificate from its internal CA on first start, so the CI step that runs
# this script is allowed to fail without blocking the pipeline.
#
# Usage:
#   PATENT_CHECKER_IMAGE=patent-checker:ci tools/lan_smoke.sh
#
# Environment:
#   PATENT_CHECKER_IMAGE  Required. The locally built image to run.
#   LAN_SMOKE_PORT        Host port Caddy publishes (default 8443).
#
# The generated bearer token is never printed.

set -eu

PROJECT=patent-checker-lan-smoke
PORT=${LAN_SMOKE_PORT:-8443}
HOST=patents.test
REPO=$(cd -- "$(dirname -- "$0")/.." && pwd)

if [ -z "${PATENT_CHECKER_IMAGE:-}" ]; then
    echo "Usage: PATENT_CHECKER_IMAGE=<tag> tools/lan_smoke.sh" >&2
    exit 2
fi
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 is required" >&2; exit 1; }

TMP=$(mktemp -d)
STACK_UP=0

compose() {
    docker compose \
        --project-name "$PROJECT" \
        --project-directory "$TMP" \
        --file "$REPO/examples/lan-tls/compose.yaml" \
        "$@"
}

redact() {
    if [ -n "${TOKEN:-}" ]; then sed "s|${TOKEN}|<token redacted>|g"; else cat; fi
}

cleanup() {
    status=$?
    trap - EXIT
    set +e
    if [ "$STACK_UP" -eq 1 ]; then
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

fail() { echo "FAIL: $1" >&2; exit 1; }

# --- fixtures ---------------------------------------------------------------

mkdir -p "$TMP/secrets"
cp "$REPO/examples/lan-tls/Caddyfile" "$TMP/Caddyfile"
: > "$TMP/secrets/ops_key.txt"
: > "$TMP/secrets/ops_secret.txt"
if command -v python3 >/dev/null 2>&1; then
    TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
else
    TOKEN=$(openssl rand -hex 32)
fi
printf '%s\n' "$TOKEN" > "$TMP/secrets/server_token.txt"
{
    echo "PATENT_CHECKER_OPERATOR_CONSENT=1.0"
    echo "PATENT_CHECKER_IMAGE=$PATENT_CHECKER_IMAGE"
    echo "PATENT_CHECKER_LAN_HOST=$HOST"
    echo "PATENT_CHECKER_LAN_BIND=127.0.0.1"
    echo "PATENT_CHECKER_LAN_PORT=$PORT"
} > "$TMP/.env"
export PATENT_CHECKER_OPERATOR_CONSENT=1.0
export PATENT_CHECKER_IMAGE
export PATENT_CHECKER_LAN_HOST="$HOST"
export PATENT_CHECKER_LAN_BIND=127.0.0.1
export PATENT_CHECKER_LAN_PORT="$PORT"
echo "PASS: fixtures prepared"

# --- up ----------------------------------------------------------------------

STACK_UP=1
compose up --detach --wait --wait-timeout 180
echo "PASS: server and Caddy started"

# --- TLS through the proxy ----------------------------------------------------

# Caddy's internal CA is not trusted by the runner, so the certificate is not
# verified here (-k); what is checked is that TLS terminates, the Host header
# is accepted by the server behind the proxy, and /health answers.
HEALTH=""
for attempt in 1 2 3 4 5 6; do
    HEALTH=$(curl -ksS --resolve "$HOST:$PORT:127.0.0.1" "https://$HOST:$PORT/health" 2>/dev/null) && break
    sleep 5
done
case "$HEALTH" in
    *'"status"'*'"ok"'*) ;;
    *) fail "GET /health through Caddy did not answer ok: $HEALTH" ;;
esac
echo "PASS: GET /health answers ok over TLS through Caddy"

CODE=$(curl -ks -o /dev/null -w '%{http_code}' --resolve "$HOST:$PORT:127.0.0.1" \
    -X POST -H 'Content-Type: application/json' -d '{}' "https://$HOST:$PORT/mcp")
[ "$CODE" = "401" ] || fail "expected 401 from POST /mcp without a token through Caddy, got $CODE"
echo "PASS: POST /mcp without a token is refused with 401 through Caddy"

if compose logs --no-color 2>&1 | grep -qF -- "$TOKEN"; then
    fail "the bearer token appeared in the logs"
fi
echo "lan smoke: OK"
