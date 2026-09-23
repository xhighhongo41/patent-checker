# Registering the server with each agent by hand

日本語版: [mcp-clients_ja.md](mcp-clients_ja.md)

`patent-checker install` writes these settings for you (see the README).
This page is for when you would rather do it yourself, need to fix a
registration, or use an agent the installer only prints a snippet for.

Every example uses the default URL `http://127.0.0.1:8642/mcp`. Replace
`${PATENT_CHECKER_SERVER_TOKEN}` with the token from `secrets/server_token.txt`
(or your `.env`), or export that variable before starting the agent where
the agent expands such references. The token is a shared secret: keep files
that hold the real value private (`chmod 600`) and never commit them.

| Agent | How the installer registers | Where it lands |
|---|---|---|
| Claude Code | `claude mcp add` | user settings, or `.mcp.json` with `--scope project` |
| Gemini CLI | `gemini mcp add` | `~/.gemini/settings.json` or `.gemini/settings.json` |
| GitHub Copilot CLI | `copilot mcp add` | `~/.copilot/mcp-config.json` or `.mcp.json` |
| Cursor | JSON merge | `~/.cursor/mcp.json` or `.cursor/mcp.json` |
| OpenCode | JSON merge | `~/.config/opencode/opencode.json` or `opencode.json` |
| Codex CLI | `codex mcp add` with `--token-env`, TOML append otherwise | `~/.codex/config.toml` |
| OpenHands | `openhands mcp add` (snippet to paste without the CLI) | managed by the OpenHands CLI (`~/.openhands/`) |
| Hermes Agent | snippet to paste | `~/.hermes/config.yaml` |
| Copilot coding agent | not registered (entered on GitHub.com) | repository settings |

The Gemini CLI, Copilot CLI and OpenHands commands follow their
documentation but have not been tried against the real tools.
`patent-checker uninstall` takes back what the installer wrote; the
sections below also say what to delete by hand.

## Claude Code

Run outside a session, then start a new one:

```sh
TOKEN="$(cat /path/to/secrets/server_token.txt)"
claude mcp add --transport http --scope user patent-checker http://127.0.0.1:8642/mcp \
  --header "Authorization: Bearer $TOKEN"
```

`claude mcp get patent-checker` shows what was registered; `claude mcp remove
patent-checker` removes it. Project scope (`--scope project`) writes
`.mcp.json` in the current directory:

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## Codex CLI

With the token in an environment variable (`bearer_token_env_var` names the
variable; Codex reads it when it starts):

```sh
codex mcp add patent-checker --url http://127.0.0.1:8642/mcp --bearer-token-env-var PATENT_CHECKER_SERVER_TOKEN
```

or in `~/.codex/config.toml` (project-level files are read only for trusted
projects):

```toml
[mcp_servers.patent-checker]
url = "http://127.0.0.1:8642/mcp"
bearer_token_env_var = "PATENT_CHECKER_SERVER_TOKEN"
```

To change the token later, edit that table (or re-run `codex mcp add`); the
installer does not rewrite an existing Codex entry.

## Cursor

`~/.cursor/mcp.json` (user) or `.cursor/mcp.json` (project):

```json
{"mcpServers": {"patent-checker": {"url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## Gemini CLI

```sh
gemini mcp add --transport http -s user patent-checker http://127.0.0.1:8642/mcp \
  -H "Authorization: Bearer $TOKEN"
```

or `~/.gemini/settings.json` (`.gemini/settings.json` for the project):

```json
{"mcpServers": {"patent-checker": {"httpUrl": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## GitHub Copilot CLI

```sh
copilot mcp add --transport http patent-checker http://127.0.0.1:8642/mcp
```

then add the header in `~/.copilot/mcp-config.json`. Copilot CLI does not
document environment-variable references in this file, so the value itself
goes in:

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer <paste your token>"}, "tools": ["*"]}}}
```

## OpenCode

`~/.config/opencode/opencode.json` (user; `$XDG_CONFIG_HOME/opencode/` when
that variable is set) or `opencode.json` (project). OpenCode documents only
the `~/.config` path; on Windows that is expected to be
`%USERPROFILE%\.config\opencode\`, which has not been verified. OpenCode
has no `mcp add` command, so the file is edited:

```json
{"mcp": {"patent-checker": {"type": "remote", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer {env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## OpenHands

The OpenHands CLI registers servers itself (it keeps them in
`~/.openhands/mcp.json`); this is what the installer runs when `openhands`
is on your PATH:

```sh
openhands mcp add patent-checker --transport http \
  --header "Authorization: Bearer ${PATENT_CHECKER_SERVER_TOKEN}" http://127.0.0.1:8642/mcp
openhands mcp list
```

Remove it with `openhands mcp remove patent-checker` if your OpenHands
version has that command, or delete the entry from `~/.openhands/mcp.json`.

The full OpenHands application (web UI or Docker) reads `config.toml` in the
working directory instead. Its documentation does not say which header
`api_key` is sent in, so this is not guaranteed to reach the server as a
bearer token; if the first call is refused with 401, use the CLI form above.

```toml
[mcp]
shttp_servers = [{ url = "http://127.0.0.1:8642/mcp", api_key = "<paste your token>" }]
```

## Hermes Agent

`~/.hermes/config.yaml`. Hermes expands `${VAR}` in header values, so the
file can reference the variable instead of holding the token:

```yaml
mcp_servers:
  patent-checker:
    url: http://127.0.0.1:8642/mcp
    headers:
      Authorization: "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"
```

## GitHub Copilot coding agent

The coding agent runs on GitHub.com and reaches your server only if it is
published on the internet with TLS (see [deploy-lan.md](deploy-lan.md); a
tailnet or VPN address is not reachable from GitHub). Its MCP configuration
is entered in the repository's settings on GitHub.com (Settings → Copilot →
Coding agent); there is no file or API the installer could write it to.
Store the token as a Copilot environment secret named with the
`COPILOT_MCP_` prefix and reference it:

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "https://patents.example.com/mcp",
  "headers": {"Authorization": "Bearer $COPILOT_MCP_PATENT_CHECKER_TOKEN"}, "tools": ["*"]}}}
```

A custom agent defined in the repository (`.github/agents/<name>.md`) can
also carry MCP servers in its front matter under `mcp-servers`, with the
same keys; that configuration applies to that custom agent only.

## stdio instead of HTTP

`patent-checker serve --transport stdio` speaks MCP on standard input and
output, with no bearer token: the agent starts the server as a child process
that runs with your own permissions, which is why HTTP is the default. To
register it, use each agent's stdio form, for example:

```sh
claude mcp add --transport stdio patent-checker -- patent-checker serve --transport stdio
```

```json
{"mcpServers": {"patent-checker": {"command": "patent-checker",
  "args": ["serve", "--transport", "stdio"]}}}
```

The operator notice still applies: set `PATENT_CHECKER_OPERATOR_CONSENT` (in
the environment or in a `.env` in the project) or the server refuses to
start.

## The Skill by hand

The Skill is a directory with a `SKILL.md`. Copying the
`patent-checker` directory that ships inside the Python package
(`patent-checker install --list-agents` prints where it is) or the
`skills/patent-checker/` directory of this repository into your agent's
skills directory is all a manual Skill installation takes:
`~/.agents/skills/patent-checker/` is read by Codex CLI, OpenCode, Cursor,
Gemini CLI and Copilot CLI; Claude Code reads `~/.claude/skills/`, OpenHands
`~/.openhands/skills/`, Hermes `~/.hermes/skills/`. For a project, use
`.agents/skills/patent-checker/` (and `.claude/skills/` for Claude Code).
