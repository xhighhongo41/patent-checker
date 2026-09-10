"""Tests for :mod:`patent_checker.net`."""

from __future__ import annotations

import httpx
import pytest

from patent_checker import config
from patent_checker.net import AllowlistTransport, HostNotAllowedError, allowlist_transport


class RecordingTransport(httpx.MockTransport):
    """A :class:`httpx.MockTransport` that records requests and tracks closing."""

    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []
        self.closed = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return super().handle_request(request)

    def close(self) -> None:
        self.closed = True
        super().close()


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def test_allowed_host_reaches_inner_transport() -> None:
    """A request to an allowlisted host is forwarded to the inner transport."""
    inner = RecordingTransport(_ok_handler)
    transport = AllowlistTransport(inner)

    with httpx.Client(transport=transport) as client:
        response = client.get("https://ops.epo.org/x")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(inner.requests) == 1


def test_disallowed_host_is_refused_without_calling_inner() -> None:
    """A request to a host outside the allowlist never reaches the inner transport."""
    inner = RecordingTransport(_ok_handler)
    transport = AllowlistTransport(inner)

    with httpx.Client(transport=transport) as client, pytest.raises(HostNotAllowedError):
        client.get("https://example.com/")

    assert inner.requests == []
    assert issubclass(HostNotAllowedError, httpx.TransportError)
    assert issubclass(HostNotAllowedError, httpx.HTTPError)


def test_redirect_to_disallowed_host_is_blocked() -> None:
    """Following a redirect to a disallowed host is blocked on the second hop."""

    def redirecting_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    inner = RecordingTransport(redirecting_handler)
    transport = AllowlistTransport(inner)

    with (
        httpx.Client(transport=transport, follow_redirects=True) as client,
        pytest.raises(HostNotAllowedError),
    ):
        client.get("https://patents.google.com/patent/X")

    assert len(inner.requests) == 1


def test_host_matching_is_case_insensitive() -> None:
    """Host comparison against the allowlist ignores case, on both sides."""
    inner = RecordingTransport(_ok_handler)
    transport = AllowlistTransport(inner)

    with httpx.Client(transport=transport) as client:
        response = client.get("https://OPS.EPO.ORG/")

    assert response.status_code == 200

    custom_inner = RecordingTransport(_ok_handler)
    custom_transport = AllowlistTransport(custom_inner, allowed_hosts=["Patents.Google.com"])

    with httpx.Client(transport=custom_transport) as client:
        response = client.get("https://patents.google.com/")

    assert response.status_code == 200


def test_default_allowlist_matches_config() -> None:
    """The default allowlist is exactly ``config.ALLOWED_HOSTS``."""
    inner = RecordingTransport(_ok_handler)
    transport = AllowlistTransport(inner)

    assert transport.allowed_hosts == config.ALLOWED_HOSTS


def test_custom_allowlist_restricts_access() -> None:
    """A custom allowlist refuses hosts that are not part of it."""
    inner = RecordingTransport(_ok_handler)
    transport = AllowlistTransport(inner, allowed_hosts=["example.org"])

    with httpx.Client(transport=transport) as client, pytest.raises(HostNotAllowedError):
        client.get("https://ops.epo.org/")

    assert inner.requests == []


def test_close_delegates_to_inner_transport() -> None:
    """Closing the wrapper transport also closes the inner transport."""
    inner = RecordingTransport(_ok_handler)
    transport = AllowlistTransport(inner)

    transport.close()

    assert inner.closed is True


def test_allowlist_transport_factory_wraps_a_real_transport() -> None:
    """The factory hands back an allowlisted wrapper around a real HTTP transport."""
    transport = allowlist_transport()

    assert isinstance(transport, AllowlistTransport)
    assert isinstance(transport._inner, httpx.HTTPTransport)
    assert transport.allowed_hosts == config.ALLOWED_HOSTS

    # No connection is attempted for a host outside the allowlist, so this
    # stays network-free.
    with httpx.Client(transport=transport) as client, pytest.raises(HostNotAllowedError):
        client.get("https://evil.example/")
