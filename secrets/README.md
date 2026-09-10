# Container secrets

`compose.yaml` mounts three files from this directory into the container as
Docker secrets. Create them before the first `docker compose up`; Compose
refuses to start the stack while any of them is missing.

| File | Mounted at | Read through | Required |
| --- | --- | --- | --- |
| `ops_key.txt` | `/run/secrets/ops_key` | `PATENT_CHECKER_OPS_KEY_FILE` | no |
| `ops_secret.txt` | `/run/secrets/ops_secret` | `PATENT_CHECKER_OPS_SECRET_FILE` | no |
| `server_token.txt` | `/run/secrets/server_token` | `PATENT_CHECKER_SERVER_TOKEN_FILE` | yes |

The server strips surrounding whitespace from each file, so a trailing
newline is harmless. **An empty file means "not configured"**, which is how
the EPO OPS credentials are made optional: leave both OPS files empty and the
server starts in degraded mode, serving everything that does not need OPS and
reporting the rest as unavailable.

## Create the files (Linux, macOS)

```sh
cd /path/to/patent-checker

# EPO OPS credentials from https://developers.epo.org/ -- or two empty files
# to run in degraded mode.
printf '%s' 'YOUR_OPS_CONSUMER_KEY'    > secrets/ops_key.txt
printf '%s' 'YOUR_OPS_CONSUMER_SECRET' > secrets/ops_secret.txt
# Degraded mode instead:
#   : > secrets/ops_key.txt
#   : > secrets/ops_secret.txt

# The bearer token every MCP client must send on /mcp. Generate it, never
# invent it by hand: it is the only thing standing between the published
# port and the tools.
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/server_token.txt

chmod 600 secrets/*.txt
```

Give the same token to the MCP client, for example as
`Authorization: Bearer <token>`. To rotate it, rewrite `server_token.txt`
and run `docker compose up -d --force-recreate`.

### A note on `chmod 600` under Docker Engine on Linux

Compose mounts these files into the container as-is, and the server runs as
UID 1000. On a Linux host where your account is not UID 1000, mode `600`
makes the files unreadable inside the container and the server fails to
start. Two ways out, in order of preference:

**1. Hand the files to that UID and keep `600`.** The files stay unreadable
to every other account on the host, which is what `600` is for:

```sh
sudo chown 1000:1000 secrets/*.txt
chmod 600 secrets/*.txt
```

**2. Guard the directory instead of the files.** If you cannot change the
owner, take the search permission away from everyone else on the directory
that holds them:

```sh
chmod 700 secrets && chmod 644 secrets/*.txt
```

Mode `644` looks permissive, but reaching a file means traversing its
directory first: with `secrets/` at `700`, no other account on the host can
enter it, so the files inside are unreadable to them whatever their own mode
says. UID 1000 inside the container is not affected -- the bind mount hands
it the file directly, without a lookup through the host directory. This is
weaker than option 1 in one respect: anything that already has a handle on
the directory (a backup job, a pre-existing shell inside it) keeps its
access, so prefer option 1 where you can.

Docker Desktop (macOS, Windows) maps ownership for you, so `600` is enough
there.

## Create the files (Windows, PowerShell)

```powershell
Set-Location C:\path\to\patent-checker
New-Item -ItemType Directory -Force secrets | Out-Null

# Empty OPS files: degraded mode.
New-Item -ItemType File -Force secrets\ops_key.txt    | Out-Null
New-Item -ItemType File -Force secrets\ops_secret.txt | Out-Null

# The bearer token. WriteAllText is used instead of `>` or Set-Content
# because Windows PowerShell 5.1 writes UTF-16 with a byte-order mark, and
# the container would read that BOM as part of the token.
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"
[IO.File]::WriteAllText("$PWD\secrets\server_token.txt", $token)
```

## Keeping them out of git

`.gitignore` excludes everything in this directory except this README, so
these files are never committed. Nothing here is recoverable from the
repository -- back the token and the OPS credentials up wherever you keep
your other secrets, and treat a leaked `server_token.txt` as a reason to
rotate it.
