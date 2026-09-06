"""Acquisition and safe rendering of the MCP server's bearer token.

The installer needs the token twice: once as a literal value, when a host
application stores credentials in its own configuration file, and once as
an environment-variable reference, when the host expands one for us. This
module keeps both spellings in one place and guarantees that the token
itself never reaches a message: it is read from a file, from the
environment or from an interactive prompt, and failures are reported by
source, never by value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path

from .errors import InstallerError
from .writers import redact

#: Environment variable holding the server's bearer token.
ENV_TOKEN = "PATENT_CHECKER_SERVER_TOKEN"

#: Text shown by the installer when it has to ask for the token.
TOKEN_PROMPT = "Server bearer token: "


class ReferenceStyle(StrEnum):
    """How a host application spells an environment-variable reference.

    The value is the template; ``VAR`` stands for the variable name and is
    substituted by :func:`env_reference`.
    """

    DOLLAR = "${VAR}"
    VSCODE = "${env:VAR}"
    OPENCODE = "{env:VAR}"
    NAME = "VAR"


def env_reference(style: ReferenceStyle) -> str:
    """Return the reference to :data:`ENV_TOKEN` written in *style*.

    Args:
        style: The spelling the host application understands.

    Returns:
        For example ``"${PATENT_CHECKER_SERVER_TOKEN}"`` for
        :attr:`ReferenceStyle.DOLLAR`.
    """
    return style.value.replace("VAR", ENV_TOKEN)


def resolve_token(
    *,
    token_file: Path | None,
    environ: Mapping[str, str],
    prompt: Callable[[str], str] | None,
) -> str:
    """Return the bearer token from the first source that provides one.

    Sources are tried in order of explicitness: an operator-supplied file,
    then the environment, then an interactive prompt. Whitespace around the
    value is stripped, since a token file usually ends with a newline.

    Args:
        token_file: File holding the token, or ``None``.
        environ: Environment to read :data:`ENV_TOKEN` from.
        prompt: Callable asked for the token when no other source has one,
            or ``None`` when the installer is not interactive.

    Returns:
        The stripped token.

    Raises:
        InstallerError: The chosen source exists but is empty or
            unreadable, or no source is available at all. The message
            names the source only; it never contains the token.
    """
    if token_file is not None:
        try:
            content = token_file.read_text(encoding="utf-8")
        except OSError as error:
            raise InstallerError(
                f"token file {token_file} could not be read ({error.strerror})"
            ) from error
        except UnicodeDecodeError as error:
            raise InstallerError(f"token file {token_file} is not valid UTF-8") from error
        token = content.strip()
        if not token:
            raise InstallerError(f"token file {token_file} is empty")
        return token

    from_environment = environ.get(ENV_TOKEN, "").strip()
    if from_environment:
        return from_environment

    if prompt is not None:
        token = prompt(TOKEN_PROMPT).strip()
        if not token:
            raise InstallerError("no server token: the value entered at the prompt was empty")
        return token

    raise InstallerError(
        f"no server token: pass --token-file, set {ENV_TOKEN}, or run interactively"
    )


def bearer(token: str) -> str:
    """Return the ``Authorization`` header value for *token*."""
    return f"Bearer {token}"


def mask(text: str, token: str) -> str:
    """Return *text* with *token* replaced by ``****``.

    A one-token alias of :func:`patent_checker.installer.writers.redact`,
    so callers that only handle the bearer token do not have to build a
    sequence.
    """
    return redact(text, [token])
