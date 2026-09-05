# Patent Checker

Search published patents related to your own software project and generate a
survey report (best effort). This tool does **not** determine infringement and
is not a substitute for professional legal advice.

**Status: pre-release (v0.4, cache management and clean-up).** This version
provides the core library, the `patent-checker` CLI (EPO OPS + Google Patents
access, screening utilities, consent gate, `cache status` / `cache clear` /
`clean`), an MCP server exposing the same operations as tools (with a
per-user shared document cache, per-kind expiry and self-repair), and the
Agent Skill under `skills/`. Packaged distribution and installers are planned for later
versions; full installation instructions will follow with the first public
release.

## Running the MCP server (v0.4, from a checkout)

1. `uv sync`, then copy `.env.example` to `.env` and fill in the EPO OPS
   credentials (`PATENT_CHECKER_OPS_KEY` / `_SECRET`). Without them the
   server still starts in degraded mode (Google Patents claims and offline
   helpers only).
2. Read the operator notice and acknowledge it:
   `uv run patent-checker serve --show-operator-notice` (add `--lang ja` for
   Japanese), then set `PATENT_CHECKER_OPERATOR_CONSENT=1.0` in `.env`.
3. Generate a bearer token and set `PATENT_CHECKER_SERVER_TOKEN` in `.env`:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. Start the server: `uv run patent-checker serve` (Streamable HTTP on
   `http://127.0.0.1:8642/mcp`, bearer token required). `--transport stdio`
   runs it without authentication for a single local client;
   `--host`/`--port` change the bind address (binding beyond loopback
   additionally requires `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` and TLS from a
   reverse proxy).
5. Register it with your MCP client, e.g. for Claude Code (run outside a
   session, then start a new one):
   `claude mcp add --transport http patent-checker http://127.0.0.1:8642/mcp --header "Authorization: Bearer <token>"`.

The server's raw responses and cache live under the per-user data directory
(`$XDG_DATA_HOME/patent-checker`, Windows `%LOCALAPPDATA%\patent-checker`;
override with `PATENT_CHECKER_DATA_DIR`). The CLI keeps using
`./.patent-checker` and remains available as a fallback for every tool.
