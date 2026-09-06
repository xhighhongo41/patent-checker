# Patent Checker

日本語版: [README_ja.md](README_ja.md)

Patent Checker helps you explore published patents that may relate to your
own software project, and to write down what you found as a dated,
procedure-style report. It is built from two parts:

- an **MCP server** that fetches public patent data (EPO Open Patent
  Services and Google Patents) deterministically: searches, bibliographic
  records, claims, legal status, patent families, plus offline helpers, and
- an **Agent Skill** that drives your AI coding agent (Claude Code, Codex
  CLI, Cursor, Gemini CLI, GitHub Copilot CLI, OpenCode, OpenHands, Hermes
  Agent) through the judgment work: reading your codebase, translating its
  features into patent vocabulary, screening candidates and writing the
  report.

Patent Checker **does not decide whether anything infringes a patent**. Its
reports contain observations, scope statements and open questions, never a
verdict.

**Status: pre-release (v0.5, deployment and first public release).**

## Important notices

Please read these before you install anything. The tool asks you to
acknowledge them once, and records that you did.

- **What you learn here can be used against you.** A report documents that
  you knew of specific patents on specific dates. In some jurisdictions,
  notably the United States, knowledge of a patent can support a claim of
  willful infringement and increased damages. Dated records of how you
  responded can also work in your favour. Patent Checker lets you choose
  where its records live and whether they are tracked in version control:
  make that choice deliberately.
- **Search results are incomplete.** No patent search is exhaustive.
  Coverage varies by country, language, publication stage and data source,
  and recent applications may be missing entirely. The absence of a document
  from a report is never evidence that no relevant patent exists.
- **This is a search aid, not advice.** Patent Checker does not replace a
  qualified patent attorney or agent, and its authors accept no
  responsibility for decisions made, or not made, on the basis of its
  output. The final judgment about your project, and any response to it,
  is yours.

Three notices are shown at the points where they matter: the **Skill**
shows the notice above to whoever runs an exploration and records their
consent locally (the record is what the workflow checks first); the
**server** refuses to start until its operator has read a separate
operator notice about data-source terms, credentials and what the server
stores; and the **installer** shows the user notice up front so that the
Skill finds the consent already recorded.

## How it works

```
your agent ──(Skill: judgment)──► patent-checker MCP server ──► EPO OPS
   │                                   │  deterministic fetch,      Google Patents
   │  reads your code, writes           │  normalisation, cache
   ▼  the report                        ▼
.patent-checker/reports/…        per-user document cache
```

1. The Skill reads your codebase, lists the technical features that could be
   claimed, translates them into patent vocabulary and builds search
   queries.
2. The MCP server runs the queries against EPO OPS, fetches the candidate
   documents (claims from Google Patents, bibliography and legal status
   from OPS) and caches every document so nothing is fetched twice.
3. The Skill screens the candidates in stages, maps claim elements to your
   features and writes a report: what was searched, what was found, how
   each candidate relates to your code, and what was *not* covered.

The server only ever receives search expressions and publication numbers.
Your source code and project description never leave your machine: the
analysis happens inside your agent.

## Prerequisites

- **An AI coding agent** that supports Agent Skills and MCP: Claude Code,
  OpenAI Codex CLI, Cursor, Gemini CLI, GitHub Copilot CLI, OpenCode,
  OpenHands or Hermes Agent.
- **Python 3.12 or newer and [uv](https://docs.astral.sh/uv/)** for the
  command-line tool (`pipx` or a plain `pip install` in a virtual
  environment work too).
- **Docker with Compose v2**, if you run the server in a container
  (recommended). Without Docker the server runs as an ordinary process.
- **An EPO Open Patent Services account** (recommended). Register at
  <https://developers.epo.org/>, then create an app to obtain a consumer
  key and secret. **Apply early: approval is manual and typically takes
  about a business day.** Without OPS credentials the server starts in
  *degraded mode*: it can still fetch claims from Google Patents for
  publication numbers your agent finds by other means, but searches,
  bibliography, families and legal status are unavailable, and the
  coverage of an exploration drops accordingly.

## Install the server

Both routes need the operator notice acknowledged and a bearer token. Keep
the token: your MCP client needs the same value.

### Option A: Docker Compose (recommended)

```sh
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/compose.yaml
mkdir secrets

# EPO OPS credentials (or leave both files empty for degraded mode)
printf '%s' 'YOUR_OPS_CONSUMER_KEY'    > secrets/ops_key.txt
printf '%s' 'YOUR_OPS_CONSUMER_SECRET' > secrets/ops_secret.txt

# The bearer token every MCP client must present
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/server_token.txt

# Read the operator notice (add --lang ja for Japanese), then acknowledge it
docker compose run --rm patent-checker serve --show-operator-notice
echo 'PATENT_CHECKER_OPERATOR_CONSENT=1.0' >> .env

docker compose up -d
curl -fsS http://127.0.0.1:8642/health
```

The image is published as `ghcr.io/xhighhongo41/patent-checker` and
`docker.io/xhighhongo41/patent-checker` with tags `X.Y.Z`, `X.Y` and
`latest`; `compose.yaml` follows the `X.Y` tag of the release it ships
with. The container binds `0.0.0.0` internally, but Compose publishes the
port on **your machine's loopback only** (`127.0.0.1:8642`); the server
accepts the `localhost` and `127.0.0.1` Host headers and nothing else.
Fetched documents, search results and the request log live in the named
volume `patent-checker-data`.

The secret files are read once at start; empty OPS files mean "not
configured" and put the server in degraded mode. `secrets/README.md`
explains file permissions and the Windows (PowerShell) equivalents.

### Option B: without Docker

```sh
uv tool install patent-checker          # or: pipx install patent-checker
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/.env.example
cp .env.example .env                    # fill in the OPS key/secret and a token
patent-checker serve --show-operator-notice   # add --lang ja for Japanese
# then set PATENT_CHECKER_OPERATOR_CONSENT=1.0 in .env
patent-checker serve
```

`patent-checker serve` speaks Streamable HTTP on `http://127.0.0.1:8642/mcp`
and requires the bearer token from `PATENT_CHECKER_SERVER_TOKEN` (or
`PATENT_CHECKER_SERVER_TOKEN_FILE`). The `.env` file is read from the
directory you start the server in (or a parent of it); variables already
in the environment win. Data goes to the per-user data directory
(`~/.local/share/patent-checker` on Linux and macOS,
`%LOCALAPPDATA%\patent-checker` on Windows).

`--transport stdio` runs the server without a network port and without
authentication, for a single local client only; it then runs with your
user's permissions, which is why HTTP is the default.

## Install the Skill and connect your agent

One command installs the command-line tool, shows the user notice, copies
the Skill into the places your agents read, and registers the MCP server
with them:

```sh
curl -LsSf https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.ps1 | iex"
```

Prefer to read the script first? Fetch it with `| more` instead of `| sh`, or
skip it entirely: the script only installs uv and the tool, and then tells
you to run the installer yourself.

```sh
uv tool install patent-checker
patent-checker install
```

`patent-checker install`:

1. shows the user notice and asks for your agreement (recorded in
   `~/.config/patent-checker/consent.json` with the date and notice
   version; you are asked again only when the notice changes);
2. copies the Skill into `~/.agents/skills/patent-checker/` (read by Codex
   CLI, OpenCode, Cursor, Gemini CLI and Copilot CLI) and into the private
   directories of the agents that need one (`~/.claude/skills/`,
   `~/.openhands/skills/`, `~/.hermes/skills/`);
3. registers the server (`http://127.0.0.1:8642/mcp` unless you pass
   `--url`) with each detected agent, using the agent's own `mcp add`
   command where one exists (Claude Code, Gemini CLI, Copilot CLI), editing
   the agent's JSON or TOML configuration otherwise (Cursor, OpenCode,
   Codex CLI), and printing a snippet to paste for the agents whose
   configuration it will not touch (OpenHands, Hermes Agent).

Useful options: `--list-agents` (what would be installed where),
`--dry-run` (do everything except write), `--agent claude-code --agent cursor`
(instead of auto-detection; `--agent all` for every supported agent),
`--scope project` (install into the current project instead of your home
directory), `--lang ja` (Japanese notice), `--no-skill` / `--no-mcp`.

### Passing the bearer token without exposing it

The installer never takes the token as a command-line argument, so it does
not land in your shell history. Give it one of:

```sh
patent-checker install --token-file /path/to/secrets/server_token.txt
# or
export PATENT_CHECKER_SERVER_TOKEN="$(cat /path/to/secrets/server_token.txt)"
patent-checker install
# or just run it: the installer prompts for the token without echoing it
```

By default the token value is written into each agent's own configuration
file (created with owner-only permissions where the platform supports
them). With `--token-env` the installer writes a *reference* to the
`PATENT_CHECKER_SERVER_TOKEN` environment variable instead, in the notation
each agent expands, so the file never holds the value; you then have to
export that variable before starting the agent. Copilot CLI and OpenHands
do not document such references, so `--token-env` prints a snippet for
them instead. When an agent's own CLI is used for registration, the token
appears in that process's arguments for the duration of the call.

Registration commands and snippets that the installer prints never contain
the token; they use `${PATENT_CHECKER_SERVER_TOKEN}` or a placeholder.

### If the server answers 401

Check that the token your agent sends is exactly the one the server was
started with (Claude Code: `claude mcp get patent-checker` shows the
registered header; make sure a placeholder such as `<token>` did not get
registered verbatim). A server that is not running produces a connection
refused error, not a 401, so the two are easy to tell apart.

### Registering by hand

<details>
<summary>Per-agent configuration (what the installer writes)</summary>

Replace `${PATENT_CHECKER_SERVER_TOKEN}` with the token, or export the
variable where the agent expands references.

**Claude Code** (run outside a session, then start a new one):

```sh
TOKEN="$(cat /path/to/secrets/server_token.txt)"
claude mcp add --transport http --scope user patent-checker http://127.0.0.1:8642/mcp \
  --header "Authorization: Bearer $TOKEN"
```

Project scope writes `.mcp.json`:

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**Codex CLI** (`~/.codex/config.toml`; project files are read only for
trusted projects):

```toml
[mcp_servers.patent-checker]
url = "http://127.0.0.1:8642/mcp"
bearer_token_env_var = "PATENT_CHECKER_SERVER_TOKEN"
```

**Cursor** (`~/.cursor/mcp.json` or `.cursor/mcp.json`):

```json
{"mcpServers": {"patent-checker": {"url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**Gemini CLI** (`gemini mcp add --transport http patent-checker http://127.0.0.1:8642/mcp --header "Authorization: Bearer $TOKEN"`,
or `~/.gemini/settings.json`):

```json
{"mcpServers": {"patent-checker": {"httpUrl": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**GitHub Copilot CLI** (`copilot mcp add --transport http patent-checker http://127.0.0.1:8642/mcp --header "Authorization: Bearer $TOKEN"`,
or `~/.copilot/mcp-config.json`):

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer <paste your token>"}}}}
```

**OpenCode** (`~/.config/opencode/opencode.json` or `opencode.json`):

```json
{"mcp": {"patent-checker": {"type": "remote", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer {env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**OpenHands** (`config.toml` in the working directory; whether `api_key`
is sent as a bearer token is not documented, so verify with a first call):

```toml
[mcp]
shttp_servers = [{ url = "http://127.0.0.1:8642/mcp", api_key = "<paste your token>" }]
```

**Hermes Agent** (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  patent-checker:
    url: http://127.0.0.1:8642/mcp
    headers:
      Authorization: "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"
```

The Skill itself is a directory with a `SKILL.md`; copying
`skills/patent-checker/` from this repository into your agent's skills
directory is all a manual Skill installation takes.

</details>

### Phone-only and cloud agents

Commit the Skill into your repository as `.agents/skills/patent-checker/`
(`patent-checker install --scope project`, then add the directory to git),
and run the server somewhere the cloud agent can reach over HTTPS: bind it
beyond loopback, set `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` to the host name
clients will use, and terminate TLS in a reverse proxy in front of it. The
consent record is per user; a cloud agent whose home directory is reset
records it again on the next run, or use `patent-checker consent record
--project` to keep it with the project.

## Usage

Ask your agent for a prior-art exploration of the project it is working in:

> Use the patent-checker skill to explore prior patents related to this
> project.

The Skill first checks the consent record, asks (once per project) whether
`.patent-checker/` should be added to `.gitignore`, then works through the
steps above and writes the report to
`.patent-checker/reports/report-<target>-<YYYYMMDD-HHMM>.md`. Reports are
never overwritten; a later run of the same project produces a new, dated
file. Keep the server running for the whole session.

**Scale and cost.** One exploration of a medium-sized project involves
reading your code, one or more search rounds, and a staged screening of a
few dozen candidate documents. Expect it to consume on the order of a
million tokens of agent traffic, most of it in screening; the Skill hands
the first screening stage to smaller models where your agent supports
delegation. One run is not exhaustive: repeated runs, different query
vocabularies and a professional search will each find things a single run
does not.

## Configuration

Environment variables read by the server (`patent-checker serve`) and,
where noted, by the CLI. See `.env.example` for the same list with
comments.

| Variable | Default | Meaning |
|---|---|---|
| `PATENT_CHECKER_OPS_KEY`, `PATENT_CHECKER_OPS_SECRET` | unset | EPO OPS consumer key and secret. Either the value, or the path to a file holding it via the `_FILE` variants (`PATENT_CHECKER_OPS_KEY_FILE`, …); never both. Empty files mean "not configured" (degraded mode). |
| `PATENT_CHECKER_OPERATOR_CONSENT` | unset (required) | Version of the operator notice you acknowledged (`1.0`). The server refuses to start otherwise. |
| `PATENT_CHECKER_SERVER_TOKEN` (`_FILE`) | unset (required for http) | Bearer token clients must present. |
| `PATENT_CHECKER_SERVER_HOST`, `PATENT_CHECKER_SERVER_PORT` | `127.0.0.1`, `8642` | Bind address. Binding beyond loopback additionally requires `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` (comma-separated host names clients will use) and TLS in front. |
| `PATENT_CHECKER_SERVER_RPS`, `PATENT_CHECKER_SERVER_BURST` | `5`, `10` | Limit on incoming MCP requests (a guard against runaway agent loops; upstream pacing is separate). |
| `PATENT_CHECKER_DATA_DIR` | per-user data directory (server), `./.patent-checker` (CLI) | Search cache, request log and, when set, the shared document cache (`<dir>/cache`). The container image sets it to `/data`. |
| `PATENT_CHECKER_CACHE_DIR` | `<per-user data dir>/cache` | Shared document cache (bibliography, claims, legal status, families, Google Patents pages), reused across projects by the CLI and the server alike. |
| `PATENT_CHECKER_CACHE_TTL` | `biblio=90d,claims=0,legal=7d,family=30d,gp=0,search=1d,searchbib=1d` | Per-kind expiry overrides (`<n>d`, `<n>h`, `0` = never). |
| `PATENT_CHECKER_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. All server logs go to standard error. |

With Compose, the credentials come from the files under `secrets/`, and
the `.env` next to `compose.yaml` provides `PATENT_CHECKER_OPERATOR_CONSENT`
(plus, optionally, `PATENT_CHECKER_IMAGE`, `PATENT_CHECKER_PORT` and
`PATENT_CHECKER_LOG_LEVEL`). Other variables can be added under
`environment:` in `compose.yaml`.

<details>
<summary>Sharing the document cache between the container and the CLI (advanced)</summary>

The CLI on your machine and the server in the container keep separate
document caches. To share one, point both at the same directory: add
`PATENT_CHECKER_CACHE_DIR: /cache` under `environment:` and bind-mount your
cache directory, for example `~/.local/share/patent-checker/cache:/cache`,
under `volumes:`. The container writes as UID 1000; if that is not your
user, the mounted directory must be writable by it.

</details>

## Cache and clean-up

Fetched patent documents are kept once, in the per-user shared cache, with
a per-kind expiry; search results and the request log stay with whoever ran
them (the server's data directory, or the project's `.patent-checker/`
when the CLI is used from the project).

- `patent-checker cache status` shows what is cached, where, and how much of
  it has expired or is broken.
- `patent-checker cache clear [--kind …] [--older-than DAYS] [--pub …] [--expired] [--broken]`
  lists what it would delete; add `--yes` to delete.
- `patent-checker clean` lists the project's traces (search cache, request
  log, leftovers of older layouts) and, with `--yes`, deletes them. Reports,
  your agent's exploration notes and the consent record are kept unless
  you add `--include-artifacts` or `--include-consent`; `--shared` extends
  the clean-up to the shared cache and the server's data directory.
- With Compose, `docker compose down -v` removes the server's volume,
  cache and all.

Nothing is deleted without `--yes`.

## Security model

- The server binds the loopback interface by default and requires a bearer
  token on every request. Host and Origin headers are validated, which is
  what stops a browser page or a DNS-rebinding attempt from reaching a
  loopback server.
- It only ever connects to `ops.epo.org` and `patents.google.com`, and only
  writes below its own data directory. It has no code-execution tools.
- It receives search expressions and publication numbers, nothing else.
  Your code and documents are analysed by your agent, on your machine.
- Incoming requests are rate-limited, and upstream requests are paced and
  cached so that a runaway agent cannot hammer the data sources.
- `GET /health` is unauthenticated for container health checks and returns
  only a status and the version.
- Credentials are read from the environment or from files, are never
  logged, and never appear in the startup banner or in error messages.
- `--transport stdio` has no authentication: use it only for a single local
  client, aware that the server then runs with your permissions.
- Serving a LAN or the internet is possible but is your responsibility:
  the transport is plain HTTP, so put a TLS-terminating reverse proxy in
  front, set `PATENT_CHECKER_SERVER_ALLOWED_HOSTS`, and treat the token as
  the only thing between the network and your OPS quota.
- The Skill treats everything the server returns, patent text included, as
  data to analyse, not as instructions to follow.

## Data sources and fair use

- **EPO Open Patent Services** is used under the terms attached to your
  registered application, including its weekly fair-use quota. The server
  spaces its requests and honours OPS throttling headers, but it cannot know
  how many other clients share your credentials. Legal-status information
  comes from OPS and is the authoritative value in reports.
- **Google Patents** is fetched one document page at a time, at a pace and
  volume comparable to a person reading in a browser, and every page is
  cached so it is not fetched twice. The search endpoint is not used. Keep
  it that way: Google's `robots.txt` permitting a path is not the same as
  its terms of service permitting bulk collection.
- Patent documents are copyrighted by their applicants. Reports quote only
  the claim passages they discuss, with the source, and never redistribute
  full texts; the cache stays on your machine and is not part of any
  report.

## Notes

- **US publication numbers changed length in 2026.** EPO's DOCDB
  representation of US pre-grant publications is 10 digits up to 2025
  (`US2007016547A1`) and 11 digits from 2026 (`US20260024003A1`). Google
  Patents spells all years with 11 digits. The tools normalise both spellings;
  when you type a number by hand, either form is accepted.
- The user notice (version 1) and the operator notice (version 1.0) carry
  their own version numbers. When one changes, the Skill asks for consent
  again, or the server asks for a fresh acknowledgement.

## Updating

```sh
uv tool upgrade patent-checker && patent-checker install   # tool and Skill
docker compose pull && docker compose up -d                # containerised server
```

`patent-checker install` can be re-run at any time: it refreshes the Skill
copies and updates the server entry in each agent's configuration in place.

## Changelog

- **v0.5** (2026-09): first public release. Docker image (GHCR, Docker Hub) and
  Compose deployment with file-based secrets, `patent-checker install` for
  eight agents, bootstrap scripts, PyPI package, `/health`, log level
  setting, `.env` looked up from the working directory, this README.
- **v0.4** (2026-09-05): per-user shared document cache with per-kind expiry
  and self-repair, `cache status` / `cache clear` / `clean`, publication
  number normalisation for the 2026 change in US numbers.
- **v0.3** (2026-09-04): MCP server (FastMCP 4, Streamable HTTP, bearer
  token, Host/Origin validation, outbound allowlist, rate limits), operator
  notice, first file cache.
- **v0.2** (2026-08-29): core library and CLI, the Agent Skill with its
  consent gate and report template, end-to-end validation on two projects.
- **v0.1** (2026-08-29): proof of concept run by hand; data-source and
  legal-risk study.

## License

Apache-2.0. See [LICENSE](LICENSE).

Questions and bug reports: [GitHub Issues](https://github.com/xhighhongo41/patent-checker/issues).
Patent Checker is provided as is, without warranty of any kind; it does
not determine infringement and is not legal advice.
