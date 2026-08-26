"""EPO OPS connectivity check (completion requirement R1).

Obtains an OAuth2 access token with the client-credentials flow and runs one
trivial published-data search, printing the HTTP status, the total result
count, and the ``X-Throttling-Control`` header. Credentials never appear in
the output.
"""

from __future__ import annotations

import base64
import sys
import xml.etree.ElementTree as ET

import httpx
from config import ConfigError, ops_credentials

OPS_BASE = "https://ops.epo.org/3.2"
TOKEN_URL = f"{OPS_BASE}/auth/accesstoken"
SEARCH_URL = f"{OPS_BASE}/rest-services/published-data/search"

# Deliberately generic query: connectivity only, no project-related terms.
TEST_QUERY = "ti=computer"


def fetch_token(client: httpx.Client, key: str, secret: str) -> str:
    """Obtain an OAuth2 access token via the client-credentials flow."""
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    resp = client.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "client_credentials"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_count(client: httpx.Client, token: str, query: str) -> tuple[int, int, str]:
    """Run a search; return ``(HTTP status, total result count, throttling header)``.

    The count is read from the ``total-result-count`` attribute of the
    ``biblio-search`` element (-1 if the element is missing).
    """
    resp = client.get(
        SEARCH_URL,
        params={"q": query},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    node = root.find(".//{*}biblio-search")
    count = int(node.get("total-result-count", "-1")) if node is not None else -1
    throttling = resp.headers.get("X-Throttling-Control", "(absent)")
    return resp.status_code, count, throttling


def main() -> int:
    """Run the connectivity check and return the process exit code."""
    try:
        key, secret = ops_credentials()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    try:
        with httpx.Client(timeout=30.0) as client:
            token = fetch_token(client, key, secret)
            status, count, throttling = search_count(client, token, TEST_QUERY)
    except httpx.HTTPStatusError as exc:
        print(
            f"OPS request failed: HTTP {exc.response.status_code} for {exc.request.url}",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as exc:
        print(f"OPS request failed: {exc}", file=sys.stderr)
        return 1

    print(f"OPS search: HTTP {status}, query {TEST_QUERY!r}, total-result-count={count}")
    print(f"X-Throttling-Control: {throttling}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
