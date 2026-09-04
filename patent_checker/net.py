"""Outbound network guard for patent-checker.

Provides :class:`AllowlistTransport`, an ``httpx`` transport wrapper that
refuses to contact any host outside the allowlist before any connection is
attempted. Because every hop of a redirect chain is routed back through the
transport, this also blocks redirects to hosts outside the allowlist.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from patent_checker.config import ALLOWED_HOSTS


class HostNotAllowedError(httpx.TransportError):
    """Raised before any connection when a request targets a host outside the allowlist."""


class AllowlistTransport(httpx.BaseTransport):
    """An ``httpx`` transport that only forwards requests to allowlisted hosts."""

    def __init__(
        self, inner: httpx.BaseTransport, allowed_hosts: Iterable[str] = ALLOWED_HOSTS
    ) -> None:
        """Wrap ``inner`` so it is only reachable for allowlisted hosts.

        Args:
            inner: The transport that actually performs the request once the
                target host has been approved.
            allowed_hosts: Hosts permitted to be contacted. Compared
                case-insensitively; defaults to :data:`patent_checker.config.ALLOWED_HOSTS`.
        """
        self._inner = inner
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Forward ``request`` to the inner transport, or refuse it.

        Raises:
            HostNotAllowedError: If the request's host is empty or not in
                ``self.allowed_hosts``. The inner transport is never called
                in this case, so no connection is attempted.
        """
        host = request.url.host.lower()
        if not host or host not in self.allowed_hosts:
            raise HostNotAllowedError(f"host not allowed: {host!r}", request=request)
        return self._inner.handle_request(request)

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()

    def __enter__(self) -> AllowlistTransport:
        """Enter the inner transport's context and return this wrapper."""
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Exit the inner transport's context."""
        self._inner.__exit__(*exc_info)
