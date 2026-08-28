# Patent Checker v0.1 PoC

Throwaway proof-of-concept scripts for observing the external APIs used by
Patent Checker (EPO OPS, Google Patents). This directory will be replaced by
the `patent_checker/` core library in v0.2 and then removed.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) and run `uv sync`.
2. Copy `.env.example` to `.env` and fill in your EPO OPS credentials
   (register at <https://developers.epo.org/>).

## Usage

- `uv run python poc/check_access.py` — verify EPO OPS connectivity
  (OAuth2 token + one trivial search).
- `tools/check.sh` — run lint and tests.

Raw API responses are written to an untracked local data directory and are
never committed.
