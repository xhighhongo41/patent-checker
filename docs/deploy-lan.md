# Serving Patent Checker on a LAN or VPN (TLS)

日本語版: [deploy-lan_ja.md](deploy-lan_ja.md)

The default deployment (`compose.yaml` at the repository root) publishes the
server on the host's loopback address only. That is the right choice for one
developer on one machine. When several machines, or a team, should share one
server, the server has to be reachable over the network, and two things then
matter that loopback made irrelevant:

- **TLS.** The MCP transport is plain HTTP and every request carries the
  bearer token. On a network the token would travel in the clear, so a
  TLS-terminating reverse proxy must sit in front of the server.
- **Host names.** The server refuses requests whose `Host` header is not on
  its allow-list (a defence against DNS rebinding). You therefore declare the
  name clients will use.

`examples/lan-tls/` is a ready-made Compose file that does both with
[Caddy](https://caddyserver.com/) as the proxy. The server's port is never
published on the host; only Caddy's port 443 is.

## What you need

- A host name that resolves to the machine for every client: a LAN DNS entry,
  a VPN name, or an entry in each client's hosts file. Call it
  `patents.lan` below.
- The same three secret files as the loopback deployment
  (`secrets/README.md` at the repository root explains how to create them),
  placed in `examples/lan-tls/secrets/`.
- Docker Compose v2.

## Steps

```sh
git clone https://github.com/xhighhongo41/patent-checker.git
cd patent-checker/examples/lan-tls
cp .env.example .env
# edit .env: PATENT_CHECKER_LAN_HOST=patents.lan and the operator consent
# create secrets/ops_key.txt, secrets/ops_secret.txt, secrets/server_token.txt
docker compose up -d --wait
```

`docker compose ps` shows both services healthy. Caddy answers on
`https://patents.lan/`; the MCP endpoint is `https://patents.lan/mcp` and the
unauthenticated health check is `https://patents.lan/health`.

## Certificates: internal CA or ACME

The example ships with `tls internal` in the `Caddyfile`: Caddy creates its
own certificate authority on first start and issues a certificate for
`patents.lan` from it. Nothing leaves the machine, and no public DNS is
needed, but **each client must trust that CA once**. Export the root
certificate from the running container:

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

then install `caddy-root.crt` in the operating system's trust store on each
client (macOS: Keychain Access, "Always Trust"; Windows: `certmgr.msc`,
Trusted Root Certification Authorities; Linux: `update-ca-certificates` or
`trust anchor`). Coding agents use the system store through their runtime, so
this is usually enough; an agent that bundles its own CA list may need its
own setting, and `curl --cacert caddy-root.crt https://patents.lan/health` is
the quickest way to tell whether the certificate or the agent is the problem.

If the host name is a **public DNS name** and ports 80 and 443 of the machine
are reachable from the internet, delete the `tls internal` line instead.
Caddy then obtains a certificate from Let's Encrypt automatically and no
client-side trust step is needed.

## Connecting agents

Register the server exactly as in the README, with the HTTPS URL:

```sh
TOKEN=$(cat examples/lan-tls/secrets/server_token.txt)
patent-checker install --url https://patents.lan/mcp --token-file examples/lan-tls/secrets/server_token.txt
```

Every client presents the same bearer token; there is one token per server,
not per user. Rotate it by rewriting `server_token.txt` and running
`docker compose up -d --force-recreate patent-checker`, then re-running
`patent-checker install` on each client.

## Narrowing exposure

- Bind Caddy to one interface with `PATENT_CHECKER_LAN_BIND` in `.env` (for
  example the VPN address) when the machine also has an internet-facing
  interface.
- The server accepts only the names in `PATENT_CHECKER_SERVER_ALLOWED_HOSTS`;
  the example sets it from `PATENT_CHECKER_LAN_HOST`. Wildcards are rejected
  on purpose; list every name explicitly, comma-separated.
- The operator notice applies to whoever runs the server; telling the team
  about the notices in the README is the operator's responsibility.

## Removing everything

```sh
docker compose down -v      # stops both services and deletes the data and certificate volumes
```

The search cache, request log and Caddy's CA are in named volumes and go
away with `-v`; the secret files and `.env` stay on disk until you delete
them.
