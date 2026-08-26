"""EPO OPS client for the v0.1 PoC.

Observation-first design (plan section 4.1): every request saves the raw
response body under ``raw/ops/`` and appends the response headers to
``raw/ops/headers.jsonl`` so throttling behaviour can be analyzed later.
Requests are spaced per OPS service; the ``X-Throttling-Control`` header is
recorded on every call and a non-green service state triggers a cool-down
before the next request.

Command line usage (connectivity probe; saves raw XML for one document)::

    uv run python poc/ops_client.py probe US11468338B2
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from config import data_dir, ops_credentials
from pubnum import parse_pubnum

OPS_BASE = "https://ops.epo.org/3.2"
TOKEN_URL = f"{OPS_BASE}/auth/accesstoken"

# Seconds a token is treated as valid (OPS issues ~20 min tokens).
TOKEN_LIFETIME_SECONDS = 18 * 60

# Minimum interval between requests, per OPS throttling service
# (X-Throttling-Control reports per-minute limits; search is the tightest).
MIN_INTERVAL_SECONDS = {
    "search": 4.0,
    "retrieval": 1.0,
    "inpadoc": 2.0,
    "other": 1.0,
}

# Cool-down applied when X-Throttling-Control reports a non-green state
# for the service that was just used.
COOL_DOWN_SECONDS = 60.0

_THROTTLING_STATE_RE = re.compile(r"(\w+)=(\w+):(\d+)")


class OpsClient:
    """Minimal OPS client: authenticated GETs with raw-response capture."""

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=30.0)
        self._token: str | None = None
        self._token_acquired_at: float = 0.0
        self._last_request_at: dict[str, float] = {}
        self._cool_down_until: float = 0.0
        self._data_dir = data_dir("ops")

    def close(self) -> None:
        """Close the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> OpsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _fresh_token(self) -> str:
        """Return a cached OAuth2 token, refreshing it when close to expiry."""
        now = time.monotonic()
        if self._token is None or now - self._token_acquired_at > TOKEN_LIFETIME_SECONDS:
            key, secret = ops_credentials()
            basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
            resp = self._client.post(
                TOKEN_URL,
                headers={"Authorization": f"Basic {basic}"},
                data={"grant_type": "client_credentials"},
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
            self._token_acquired_at = now
        return self._token

    def _wait_for_service(self, service: str) -> None:
        """Honor per-service minimum intervals and any active cool-down."""
        now = time.monotonic()
        wait = max(0.0, self._cool_down_until - now)
        last = self._last_request_at.get(service)
        if last is not None:
            interval = MIN_INTERVAL_SECONDS.get(service, 1.0)
            wait = max(wait, interval - (now - last))
        if wait > 0:
            time.sleep(wait)
        self._last_request_at[service] = time.monotonic()

    def _note_throttling(self, service: str, throttling: str) -> None:
        """Start a cool-down when the used service is not reported green."""
        for name, state, _limit in _THROTTLING_STATE_RE.findall(throttling):
            if name == service and state != "green":
                self._cool_down_until = time.monotonic() + COOL_DOWN_SECONDS

    def _save_raw(self, kind: str, ident: str, content: bytes) -> Path:
        """Save a raw response body as ``<timestamp>_<kind>_<ident>.xml``."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_ident = re.sub(r"[^A-Za-z0-9_.-]", "-", ident)[:60]
        path = self._data_dir / f"{stamp}_{kind}_{safe_ident}.xml"
        path.write_bytes(content)
        return path

    def _log_headers(self, kind: str, url: str, resp: httpx.Response) -> None:
        """Append one JSON line describing the response headers."""
        record = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "url": url,
            "status": resp.status_code,
            "throttling": resp.headers.get("X-Throttling-Control", ""),
        }
        log_path = self._data_dir / "headers.jsonl"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _get(self, path: str, *, service: str, kind: str, ident: str) -> tuple[bytes, Path]:
        """Authenticated GET; returns ``(body, saved_raw_path)``."""
        self._wait_for_service(service)
        url = f"{OPS_BASE}/rest-services/{path}"
        resp = self._client.get(url, headers={"Authorization": f"Bearer {self._fresh_token()}"})
        self._log_headers(kind, path, resp)
        self._note_throttling(service, resp.headers.get("X-Throttling-Control", ""))
        resp.raise_for_status()
        saved = self._save_raw(kind, ident, resp.content)
        return resp.content, saved

    # -- endpoints ---------------------------------------------------------

    def search(self, cql: str, *, begin: int = 1, end: int = 25) -> tuple[bytes, Path]:
        """Run a published-data CQL search; returns raw XML and its saved path."""
        ident = hashlib.sha1(cql.encode()).hexdigest()[:10]
        query = httpx.QueryParams({"q": cql, "Range": f"{begin}-{end}"})
        return self._get(
            f"published-data/search?{query}",
            service="search",
            kind="search",
            ident=ident,
        )

    def biblio(self, pub: str) -> tuple[bytes, Path]:
        """Fetch bibliographic data (docdb reference); returns raw XML and path."""
        ref = parse_pubnum(pub).docdb()
        return self._get(
            f"published-data/publication/docdb/{ref}/biblio",
            service="retrieval",
            kind="biblio",
            ident=ref,
        )

    def legal(self, pub: str) -> tuple[bytes, Path]:
        """Fetch INPADOC legal data (docdb reference); returns raw XML and path."""
        ref = parse_pubnum(pub).docdb()
        return self._get(
            f"legal/publication/docdb/{ref}",
            service="inpadoc",
            kind="legal",
            ident=ref,
        )

    def family(self, pub: str) -> tuple[bytes, Path]:
        """Fetch the simple patent family (docdb reference); returns raw XML and path."""
        ref = parse_pubnum(pub).docdb()
        return self._get(
            f"family/publication/docdb/{ref}",
            service="other",
            kind="family",
            ident=ref,
        )


def main(argv: list[str]) -> int:
    """Probe subcommand: fetch search/biblio/legal/family samples for one pub."""
    if len(argv) != 2 or argv[0] != "probe":
        print("usage: ops_client.py probe PUBLICATION_NUMBER", file=sys.stderr)
        return 2
    pub = argv[1]
    with OpsClient() as client:
        body, path = client.search("ta=computer", begin=1, end=5)
        print(f"search: {len(body)} bytes -> {path}")
        for name in ("biblio", "legal", "family"):
            body, path = getattr(client, name)(pub)
            print(f"{name}: {len(body)} bytes -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
