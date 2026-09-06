# syntax=docker/dockerfile:1

# Patent Checker container image.
#
# The image is built in two stages because the build needs uv, a writable
# cache and the whole source tree, while running the server needs none of
# them: the runtime stage copies only `/app/.venv`, the virtual environment
# uv resolved from `uv.lock`, so uv itself, the build cache and the sources
# never reach a published layer. Both stages sit on the same
# `python:3.12-slim-trixie` base (Debian pinned so that the runtime does not
# drift under the venv), which is what makes that copy safe -- the venv
# holds symlinks into `/usr/local`, and the interpreter they point at has to
# be the identical build.
#
# Networking: the server binds 0.0.0.0 because a container's own loopback is
# unreachable from the host. Binding a non-loopback address makes the server
# demand PATENT_CHECKER_SERVER_ALLOWED_HOSTS, set here to the loopback names
# only. That is deliberate: the supported deployment publishes the port on
# the *host's* loopback (see `127.0.0.1:8642:8642` in compose.yaml), so the
# only Host headers that can legitimately arrive are `localhost` and
# `127.0.0.1`, and a DNS-rebinding attempt spelling any other name is
# refused. Serving a LAN means overriding that variable with the names
# clients really use and terminating TLS in front -- this transport is
# plain HTTP.

FROM python:3.12-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

# Compile bytecode at install time so the first request does not pay for it;
# copy out of the cache mount instead of hardlinking, because the cache lives
# on a different filesystem than the layer; and never let uv download its own
# interpreter, so the venv is always built against the image's python.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, from the lockfile alone. This layer is reused for as
# long as uv.lock and pyproject.toml are untouched, which is why they are
# bind-mounted rather than copied: copying them would invalidate the layer on
# every unrelated file change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /app

# Then the project itself, installed as a built wheel (`--no-editable`) so
# the runtime image does not need the source tree to stay in place. The wheel
# carries the bundled Skill, which is why README.md, LICENSE and skills/ must
# be part of the build context (see .dockerignore).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.12-slim-trixie

LABEL org.opencontainers.image.source="https://github.com/xhighhongo41/patent-checker" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.description="Patent prior-art exploration MCP server; it explores prior art and does not judge infringement"

# An unprivileged user owns /data, the only path the server ever writes to.
RUN useradd --uid 1000 --user-group --create-home app \
    && mkdir -p /data \
    && chown app:app /data

COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PATENT_CHECKER_DATA_DIR=/data \
    PATENT_CHECKER_SERVER_HOST=0.0.0.0 \
    PATENT_CHECKER_SERVER_ALLOWED_HOSTS=localhost,127.0.0.1 \
    PATENT_CHECKER_LOG_LEVEL=info

USER app
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8642

# curl is deliberately not installed: the venv's python can probe the
# unauthenticated health endpoint on its own, and every package left out of
# the runtime image is one less thing to patch.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8642/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["patent-checker"]
CMD ["serve"]
