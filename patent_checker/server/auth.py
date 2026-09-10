"""Bearer-token verification for the http transport.

patent-checker authenticates a single local operator, not a multi-tenant
OAuth client population: one static token, configured through
``PATENT_CHECKER_SERVER_TOKEN`` (or its ``_FILE`` variant), guards the whole
tool surface. FastMCP ships ``StaticTokenVerifier`` for that shape, but it
looks the presented token up in a dictionary, which compares strings in
non-constant time and keeps the token reachable through the object. This
module replaces it with a verifier that compares the presented token with
:func:`hmac.compare_digest` and never exposes the configured value.
"""

from __future__ import annotations

from hmac import compare_digest

from fastmcp.server.auth import AccessToken, TokenVerifier


class ConstantTimeTokenVerifier(TokenVerifier):
    """Verify the one configured bearer token in constant time.

    Attributes:
        client_id: The client id recorded on every accepted token.
    """

    def __init__(self, token: str, *, client_id: str) -> None:
        """Store the expected token.

        Args:
            token: The bearer token clients must present.
            client_id: The client id to record on accepted tokens.

        Raises:
            ValueError: If *token* is empty. An empty expected token would
                accept an empty ``Authorization`` header.
        """
        super().__init__()
        if not token:
            raise ValueError("the bearer token must not be empty")
        self.client_id = client_id
        # Kept as UTF-8 bytes: compare_digest refuses str arguments that are
        # not ASCII-only, and a client is free to send any header bytes.
        self._expected = token.encode("utf-8")

    def __repr__(self) -> str:
        """Describe the verifier without revealing the configured token."""
        return f"{type(self).__name__}(client_id={self.client_id!r})"

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return the access info for *token*, or ``None`` if it is not the configured one.

        The comparison is the same for a wrong token of the right length and
        for one of a different length, so a caller learns nothing from how
        long the answer takes beyond the token's length.

        Args:
            token: The bearer token presented by the client.

        Returns:
            An :class:`~fastmcp.server.auth.AccessToken` with the configured
            client id and no scopes, or ``None`` for any other token.
        """
        if not compare_digest(token.encode("utf-8"), self._expected):
            return None
        return AccessToken(token=token, client_id=self.client_id, scopes=[])
