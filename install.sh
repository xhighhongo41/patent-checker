#!/bin/sh
# Bootstrap installer for patent-checker.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.sh | sh
#
# To read this script before running it:
#   curl -LsSf https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.sh | more
#
# This script only installs uv (if missing) and the patent-checker CLI via
# "uv tool install". It does NOT show the consent notice, place the Skill
# or register the MCP server -- that all happens interactively when you run
# "patent-checker install" (this script runs it for you automatically when
# stdin is a TTY; otherwise it just prints the command to run next).
#
# Environment variables:
#   PATENT_CHECKER_SPEC   Spec passed to "uv tool install" (default:
#                         patent-checker). Examples:
#                           git+https://github.com/xhighhongo41/patent-checker@v0.5.0
#                           .

set -eu

dry_run=0

usage() {
    cat <<'EOF'
Usage: install.sh [--dry-run] [--help]

  --dry-run  Show what would be done without installing anything.
  --help     Show this help message and exit.

Environment variables:
  PATENT_CHECKER_SPEC  Spec passed to "uv tool install" (default: patent-checker).
EOF
}

for arg in "$@"; do
    case "$arg" in
        --dry-run)
            dry_run=1
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

spec="${PATENT_CHECKER_SPEC:-patent-checker}"

run_or_show() {
    if [ "$dry_run" -eq 1 ]; then
        echo "would run: $*"
    else
        "$@"
    fi
}

install_uv() {
    if command -v curl >/dev/null 2>&1; then
        echo "Installing uv (https://astral.sh/uv)"
        if [ "$dry_run" -eq 1 ]; then
            echo "would run: curl -LsSf https://astral.sh/uv/install.sh | sh"
        else
            curl -LsSf https://astral.sh/uv/install.sh | sh
        fi
    elif command -v wget >/dev/null 2>&1; then
        echo "Installing uv (https://astral.sh/uv)"
        if [ "$dry_run" -eq 1 ]; then
            echo "would run: wget -qO- https://astral.sh/uv/install.sh | sh"
        else
            wget -qO- https://astral.sh/uv/install.sh | sh
        fi
    else
        echo "Neither curl nor wget is available." >&2
        echo "Install uv manually (see https://astral.sh/uv) and re-run this script." >&2
        exit 1
    fi
}

# Step 1: make sure uv is available.
if command -v uv >/dev/null 2>&1; then
    echo "uv found: $(command -v uv)"
else
    echo "uv not found"
    install_uv
    if [ "$dry_run" -eq 1 ]; then
        echo "would add: \$HOME/.local/bin to PATH"
    else
        PATH="$HOME/.local/bin:$PATH"
        export PATH
        if ! command -v uv >/dev/null 2>&1; then
            echo "uv still not found; open a new shell and run this script again." >&2
            exit 1
        fi
    fi
fi

# Step 2: install (or upgrade) the CLI.
run_or_show uv tool install --upgrade "$spec"

# Step 3: confirm the CLI is on PATH and report its version.
run_or_show patent-checker --version

# Step 4: hand off to the interactive installer, when possible.
if [ "$dry_run" -eq 1 ]; then
    echo "would run: patent-checker install (if stdin is a TTY)"
    echo "would print: Next: run 'patent-checker install' in your project directory (it shows the notice, installs the Skill and registers the MCP server) (if stdin is not a TTY)"
    exit 0
fi

if [ -t 0 ]; then
    exec patent-checker install
else
    echo "Next: run 'patent-checker install' in your project directory (it shows the notice, installs the Skill and registers the MCP server)"
    exit 0
fi
