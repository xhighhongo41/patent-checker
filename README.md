# Patent Checker

<!-- mcp-name: io.github.xhighhongo41/patent-checker -->

日本語版: [README_ja.md](README_ja.md)

[![PyPI](https://img.shields.io/pypi/v/patent-checker)](https://pypi.org/project/patent-checker/)
[![CI](https://github.com/xhighhongo41/patent-checker/actions/workflows/ci.yml/badge.svg)](https://github.com/xhighhongo41/patent-checker/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/docker/pulls/xhighhongo41/patent-checker)](https://hub.docker.com/r/xhighhongo41/patent-checker)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

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

**Status: stable release (v1.0).**

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

There are two notices. The **user notice** (the three points above) is
shown by the Skill to whoever runs an exploration, and by the installer up
front; your agreement is recorded locally and checked before every run. The
**operator notice** is for whoever starts the server: it covers data-source
terms, credentials, and what the server stores, and the server refuses to
start until it has been acknowledged.

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
  command-line tool and installer (`pipx` or a plain `pip install` in a
  virtual environment work too). Python is also the easiest way to generate
  the bearer token below.
- **Docker with Compose v2**, if you run the server in a container
  (recommended). Without Docker the server runs as an ordinary process.
- **An EPO Open Patent Services account** (recommended). Register at
  <https://developers.epo.org/>, then create an app to obtain a consumer
  key and secret. **Apply early: approval is manual and typically takes
  about a business day.** Without it the server runs in
  [degraded mode](#without-an-epo-ops-account-degraded-mode).

## The bearer token

The server listens on HTTP, and every MCP client must present a **bearer
token** on every request: a secret string that only your server and your
own agents know. It is what stops any other program on the machine, or on
the network if you ever expose the port, from using your server and your
EPO OPS quota. There is no account or sign-up behind it; you make the token
yourself, once, and give the same value to the server and to the installer.

Generate one (any 32 random bytes will do):

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"      # macOS, Linux
```

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"       # Windows
```

Where it goes:

- **Docker Compose**: into the file `secrets/server_token.txt` next to
  `compose.yaml`.
- **Without Docker**: into `.env` as `PATENT_CHECKER_SERVER_TOKEN=…` (or a
  file named by `PATENT_CHECKER_SERVER_TOKEN_FILE`).
- **Your agents**: `patent-checker install` asks for it (or reads it from
  `--token-file`), and writes it into each agent's MCP configuration.

Without a token the HTTP server does not start. The one exception is
`patent-checker serve --transport stdio`, which has no token because the
agent starts the server as a child process running with your own
permissions; that is a special case for a single local client, not the
default. Treat the token like a password: keep the files that hold it
private, and rotate it by writing a new value and re-running
`patent-checker install`.

## Without an EPO OPS account (degraded mode)

If you leave the OPS credentials unset (empty `secrets/ops_key.txt` and
`ops_secret.txt`, or no `PATENT_CHECKER_OPS_KEY` in `.env`), the server
starts in **degraded mode** and says so at start-up and in `server_status`.

- Still works: fetching the claims of a document from Google Patents when
  your agent already knows its publication number, publication-number
  normalisation, the offline helpers (deduplication, batch verification).
- Unavailable: patent searches, bibliographic records, legal status and
  patent families. The Skill switches to its degraded procedure, in which
  your agent finds candidate publication numbers through its own web
  search; coverage drops, and the report says so.

Degraded mode is meant for a first look. For a real exploration, get an OPS
account and restart the server with the credentials in place.

## Install the server

Both routes need the operator notice acknowledged and a bearer token.

### Option A: Docker Compose (recommended)

```sh
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/compose.yaml
mkdir secrets

# 1. EPO OPS credentials (or leave both files empty for degraded mode)
printf '%s' 'YOUR_OPS_CONSUMER_KEY'    > secrets/ops_key.txt
printf '%s' 'YOUR_OPS_CONSUMER_SECRET' > secrets/ops_secret.txt

# 2. The bearer token (see above)
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/server_token.txt
chmod 600 secrets/*.txt

# 3. Read the operator notice (add --lang ja for Japanese), then acknowledge it
docker compose run --rm patent-checker serve --show-operator-notice
echo 'PATENT_CHECKER_OPERATOR_CONSENT=1.0' >> .env

# 4. Start
docker compose up -d
curl -fsS http://127.0.0.1:8642/health
```

On Windows, create the three files with PowerShell instead of `printf`
(`Set-Content` writes a byte-order mark that would become part of the
token, so use .NET directly):

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
New-Item -ItemType File -Force secrets\ops_key.txt, secrets\ops_secret.txt | Out-Null   # empty: degraded mode
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"
[IO.File]::WriteAllText("$PWD\secrets\server_token.txt", $token)
```

The image is published as `ghcr.io/xhighhongo41/patent-checker` and
`docker.io/xhighhongo41/patent-checker` with tags `X.Y.Z`, `X.Y` and
`latest`. The container binds `0.0.0.0` internally, but Compose publishes
the port on **your machine's loopback only** (`127.0.0.1:8642`); the server
accepts the `localhost` and `127.0.0.1` Host headers and nothing else.
Fetched documents, search results and the request log live in the named
volume `patent-checker-data`. The container runs as an unprivileged user
on a read-only filesystem with all Linux capabilities dropped; if your
Docker engine rejects one of those settings, the four lines under
`# Hardening` in `compose.yaml` can be removed without changing what the
server does.

The secret files are read once at start; empty OPS files mean "not
configured" (degraded mode). If you run Docker Engine on Linux under an
account that is not UID 1000, see the note on file ownership in
[`secrets/README.md`](secrets/README.md).

To serve a LAN or a VPN (several machines, one server), see
[docs/deploy-lan.md](docs/deploy-lan.md): it adds a TLS-terminating proxy
in front of the same container.

### Option B: without Docker

```sh
uv tool install patent-checker          # or: pipx install patent-checker
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/.env.example
cp .env.example .env
```

Then edit `.env`:

1. Put the EPO OPS key and secret in `PATENT_CHECKER_OPS_KEY` and
   `PATENT_CHECKER_OPS_SECRET` (leave them empty for degraded mode).
2. Generate a bearer token (see above) and put it in
   `PATENT_CHECKER_SERVER_TOKEN`.
3. Read the operator notice with `patent-checker serve --show-operator-notice`
   (add `--lang ja` for Japanese) and set
   `PATENT_CHECKER_OPERATOR_CONSENT=1.0`.

A minimal `.env` looks like this:

```sh
PATENT_CHECKER_OPS_KEY=YOUR_OPS_CONSUMER_KEY
PATENT_CHECKER_OPS_SECRET=YOUR_OPS_CONSUMER_SECRET
PATENT_CHECKER_SERVER_TOKEN=the-token-you-generated
PATENT_CHECKER_OPERATOR_CONSENT=1.0
```

Start the server with `patent-checker serve`. It speaks Streamable HTTP on
`http://127.0.0.1:8642/mcp`. The `.env` file is looked up in the directory
you start from and its parents, stopping short of your home directory and
the filesystem root; variables already in the environment win. Data goes
to the per-user data directory (`~/.local/share/patent-checker` on Linux
and macOS, `%LOCALAPPDATA%\patent-checker` on Windows). If the port is
already taken, the server stops with a message; pick another with
`--port` and register that URL with your agents.

`--transport stdio` runs the server without a network port and without
a token, for a single local client only; it then runs with your user's
permissions, which is why HTTP is the default. How to register it with an
agent is in [docs/mcp-clients.md](docs/mcp-clients.md).

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
2. asks for the bearer token (or reads it from `--token-file`);
3. copies the Skill into `~/.agents/skills/patent-checker/` (read by Codex
   CLI, OpenCode, Cursor, Gemini CLI and Copilot CLI) and into the private
   directories of the agents that need one (`~/.claude/skills/`,
   `~/.openhands/skills/`, `~/.hermes/skills/`);
4. registers the server (`http://127.0.0.1:8642/mcp` unless you pass
   `--url`) with each detected agent, using the agent's own `mcp add`
   command where one exists (Claude Code, Gemini CLI, Copilot CLI, Codex
   CLI with `--token-env`), editing the agent's JSON or TOML configuration
   otherwise (Cursor, OpenCode, Codex CLI), and printing a snippet to paste
   for the agents whose configuration it will not touch (OpenHands, Hermes
   Agent);
5. prints a table of what it did per agent. The exit code is non-zero if
   any step failed; a printed snippet is not a failure.

| Agent | Registered by | Configuration file |
|---|---|---|
| Claude Code | `claude mcp add` | user settings (`.mcp.json` with `--scope project`) |
| Gemini CLI | `gemini mcp add` | `~/.gemini/settings.json` |
| GitHub Copilot CLI | `copilot mcp add` | `~/.copilot/mcp-config.json` |
| Cursor | edited in place | `~/.cursor/mcp.json` |
| OpenCode | edited in place | `~/.config/opencode/opencode.json` |
| Codex CLI | `codex mcp add` / appended | `~/.codex/config.toml` |
| OpenHands, Hermes Agent | snippet to paste | `config.toml`, `~/.hermes/config.yaml` |

Useful options: `--list-agents` (what would be installed where),
`--dry-run` (do everything except write), `--agent claude-code --agent cursor`
(instead of auto-detection; `--agent all` for every supported agent),
`--scope project` (install into the current project instead of your home
directory), `--lang ja` (Japanese notice), `--no-skill` / `--no-mcp`.
Re-running the installer is safe: it refreshes the Skill copies and updates
the server entry in place, and it never overwrites the backup (`.bak`) it
made of your original configuration file on the first run. The one
exception is Codex CLI, whose existing entry is left alone; edit
`~/.codex/config.toml` to change it.

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
file, which is then made readable by you alone (on Windows the file keeps
its usual permissions). With `--token-env` the installer does not need the
value at all: it writes a *reference* to the `PATENT_CHECKER_SERVER_TOKEN`
environment variable, in the notation each agent expands, and you export
that variable before starting the agent. Copilot CLI and OpenHands do not
document such references, so `--token-env` prints a snippet for them
instead. When an agent's own CLI is used for registration, the token
appears in that process's arguments for the duration of the call.

With `--scope project`, the configuration file that holds the token is
written inside your project directory. The installer reminds you: do not
commit it.

Registration commands and snippets that the installer prints never contain
the token; they use `${PATENT_CHECKER_SERVER_TOKEN}` or a placeholder.

### If the server answers 401

A 401 means the token the agent sends is not the one the server was
started with. Compare the two: the server's is in `secrets/server_token.txt`
or `.env`; the agent's is in the configuration file from the table above
(Claude Code shows it with `claude mcp get patent-checker`). Make sure a
placeholder such as `<token>` did not get registered verbatim. A server
that is not running produces a *connection refused* error, not a 401, so
the two are easy to tell apart.

### Registering by hand

The exact command or file for each agent, the GitHub Copilot coding agent,
and the stdio form are in [docs/mcp-clients.md](docs/mcp-clients.md).

### Phone-only and cloud agents

Commit the Skill into your repository as `.agents/skills/patent-checker/`
(`patent-checker install --scope project --no-mcp`, then add the directory
to git), and run the server somewhere the cloud agent can reach over HTTPS
([docs/deploy-lan.md](docs/deploy-lan.md)). The consent record is per
user; a cloud agent whose home directory is reset records it again on the
next run, or use `patent-checker consent record --project` to keep it with
the project.

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
million tokens of agent traffic (measured with Claude Code on a
medium-sized project during development), most of it in screening; the
Skill hands the first screening stage to smaller models where your agent
supports delegation. One run is not exhaustive: repeated runs, different
query vocabularies and a professional search will each find things a
single run does not.

## Configuration

Environment variables read by the server (`patent-checker serve`) and,
where noted, by the CLI. See `.env.example` for the same list with
comments. The CLI's `--host` and `--port` options override
`PATENT_CHECKER_SERVER_HOST` and `PATENT_CHECKER_SERVER_PORT`.

| Variable | Default | Meaning |
|---|---|---|
| `PATENT_CHECKER_OPS_KEY`, `PATENT_CHECKER_OPS_SECRET` | unset | EPO OPS consumer key and secret. Either the value, or the path to a file holding it via the `_FILE` variants (`PATENT_CHECKER_OPS_KEY_FILE`, …); never both. Empty files mean "not configured" (degraded mode). |
| `PATENT_CHECKER_OPERATOR_CONSENT` | unset (required) | Version of the operator notice you acknowledged (`1.0`). The server refuses to start otherwise. |
| `PATENT_CHECKER_SERVER_TOKEN` (`_FILE`) | unset (required for http) | The bearer token clients must present. |
| `PATENT_CHECKER_SERVER_HOST`, `PATENT_CHECKER_SERVER_PORT` | `127.0.0.1`, `8642` | Bind address. Binding beyond loopback additionally requires `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` and TLS in front. |
| `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` | unset | Comma-separated host names clients will use in the Host header. Names only: wildcards are rejected, and an IPv6 address is written without brackets. |
| `PATENT_CHECKER_SERVER_RPS`, `PATENT_CHECKER_SERVER_BURST` | `5`, `10` | Limit on incoming MCP requests (a guard against runaway agent loops; upstream pacing is separate). |
| `PATENT_CHECKER_DATA_DIR` | per-user data directory (server), `./.patent-checker` (CLI) | Search cache, request log and, when set, the shared document cache (`<dir>/cache`). The server keeps its data with the user that runs it; the CLI keeps it with the project it is run from. The container image sets it to `/data`. |
| `PATENT_CHECKER_CACHE_DIR` | `<per-user data dir>/cache` | Shared document cache (bibliography, claims, legal status, families, Google Patents pages), reused across projects by the CLI and the server alike. |
| `PATENT_CHECKER_CACHE_TTL` | `biblio=90d,claims=0,legal=7d,family=30d,gp=0,search=1d,searchbib=1d` | Per-kind expiry overrides: `<n>d`, `<n>h`, or `0` meaning **never expires** (not "do not cache"). |
| `PATENT_CHECKER_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. All server logs go to standard error. |

`.env` is read by every `patent-checker` command from the directory it runs
in or a parent of it, never from your home directory or the filesystem
root. With Compose, the credentials come from the files under `secrets/`,
and the `.env` next to `compose.yaml` provides `PATENT_CHECKER_OPERATOR_CONSENT`
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
user, the mounted directory must be writable by it. Do not run
`patent-checker clean --shared` inside the container: to wipe the
container's data, delete its volume (`docker compose down -v`).

</details>

## Cache and clean-up

Fetched patent documents are kept once, in the per-user shared cache, with
a per-kind expiry; search results and the request log stay with whoever ran
them (the server's data directory, or the project's `.patent-checker/`
when the CLI is used from the project).

- `patent-checker cache status` shows what is cached, where, and how much of
  it has expired or is broken.
- `patent-checker cache clear [--kind KIND] [--older-than DAYS] [--pub …] [--expired] [--broken]`
  lists what it would delete; add `--yes` to delete. `KIND` is one of
  `biblio`, `claims`, `legal`, `family`, `gp`, `search`, `searchbib`.
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
  token on every request, compared in constant time. Host and Origin
  headers are validated against the names you configured (no wildcards),
  which is what stops a DNS-rebinding attempt from reaching a loopback
  server; a page served from loopback itself is not blocked by that check,
  and is kept out by the token.
- It only ever connects to `ops.epo.org` and `patents.google.com`, from the
  server and from the command-line tool alike, and only writes below its
  own data directory. It has no code-execution tools.
- It receives search expressions and publication numbers, nothing else, and
  refuses oversized requests. Your code and documents are analysed by your
  agent, on your machine. Authenticated clients can see where the server
  keeps its data (`server_status`); nothing else about the host is exposed.
- Incoming requests are rate-limited, and upstream requests are paced,
  serialised one at a time and cached so that a runaway agent cannot
  hammer the data sources.
- `GET /health` is unauthenticated for container health checks and returns
  only a status and the version.
- Credentials are read from the environment or from files, are never
  logged, and never appear in the startup banner or in error messages.
- The container runs as an unprivileged user on a read-only filesystem with
  all capabilities dropped; only `/data` is writable.
- `--transport stdio` has no authentication: use it only for a single local
  client, aware that the server then runs with your permissions.
- Serving a LAN or the internet is possible but is your responsibility:
  the transport is plain HTTP, so put a TLS-terminating reverse proxy in
  front ([docs/deploy-lan.md](docs/deploy-lan.md)), set
  `PATENT_CHECKER_SERVER_ALLOWED_HOSTS`, and treat the token as the only
  thing between the network and your OPS quota.
- The Skill treats everything the server returns, patent text included, as
  data to analyse, not as instructions to follow.
- Releases are built and published by CI from a version tag; dependencies
  and the container image are scanned for known vulnerabilities on every
  change. To report a security problem, see [SECURITY.md](SECURITY.md).

## Data sources and fair use

- **EPO Open Patent Services** is used under the terms attached to your
  registered application, including its weekly fair-use quota. The server
  spaces its requests and honours OPS throttling headers, but it cannot know
  how many other clients share your credentials. Legal-status information
  comes from OPS and is the authoritative value in reports.
- **Google Patents** is fetched one document page at a time, at a pace and
  volume comparable to a person reading in a browser, and every page is
  cached so it is not fetched twice. The search endpoint is deliberately not
  used: Google's `robots.txt` permitting a path is not the same as its
  terms of service permitting bulk collection.
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
- A document whose legal status comes back with no events at all is
  reported as such (`events: []` with a note), which is not the same as
  "not found".
- When a notice changes, the Skill asks for consent again, or the server
  asks for a fresh acknowledgement; until then nothing is asked twice.

## Updating

**Docker Compose**: `docker compose pull && docker compose up -d`.

**Without Docker**: stop the running server, then

```sh
uv tool upgrade patent-checker      # or: pipx upgrade patent-checker
patent-checker serve
```

**Tool and Skill on your machine**: `uv tool upgrade patent-checker &&
patent-checker install`. Re-running the installer refreshes the Skill
copies and the server entry in each agent's configuration. Keep the server
and the Skill at the same version: both are updated by the same command,
and a Skill from a newer release may call tools an older server does not
have.

## Uninstalling

- **Agents**: remove the `patent-checker` entry from each agent's MCP
  configuration (`claude mcp remove patent-checker`, `gemini mcp remove
  patent-checker`, or delete the entry from the file in the table above),
  and delete the Skill directories `~/.agents/skills/patent-checker/`,
  `~/.claude/skills/patent-checker/`, `~/.openhands/skills/patent-checker/`,
  `~/.hermes/skills/patent-checker/` (and their `.agents/skills/` and
  `.claude/skills/` counterparts in projects where you used
  `--scope project`).
- **Server**: `docker compose down -v` (container, cache and all) or
  `uv tool uninstall patent-checker` (plus the per-user data directory,
  `~/.local/share/patent-checker` or `%LOCALAPPDATA%\patent-checker`, if
  you want the cache gone too).
- **Records**: `patent-checker clean --yes --include-artifacts --include-consent`
  in a project removes its reports, notes and consent record (run it before
  uninstalling the tool); `~/.config/patent-checker/consent.json` holds the
  per-user consent record.

## Changelog

- **v1.0** (2026-09): stable release. Constant-time bearer-token check,
  `.env` never read from your home directory or the filesystem root,
  request size limits, hardened container (read-only, no capabilities),
  LAN/TLS example with Caddy, installer improvements (`--token-env` needs
  no token value, owner-only configuration files, backups never
  overwritten, non-zero exit on failure, Codex CLI registration), cache and
  parser fixes (concurrent writes, CPC symbols, empty legal status,
  non-UTF-8 pages), upstream calls retried once on 401/429/503 and never
  blocking unrelated tools, every command reads `.env`, plain error
  messages instead of tracebacks, Python 3.13, security scanning in CI,
  listing in the MCP Registry, reorganised documentation.
- **v0.5** (2026-09-06): first public release. Docker image (GHCR, Docker
  Hub) and Compose deployment with file-based secrets, `patent-checker
  install` for eight agents, bootstrap scripts, PyPI package, `/health`,
  log level setting.
- **v0.4** (2026-09-05): per-user shared document cache with per-kind expiry
  and self-repair, `cache status` / `cache clear` / `clean`, publication
  number normalisation for the 2026 change in US numbers.
- **v0.3** (2026-09-04): MCP server (FastMCP 4, Streamable HTTP, bearer
  token, Host/Origin validation, outbound allowlist, rate limits), operator
  notice, first file cache.
- **v0.1–v0.2** (2026-08): development versions; core library, CLI and the
  Agent Skill with its consent gate and report template.

## License

Apache-2.0. See [LICENSE](LICENSE).

Questions and bug reports: [GitHub Issues](https://github.com/xhighhongo41/patent-checker/issues).
