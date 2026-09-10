# Security policy

## Supported versions

Security fixes go into the latest minor release line only (the newest
`major.minor` on PyPI, GHCR and Docker Hub). Older releases are not patched;
please update.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Use GitHub's
private vulnerability reporting instead:

1. Open <https://github.com/xhighhongo41/patent-checker/security/advisories/new>.
2. Describe the problem, the version you tested, and how to reproduce it.

You should receive an acknowledgement within seven days. Once a fix is
released, the advisory is published with credit to the reporter unless you
ask otherwise.

## Scope

In scope: the `patent-checker` package (CLI, MCP server, installer), the
container image and Compose files in this repository, the bootstrap scripts,
and the bundled Agent Skill.

Out of scope: vulnerabilities in the upstream services the tool talks to
(EPO OPS, Google Patents), in the coding agents that load the Skill, or in
third-party dependencies that already have a public advisory (report those
upstream; a dependency update here follows through Dependabot and the CI
audit).

## What the tool does not protect against

The MCP server is meant to run on your own machine or network, bound to the
loopback interface by default and guarded by a bearer token. It provides no
TLS of its own; if you expose it beyond loopback, terminate TLS in front of
it as described in the README. Reports produced by the Skill may themselves
carry legal risk, as explained in the README's important notices; that is a
property of the subject matter, not a vulnerability.
