# Serving Patent Checker beyond one machine (LAN, VPN, tailnet)

日本語版: [deploy-lan_ja.md](deploy-lan_ja.md)

The default deployment (`compose.yaml` at the repository root) publishes the
server on the host's loopback address only. That is the right choice for one
developer on one machine. When several machines, or a team, should share one
server, the server has to be reachable over a network. This guide describes
three ways to do that and what each of them leaves to you.

## What the server does and does not do

- **It does not provide TLS, and it does not require it.** The MCP transport
  is plain HTTP and every request carries the bearer token. Whether that
  traffic is encrypted depends on the network you put the server on and on
  what you put in front of it.
- **Choosing the network is the operator's responsibility.** A tailnet or a
  WireGuard VPN already encrypts the traffic between its members; a home or
  office LAN usually does not, and anyone on it who can read the traffic can
  read the token. Decide whether the network you use is trustworthy enough
  for that. When a TLS-terminating proxy is available, use it: it is the
  recommended setup, and the server works behind one unchanged.
- **Host names are always checked.** The server refuses requests whose
  `Host` header is not on its allow-list (a defence against DNS rebinding),
  whatever the network. You declare the names clients will use in
  `PATENT_CHECKER_SERVER_ALLOWED_HOSTS`. Wildcards are rejected on purpose;
  list every name, comma-separated.
- **There is one token per server.** Everyone who can reach the server and
  holds the token can use it; there are no per-user accounts.

| Setup | Encryption on the wire | Certificates | Fits |
|---|---|---|---|
| 1. Tailscale Serve | HTTPS, terminated by Tailscale | Issued by Tailscale for the machine's tailnet name | A tailnet you already use |
| 2. Caddy (`examples/lan-tls/`) | HTTPS, terminated by Caddy | Caddy's internal CA, or Let's Encrypt for a public name | A LAN or VPN, with or without public DNS |
| 3. Plain HTTP on a private network | Only what the network itself provides (for example WireGuard) | None | A VPN or LAN you trust, when no proxy is wanted |

## 1. Tailscale Serve

[Tailscale Serve](https://tailscale.com/kb/1312/serve) forwards an HTTPS
name inside your tailnet to a port on `127.0.0.1` of the machine. The server
keeps its default loopback binding, which is exactly what Serve expects: it
only proxies to `127.0.0.1`.

Before the first use, enable **MagicDNS** and **HTTPS certificates** in the
Tailscale admin console. The machine's name then looks like
`patents.example-tailnet.ts.net` (`tailscale status` shows it). If your
tailnet is run by a self-hosted control server such as Headscale, whether
Serve and its certificates work depends on that server; setup 3 works on any
tailnet.

With Docker, start the default deployment as in the README, then:

```sh
# .env next to compose.yaml: add the tailnet name (keep the loopback names)
PATENT_CHECKER_SERVER_ALLOWED_HOSTS=patents.example-tailnet.ts.net,localhost,127.0.0.1

docker compose up -d --wait
tailscale serve --bg 8642
```

Without Docker, run `patent-checker serve` with the same
`PATENT_CHECKER_SERVER_ALLOWED_HOSTS` in its `.env`, then the same
`tailscale serve` command.

The Serve command line has changed between Tailscale versions; if the
command above is rejected, `tailscale serve --help` shows the form your
version takes (older versions spell it `tailscale serve https / http://127.0.0.1:8642`).
`tailscale serve status` shows what is being served, and
`tailscale serve --https=443 off` (or `tailscale serve reset`) stops it.
Check from another machine in the tailnet:

```sh
curl https://patents.example-tailnet.ts.net/health
```

The MCP endpoint is `https://patents.example-tailnet.ts.net/mcp`. Do not use
Tailscale **Funnel** for this: Funnel publishes the service to the whole
internet.

## 2. Caddy with TLS (`examples/lan-tls/`)

`examples/lan-tls/` is a ready-made Compose file with
[Caddy](https://caddyserver.com/) as a TLS-terminating proxy. The server's
port is never published on the host; only Caddy's port 443 is.

You need a host name that resolves to the machine for every client (a LAN
DNS entry, a VPN name, or an entry in each client's hosts file; `patents.lan`
below), the same three secret files as the loopback deployment
(`secrets/README.md` at the repository root explains how to create them)
placed in `examples/lan-tls/secrets/`, and Docker Compose v2.

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
unauthenticated health check is `https://patents.lan/health`. The example
sets the server's allow-list from `PATENT_CHECKER_LAN_HOST`.

### Certificates: internal CA or ACME

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

Bind Caddy to one interface with `PATENT_CHECKER_LAN_BIND` in `.env` (for
example the VPN address) when the machine also has an internet-facing
interface.

## 3. Plain HTTP on a private network

On a WireGuard VPN, or a LAN you trust, the server can listen on that
network's address directly. The token then travels unencrypted inside that
network, and every member of it can reach the server: use this only on a
network whose members and encryption you are comfortable with.

Without Docker, in the `.env` next to where you run `patent-checker serve`:

```sh
PATENT_CHECKER_SERVER_HOST=10.8.0.1
PATENT_CHECKER_SERVER_ALLOWED_HOSTS=10.8.0.1,patents.vpn
```

With Docker, in `compose.yaml` replace `127.0.0.1:` in the `ports:` line by
the network's address (`"10.8.0.1:${PATENT_CHECKER_PORT:-8642}:8642"`) and
set `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` in `.env` as above.

Bind to the private network's address rather than `0.0.0.0` when the
machine also has an internet-facing interface. The server's start-up banner
reminds you that it provides no TLS; that line is informational.

## Connecting agents

On each client machine, install the CLI and register the server with its
URL (`https://…` for setups 1 and 2, `http://10.8.0.1:8642/mcp` for setup 3)
and the token:

```sh
uv tool install patent-checker
patent-checker install --url https://patents.example-tailnet.ts.net/mcp --token-file ~/patent-checker-token.txt
```

- **Handing out the token.** Copy the server's token file to each client
  over a channel you trust (the tailnet or VPN itself, a password manager),
  keep it readable only by you (`chmod 600` on macOS and Linux), and point
  `--token-file` at it. `--token-env` writes a reference to
  `PATENT_CHECKER_SERVER_TOKEN` instead of the token for the agents that can
  expand one (see "Passing the bearer token without exposing it" in the
  README).
- **Rotating it.** Rewrite the server's token file and restart the server
  (`docker compose up -d --force-recreate patent-checker`), then run
  `patent-checker install` again on each client with the new token.
- **Removing a client.** `patent-checker uninstall` removes the registration
  and the Skill from that machine.

`patent-checker install --json` prints what was registered as JSON, which
is convenient when you set up several machines with a script.

## Removing everything

- Setup 1: `tailscale serve reset`, then stop the server as in the README.
- Setup 2, in `examples/lan-tls/`:

  ```sh
  docker compose down -v      # stops both services and deletes the data and certificate volumes
  ```

  The search cache, request log and Caddy's CA are in named volumes and go
  away with `-v`; the secret files and `.env` stay on disk until you delete
  them.
- Setup 3: stop the server and remove the address from `.env` or
  `compose.yaml`.

The operator notice applies to whoever runs the server; telling the team
about the notices in the README is the operator's responsibility.
