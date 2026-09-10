"""Command-line entry point for patent-checker.

Every subcommand prints one JSON object to stdout (``json.dumps(...,
ensure_ascii=False, indent=2)``); three subcommands are exceptions: like
``consent show``, ``serve --show-operator-notice`` prints the raw notice
Markdown so it can be shown to a human as-is, ``install`` prints the notice
and its own report as text and reports a failure as ``error: <message>`` on
stderr (exit code 2), and ``serve`` on success prints
nothing to stdout at all -- its startup banner goes to stderr (stdout is the
protocol channel of the stdio transport) and it then blocks serving until the
process is stopped. Errors are reported the same way, as
``{"error": {"type": str, "message": str}}``, so a calling Skill/agent can
branch on the JSON alone rather than parsing stderr text; the process exit
code encodes the same distinction:

- ``0``: success (this includes ``claims`` reporting a document as
  unavailable -- "could not be fetched" is itself a valid, non-error result).
- ``2``: invalid input (a ``ValueError``, including a malformed argument
  argparse itself rejects) or a file that could not be read or written
  (an ``OSError``, error type ``io_error``, message naming the path).
- ``3``: an external API call failed (``httpx.HTTPError``).
- ``4``: configuration is missing or invalid (``patent_checker.config.
  ConfigError``): EPO OPS credentials for an OPS-backed command (error type
  ``ops_not_configured``), or any other invalid configuration -- MCP server
  settings for ``serve``, a port ``serve`` cannot listen on, or a malformed
  ``$PATENT_CHECKER_CACHE_TTL`` for a cache-backed command (error type
  ``config_error``).

``serve`` with the stdio transport prints its error envelope to stderr
instead of stdout, because stdout carries the protocol.

Every subcommand handler is a thin adapter over :mod:`patent_checker.service`,
which holds the actual logic and is shared with the MCP server.

Two things are set up for every subcommand before any of them runs: the
nearest ``.env`` file is read once (so ``cache status``, ``clean`` and the
OPS-backed commands all agree on which data directory is meant), and stdout
and stderr are switched to UTF-8 (so a Japanese title cannot end a command
with an encoding error on a Windows console).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from patent_checker import __version__, cleanup, config, consent, installer, service
from patent_checker.cache import Cache, CacheEntry, default_cache
from patent_checker.config import ConfigError, ops_configured
from patent_checker.ops.client import OpsClient
from patent_checker.validation import validate_batch


class _JsonArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that reports its own errors as the shared JSON envelope.

    Without this override, an invalid argument (missing required option,
    unknown subcommand, ...) would print a plain-text usage message to
    stderr; a calling Skill/agent would then have to special-case argparse's
    own error format instead of the ``{"error": ...}`` shape every other
    failure uses.
    """

    def error(self, message: str) -> None:
        """Print the standard error envelope and exit with code 2 (invalid input)."""
        _print_json(_error_result("invalid_input", message))
        raise SystemExit(2)


def _print_json(data: dict[str, Any], stream: Any = None) -> None:
    """Print *data* as indented, non-ASCII-escaped JSON.

    Args:
        data: The object to print.
        stream: Where to print it. Defaults to stdout, which is where every
            result and every error envelope goes -- except for the stdio
            transport of ``serve``, whose stdout belongs to the protocol.
    """
    print(json.dumps(data, ensure_ascii=False, indent=2), file=stream or sys.stdout)


def _stream(to_stderr: bool) -> Any:
    """Return the stream an envelope should be printed to."""
    return sys.stderr if to_stderr else sys.stdout


def _configure_output_encoding() -> None:
    """Switch stdout and stderr to UTF-8, ignoring streams that cannot.

    Titles, abstracts and applicant names are frequently non-ASCII, and the
    Windows console defaults to a legacy code page that cannot encode them;
    without this, a successful lookup would end in a ``UnicodeEncodeError``.
    A stream that has no ``reconfigure`` (a captured or replaced stream) is
    left alone: printing must not depend on it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):  # pragma: no cover - platform dependent
            continue


def _io_error_message(exc: OSError) -> str:
    """Describe a failed file operation, naming the path and the reason.

    ``str(OSError)`` renders as ``[Errno 2] No such file or directory:
    '/x.json'`` only when the exception carries a filename, so the parts are
    assembled here instead of trusting that.
    """
    reason = exc.strerror or str(exc)
    paths = [str(path) for path in (exc.filename, exc.filename2) if path]
    if not paths:
        return reason
    return f"{reason}: {', '.join(paths)}"


def _error_result(error_type: str, message: str) -> dict[str, Any]:
    """Build the shared ``{"error": {"type", "message"}}`` envelope."""
    return {"error": {"type": error_type, "message": message}}


def _ops_client() -> OpsClient:
    """Return a fresh OpsClient for one command run.

    The credential check happens here, before the client is built, so an
    unconfigured OPS is reported without any request being attempted, per the
    CLI's ops_not_configured contract.

    Raises:
        ConfigError: If OPS is not configured.
    """
    if not ops_configured():
        raise ConfigError(service.OPS_NOT_CONFIGURED_MESSAGE)
    return OpsClient()


def _cache() -> Cache:
    """Return the file cache: the shared cache root plus this project's search cache."""
    return default_cache()


def _read_json_array(path: str, *, allow_stdin: bool = False) -> list[Any]:
    """Read *path* (or stdin, when *path* is ``"-"`` and allowed) as a JSON array.

    Raises:
        ValueError: If the content is not a JSON array (includes invalid
            JSON, since :class:`json.JSONDecodeError` is a ``ValueError``).
    """
    if allow_stdin and path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array in {path!r}, got {type(data).__name__}")
    return data


def _check_batch_size(items: Sequence[Any], label: str) -> None:
    """Apply the shared batch limits: count, element size and total size.

    Delegates to :func:`patent_checker.validation.validate_batch`, so the
    CLI refuses exactly what the MCP server refuses (at most
    :data:`patent_checker.service.MAX_BATCH_RECORDS` elements, each at most
    ``MAX_ITEM_CHARS`` long, all together at most ``MAX_PAYLOAD_CHARS``).

    Raises:
        ValueError: If any limit is broken (an ``InvalidInput``); the message
            names the offending index.
    """
    validate_batch(items, label=label, max_records=service.MAX_BATCH_RECORDS)


def _check_hits(hits: Sequence[Any], label: str) -> None:
    """Check a ``dedup`` payload: a bounded list of objects carrying ``"pub"``.

    Validating here keeps a malformed payload on the invalid_input path
    (exit code 2, JSON envelope) instead of surfacing as a ``KeyError``
    traceback from the dedup logic.

    Raises:
        ValueError: If the payload is too large, or an element is not an
            object or has no ``"pub"`` key. The message names the offending
            index.
    """
    _check_batch_size(hits, label)
    for index, hit in enumerate(hits):
        if not isinstance(hit, dict):
            raise ValueError(
                f'{label}[{index}] must be an object carrying "pub", got {type(hit).__name__}'
            )
        if "pub" not in hit:
            raise ValueError(f'{label}[{index}] has no "pub" key')


def _check_pub_strings(pubs: Sequence[Any], label: str) -> None:
    """Check a ``verify --input`` payload: a bounded list of publication strings.

    Raises:
        ValueError: If the payload is too large or an element is not a
            string. The message names the offending index.
    """
    _check_batch_size(pubs, label)
    for index, pub in enumerate(pubs):
        if not isinstance(pub, str):
            raise ValueError(
                f"{label}[{index}] must be a publication-number string, got {type(pub).__name__}"
            )


def _check_output_records(records: Sequence[Any], label: str) -> None:
    """Check a ``verify --output`` payload: strings or objects carrying ``"pub"``.

    Raises:
        ValueError: If the payload is too large, or an element is neither a
            string nor an object with a ``"pub"`` key. The message names the
            offending index.
    """
    _check_batch_size(records, label)
    for index, record in enumerate(records):
        if isinstance(record, str):
            continue
        if not isinstance(record, dict):
            raise ValueError(
                f"{label}[{index}] must be a publication-number string or an object "
                f'carrying "pub", got {type(record).__name__}'
            )
        if "pub" not in record:
            raise ValueError(f'{label}[{index}] has no "pub" key')


def _read_query_file(path: str) -> list[str]:
    """Return the CQL queries in *path*, one per non-empty, non-comment line."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


# --- search family -----------------------------------------------------


def _cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    """Run a published-data CQL search and return one page of hits."""
    with _ops_client() as client:
        return service.search(
            args.cql,
            begin=args.begin,
            end=args.end,
            client=client,
            cache=_cache(),
            refresh=args.refresh,
        )


def _cmd_search_biblio(args: argparse.Namespace) -> dict[str, Any]:
    """Run a biblio-constituent CQL search and return one page of full biblio records."""
    with _ops_client() as client:
        return service.search_biblio(
            args.cql,
            begin=args.begin,
            end=args.end,
            client=client,
            cache=_cache(),
            refresh=args.refresh,
        )


def _cmd_plan_check(args: argparse.Namespace) -> dict[str, Any]:
    """Measure the hit count of every candidate query in a search plan."""
    queries = list(args.queries)
    if args.file:
        queries.extend(_read_query_file(args.file))
    with _ops_client() as client:
        return service.plan_check(
            queries,
            max_total=args.max_total,
            client=client,
            cache=_cache(),
            refresh=args.refresh,
        )


# --- single-document lookups --------------------------------------------


def _cmd_biblio(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch bibliographic data for one publication."""
    with _ops_client() as client:
        return service.biblio(args.pub, client=client, cache=_cache(), refresh=args.refresh)


def _cmd_claims(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch claims: Google Patents first, OPS full text as a fallback for EP/WO.

    See the module docstring's exit-code note: an unavailable document is a
    normal (exit 0) result, not an error.
    """
    # An OPS client is only built when the fallback route could actually be
    # taken; the Google Patents route needs no credentials.
    if service.ops_fulltext_candidate(args.pub) and ops_configured():
        with OpsClient() as client:
            return service.claims(args.pub, client=client, cache=_cache(), refresh=args.refresh)
    return service.claims(args.pub, cache=_cache(), refresh=args.refresh)


def _cmd_legal(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch INPADOC legal-status events for one publication."""
    with _ops_client() as client:
        return service.legal(args.pub, client=client, cache=_cache(), refresh=args.refresh)


def _cmd_family(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch the simple patent family of one publication."""
    with _ops_client() as client:
        return service.family(args.pub, client=client, cache=_cache(), refresh=args.refresh)


# --- offline helpers -----------------------------------------------------


def _cmd_normalize(args: argparse.Namespace) -> dict[str, Any]:
    """Parse a publication number and return every spelling used across sources."""
    return service.normalize(args.text)


def _cmd_dedup(args: argparse.Namespace) -> dict[str, Any]:
    """Collapse a list of search hits into one record per patent family.

    Raises:
        ValueError: If the payload is not a well-formed, bounded list of
            hits (see :func:`_check_hits`).
    """
    hits = _read_json_array(args.path, allow_stdin=True)
    _check_hits(hits, "hits")
    return service.dedup(hits)


def _cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    """Cross-check a delegated batch's output against its input publication list.

    Raises:
        ValueError: If either payload is not well-formed or is over the
            shared batch limit.
    """
    input_pubs = _read_json_array(args.input)
    _check_pub_strings(input_pubs, "--input")
    output_records = _read_json_array(args.output)
    _check_output_records(output_records, "--output")
    return service.verify(input_pubs, output_records)


def _cmd_usage(args: argparse.Namespace) -> dict[str, Any]:
    """Summarize the local OPS request-header log."""
    return service.usage()


# --- serve -----------------------------------------------------------------


def _cmd_serve(args: argparse.Namespace) -> dict[str, Any] | None:
    """Print the operator notice, or run the MCP server until stopped.

    The server modules are imported here rather than at module load time, so
    a failure to import FastMCP or any other server-only dependency cannot
    affect the other subcommands.
    """
    from patent_checker.server import app as server_app
    from patent_checker.server import settings as server_settings

    if args.show_operator_notice:
        lang, text = server_settings.operator_notice_text(args.lang)
        if lang != args.lang:
            print(f"note: falling back to {lang}", file=sys.stderr)
        print(text)
        return None

    # With the stdio transport stdout is the protocol channel, so even an
    # error envelope has to go to stderr: a JSON object printed there would
    # corrupt the very first message the client reads.
    to_stderr = args.transport == "stdio"

    try:
        settings = server_settings.load_settings(
            transport=args.transport, host=args.host, port=args.port
        )
    except ConfigError as exc:
        _print_json(_error_result("config_error", str(exc)), stream=_stream(to_stderr))
        raise SystemExit(4) from exc

    try:
        server_app.run(settings)
    except OSError as exc:
        # For http this is practically always a busy port or an address the
        # machine does not have: a configuration problem, and not something
        # to show as a traceback from deep inside uvicorn. stdio binds
        # nothing, so an OSError there is a plain I/O failure.
        if settings.transport == "http":
            error_type, code = "config_error", 4
            message = _bind_failure_message(settings, exc)
        else:
            error_type, code = "io_error", 2
            message = f"the stdio server stopped: {_io_error_message(exc)}"
        _print_json(_error_result(error_type, message), stream=_stream(to_stderr))
        raise SystemExit(code) from exc
    return None


def _bind_failure_message(settings: Any, exc: OSError) -> str:
    """Explain a failure to start the server's listener, naming the port."""
    reason = exc.strerror or str(exc)
    return (
        f"could not listen on {settings.host}:{settings.port}: {reason}. "
        "Another process may already use that port: stop it, or choose a free one "
        "with --port (or $PATENT_CHECKER_SERVER_PORT)"
    )


# --- consent ---------------------------------------------------------------


def _cmd_consent_status(args: argparse.Namespace) -> dict[str, Any]:
    """Report the current consent state."""
    return consent.consent_status()


def _cmd_consent_show(args: argparse.Namespace) -> None:
    """Print the notice text for a language, falling back to English."""
    lang, text = consent.notice_text(args.lang)
    if lang != args.lang:
        print(f"note: falling back to {lang}", file=sys.stderr)
    print(text)
    return None


def _cmd_consent_record(args: argparse.Namespace) -> dict[str, Any]:
    """Record explicit consent to the current notice version."""
    scope = "project" if args.project else "user"
    path = consent.record_consent(language=args.lang, scope=scope)
    return {
        "recorded": True,
        "path": str(path),
        "notice_version": consent.NOTICE_VERSION,
        "language": args.lang,
    }


# --- cache -------------------------------------------------------------


def _cache_entry_summary(entry: CacheEntry) -> dict[str, Any]:
    """Return the JSON-serializable summary of one ``CacheEntry`` for ``cache clear``."""
    return {
        "kind": entry.kind,
        "key": entry.key,
        "root": entry.root,
        "path": str(entry.path),
        "fetched_at": entry.fetched_at,
        "expired": entry.expired,
        "broken": entry.broken,
        "problem": entry.problem,
    }


def _cmd_cache_status(args: argparse.Namespace) -> dict[str, Any]:
    """Report cache entry counts and byte totals, per kind, in total, and for the old layout."""
    cache = _cache()
    return {
        **cache.stats(),
        "legacy": cleanup.legacy_summary(config.data_base(), cache),
    }


def _cmd_cache_clear(args: argparse.Namespace) -> dict[str, Any]:
    """Select cache entries matching the given filters and, only with ``--yes``, delete them.

    Without ``--yes`` this is a dry run: nothing is deleted, and the
    selected entries are listed so a caller can review them first.

    Raises:
        ValueError: If ``--older-than`` is negative, an unknown ``--kind``
            is given, or ``--pub`` is not a parseable publication number
            (all propagated from :meth:`Cache.select`, except the
            ``--older-than`` sign check done here).
    """
    if args.older_than is not None and args.older_than < 0:
        raise ValueError(f"--older-than must not be negative, got {args.older_than}")
    older_than = timedelta(days=args.older_than) if args.older_than is not None else None

    cache = _cache()
    selected = cache.select(
        kinds=args.kind,
        older_than=older_than,
        pub=args.pub,
        expired=args.expired,
        broken=args.broken,
    )

    if not args.yes:
        return {
            "dry_run": True,
            "selected": len(selected),
            "bytes": sum(entry.size for entry in selected),
            "entries": [_cache_entry_summary(entry) for entry in selected],
        }

    removal = cache.remove(selected)
    return {
        "dry_run": False,
        "selected": len(selected),
        "removed": removal["removed"],
        "bytes": removal["bytes"],
        "paths": removal["paths"],
        "errors": removal["errors"],
    }


# --- clean -------------------------------------------------------------


def _cmd_clean(args: argparse.Namespace) -> dict[str, Any]:
    """Report, and only with ``--yes`` remove, what this project left on disk.

    Without ``--yes`` this is a dry run: the plan is listed and not one file
    is deleted. A path that could not be removed is reported under
    ``errors`` and does not fail the command, since the rest of the cleanup
    did happen.
    """
    data_base = config.data_base()
    user_data_dir = config.user_data_dir()
    cache = _cache()
    plan = cleanup.plan_cleanup(
        data_base=data_base,
        cache=cache,
        shared=args.shared,
        include_artifacts=args.include_artifacts,
        include_consent=args.include_consent,
        user_data_dir=user_data_dir,
        user_consent_path=consent.user_consent_path(),
    )
    result: dict[str, Any] = {
        "dry_run": not args.yes,
        "scope": {
            "project": str(data_base),
            # Named only when they are actually in scope, so a reader cannot
            # mistake a listed directory for one that will be touched.
            "shared": str(cache.shared) if args.shared else None,
            "user_data": str(user_data_dir) if args.shared else None,
        },
        **plan.to_dict(),
    }
    if not args.yes:
        return result

    removal = cleanup.execute(plan)
    # The removed paths themselves are left out: they are as long as the
    # plan, which the caller already has above.
    result["removed_files"] = removal["removed_files"]
    result["bytes"] = removal["bytes"]
    result["errors"] = removal["errors"]
    return result


# --- install ---------------------------------------------------------------


def _home() -> Path:
    """Return the user's home directory (indirection: tests replace this)."""
    return Path.home()


def _cwd() -> Path:
    """Return the working directory (indirection: tests replace this)."""
    return Path.cwd()


def _which(program: str) -> str | None:
    """Return the path of *program* on ``PATH`` (indirection: tests replace this)."""
    return shutil.which(program)


def _stdin_is_tty() -> bool:
    """Return whether questions can be asked (indirection: tests replace this)."""
    return sys.stdin.isatty()


def _prompt_secret(prompt: str) -> str:
    """Ask for a secret without echoing it (indirection: tests replace this)."""
    return getpass.getpass(prompt)


def _confirm(question: str) -> bool:
    """Ask *question* and return whether the answer was yes.

    Anything other than ``y``/``yes`` (case-insensitive) is a no, and so is
    a closed stdin: consent must be given, never assumed.
    """
    try:
        answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _default_lang(environ: Mapping[str, str]) -> str:
    """Return the notice language implied by the locale environment.

    ``LC_ALL`` wins over ``LANG`` (as POSIX prescribes); a value starting
    with ``ja`` selects Japanese and everything else English, which is the
    notice's controlling language.
    """
    locale = environ.get("LC_ALL") or environ.get("LANG") or ""
    return "ja" if locale.startswith("ja") else "en"


# Flags of ``install`` that only apply to an actual installation, each with
# the parser default that means "not given". ``--scope`` is deliberately
# absent: it selects which configuration files --list-agents reports on.
_INSTALL_ONLY_FLAGS: tuple[tuple[str, str, Any], ...] = (
    ("--agent", "agent", None),
    ("--lang", "lang", None),
    ("--url", "url", installer.DEFAULT_URL),
    ("--token-file", "token_file", None),
    ("--token-env", "token_env", False),
    ("--no-skill", "no_skill", False),
    ("--no-mcp", "no_mcp", False),
    ("--agree", "agree", False),
    ("--dry-run", "dry_run", False),
)


def _flags_ignored_by_list_agents(args: argparse.Namespace) -> list[str]:
    """Return the install-only flags that were given alongside ``--list-agents``."""
    return [
        flag
        for flag, attribute, unset in _INSTALL_ONLY_FLAGS
        if getattr(args, attribute, unset) != unset
    ]


def _cmd_install(args: argparse.Namespace) -> None:
    """Install the Skill and register the MCP server with the chosen agents.

    Prints a human-readable report rather than JSON (like ``consent show``),
    and asks for consent before anything is written; see the module
    docstring.
    """
    if args.list_agents:
        ignored = _flags_ignored_by_list_agents(args)
        if ignored:
            # Silently doing nothing with a flag the user typed reads as a
            # bug; --scope is not listed because listing does use it.
            print(
                f"warning: --list-agents only lists agents; {', '.join(ignored)} had no effect",
                file=sys.stderr,
            )
        print(installer.list_agents(home=_home(), cwd=_cwd(), which=_which, scope=args.scope))
        return None

    options = installer.InstallOptions(
        agents=tuple(args.agent) if args.agent else None,
        scope=args.scope,
        lang=args.lang or _default_lang(os.environ),
        url=args.url,
        token_file=Path(args.token_file) if args.token_file else None,
        token_env=args.token_env,
        skill=not args.no_skill,
        mcp=not args.no_mcp,
        agree=args.agree,
        dry_run=args.dry_run,
    )
    report = installer.install(
        options,
        home=_home(),
        cwd=_cwd(),
        environ=os.environ,
        stdin_is_tty=_stdin_is_tty(),
        confirm=_confirm,
        prompt_secret=_prompt_secret,
        which=_which,
        runner=None,
        out=sys.stdout,
    )
    print(installer.format_report(report))
    code = report.exit_code()
    if code != 0:
        raise SystemExit(code)
    return None


# --- argument parser -------------------------------------------------------


def _add_refresh_flag(parser: argparse.ArgumentParser) -> None:
    """Add the shared ``--refresh`` flag to a cache-backed subcommand."""
    parser.add_argument("--refresh", action="store_true", help="Ignore cached data and fetch again")


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser with every subcommand registered."""
    parser = _JsonArgumentParser(
        prog="patent-checker",
        description="Explore prior art and legal status for patent publications.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    search_parser = subparsers.add_parser("search", help="Run a published-data CQL search")
    search_parser.add_argument("cql", help="CQL query expression")
    search_parser.add_argument("--begin", type=int, default=1)
    search_parser.add_argument("--end", type=int, default=25)
    _add_refresh_flag(search_parser)
    search_parser.set_defaults(handler=_cmd_search)

    search_biblio_parser = subparsers.add_parser(
        "search-biblio", help="Run a CQL search returning full biblio per hit"
    )
    search_biblio_parser.add_argument("cql", help="CQL query expression")
    search_biblio_parser.add_argument("--begin", type=int, default=1)
    search_biblio_parser.add_argument("--end", type=int, default=25)
    _add_refresh_flag(search_biblio_parser)
    search_biblio_parser.set_defaults(handler=_cmd_search_biblio)

    plan_check_parser = subparsers.add_parser(
        "plan-check", help="Measure the hit count of candidate search queries"
    )
    plan_check_parser.add_argument(
        "queries", nargs="*", metavar="QUERY", help="CQL query expression"
    )
    plan_check_parser.add_argument("--file", help="File with one CQL query per line")
    plan_check_parser.add_argument("--max-total", type=int, default=None)
    _add_refresh_flag(plan_check_parser)
    plan_check_parser.set_defaults(handler=_cmd_plan_check)

    biblio_parser = subparsers.add_parser("biblio", help="Fetch bibliographic data")
    biblio_parser.add_argument("pub", help="Publication number")
    _add_refresh_flag(biblio_parser)
    biblio_parser.set_defaults(handler=_cmd_biblio)

    claims_parser = subparsers.add_parser(
        "claims", help="Fetch claims (GP, OPS fallback for EP/WO)"
    )
    claims_parser.add_argument("pub", help="Publication number")
    _add_refresh_flag(claims_parser)
    claims_parser.set_defaults(handler=_cmd_claims)

    legal_parser = subparsers.add_parser("legal", help="Fetch INPADOC legal-status events")
    legal_parser.add_argument("pub", help="Publication number")
    _add_refresh_flag(legal_parser)
    legal_parser.set_defaults(handler=_cmd_legal)

    family_parser = subparsers.add_parser("family", help="Fetch the simple patent family")
    family_parser.add_argument("pub", help="Publication number")
    _add_refresh_flag(family_parser)
    family_parser.set_defaults(handler=_cmd_family)

    normalize_parser = subparsers.add_parser("normalize", help="Normalize a publication number")
    normalize_parser.add_argument("text", help="Publication number in any supported spelling")
    normalize_parser.set_defaults(handler=_cmd_normalize)

    dedup_parser = subparsers.add_parser("dedup", help="Collapse search hits into families")
    dedup_parser.add_argument(
        "path", metavar="HITS_JSON_PATH", help="JSON array file, or '-' for stdin"
    )
    dedup_parser.set_defaults(handler=_cmd_dedup)

    verify_parser = subparsers.add_parser("verify", help="Cross-check a delegated batch's output")
    verify_parser.add_argument("--input", required=True, help="JSON array of input pub strings")
    verify_parser.add_argument("--output", required=True, help="JSON array of output records")
    verify_parser.set_defaults(handler=_cmd_verify)

    usage_parser = subparsers.add_parser("usage", help="Summarize the local OPS request-header log")
    usage_parser.set_defaults(handler=_cmd_usage)

    serve_parser = subparsers.add_parser("serve", help="Run the MCP server")
    serve_parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    serve_parser.add_argument("--host", default=None, help="Bind host (http only)")
    serve_parser.add_argument("--port", type=int, default=None, help="Bind port (http only)")
    serve_parser.add_argument(
        "--show-operator-notice",
        action="store_true",
        help="Print the operator notice and exit, instead of starting the server",
    )
    serve_parser.add_argument("--lang", default="en", help="Language of the operator notice")
    serve_parser.set_defaults(handler=_cmd_serve)

    _add_consent_parser(subparsers)
    _add_cache_parser(subparsers)
    _add_clean_parser(subparsers)
    _add_install_parser(subparsers)

    return parser


def _add_install_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``install`` subcommand and its client-side wiring flags."""
    install_parser = subparsers.add_parser(
        "install", help="Install the Agent Skill and register the MCP server"
    )
    install_parser.add_argument(
        "--agent",
        action="append",
        choices=(*installer.AGENT_KEYS, "all"),
        default=None,
        help="Agent to install for (repeatable; default: the detected agents)",
    )
    install_parser.add_argument(
        "--scope",
        choices=("user", "project"),
        default="user",
        help="Install for this user (default) or for the current project",
    )
    install_parser.add_argument(
        "--lang", default=None, help="Language of the notice (default: from LC_ALL/LANG)"
    )
    install_parser.add_argument(
        "--url", default=installer.DEFAULT_URL, help="MCP endpoint of the server"
    )
    install_parser.add_argument(
        "--token-file",
        default=None,
        help=f"File holding the server token (or set ${installer.ENV_TOKEN})",
    )
    install_parser.add_argument(
        "--token-env",
        action="store_true",
        help=f"Reference ${installer.ENV_TOKEN} in the configuration instead of writing the token",
    )
    install_parser.add_argument(
        "--no-skill", action="store_true", help="Do not install the Agent Skill"
    )
    install_parser.add_argument(
        "--no-mcp", action="store_true", help="Do not register the MCP server"
    )
    install_parser.add_argument(
        "--agree",
        action="store_true",
        help="Agree to the notice without being asked (required without a terminal)",
    )
    install_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would happen and write nothing"
    )
    install_parser.add_argument(
        "--list-agents", action="store_true", help="List the known agents and exit"
    )
    install_parser.set_defaults(handler=_cmd_install)


def _add_clean_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``clean`` subcommand and its opt-in scope flags."""
    clean_parser = subparsers.add_parser(
        "clean",
        help="Remove this project's cache and exploration residue (dry run unless --yes)",
    )
    clean_parser.add_argument(
        "--shared",
        action="store_true",
        help="Also the shared cache and the MCP server's data directory",
    )
    clean_parser.add_argument(
        "--include-artifacts",
        action="store_true",
        help="Also the data-directory entries this tool does not write (reports, notes)",
    )
    clean_parser.add_argument(
        "--include-consent",
        action="store_true",
        help="Also the recorded consent (the per-user record needs --shared too)",
    )
    clean_parser.add_argument(
        "--yes", action="store_true", help="Actually delete the listed paths (default: dry run)"
    )
    clean_parser.set_defaults(handler=_cmd_clean)


def _add_cache_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``cache`` subcommand and its ``status``/``clear`` children."""
    cache_parser = subparsers.add_parser("cache", help="Inspect and clear the file cache")
    cache_subparsers = cache_parser.add_subparsers(dest="cache_command", required=True)

    status_parser = cache_subparsers.add_parser(
        "status", help="Report cache entry counts and byte totals"
    )
    status_parser.set_defaults(handler=_cmd_cache_status)

    clear_parser = cache_subparsers.add_parser(
        "clear", help="Delete cache entries matching filters (dry run unless --yes)"
    )
    clear_parser.add_argument(
        "--kind", action="append", default=None, help="Restrict to this cache kind (repeatable)"
    )
    clear_parser.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help="Only entries at least this many days old",
    )
    clear_parser.add_argument(
        "--pub", default=None, help="Only the entry for this publication number"
    )
    clear_parser.add_argument("--expired", action="store_true", help="Only expired entries")
    clear_parser.add_argument("--broken", action="store_true", help="Only broken entries")
    clear_parser.add_argument(
        "--yes", action="store_true", help="Actually delete the selected entries (default: dry run)"
    )
    clear_parser.set_defaults(handler=_cmd_cache_clear)


def _add_consent_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``consent`` subcommand and its ``status``/``show``/``record`` children."""
    consent_parser = subparsers.add_parser("consent", help="Manage the legal-notice consent gate")
    consent_subparsers = consent_parser.add_subparsers(dest="consent_command", required=True)

    status_parser = consent_subparsers.add_parser("status", help="Report the current consent state")
    status_parser.set_defaults(handler=_cmd_consent_status)

    show_parser = consent_subparsers.add_parser("show", help="Print the notice text")
    show_parser.add_argument("--lang", default="en")
    show_parser.set_defaults(handler=_cmd_consent_show)

    record_parser = consent_subparsers.add_parser("record", help="Record explicit consent")
    record_parser.add_argument("--lang", required=True)
    record_parser.add_argument(
        "--project", action="store_true", help="Record for this project instead of this user"
    )
    record_parser.set_defaults(handler=_cmd_consent_record)


# --- entry point -------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and dispatch to a subcommand.

    With no arguments, prints help text and returns 0. See the module
    docstring for the output/exit-code contract.
    """
    _configure_output_encoding()
    # Read .env here, not in the individual commands: every subcommand has to
    # resolve the same data directory, and a command that skipped this step
    # would work on a different cache than the one before it.
    config.load_env()
    parser = _build_parser()

    # Resolve the effective argument list up front so the "no arguments"
    # case can be detected regardless of whether argv was passed explicitly.
    effective_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(effective_argv)

    handler = getattr(args, "handler", None)
    if not effective_argv or handler is None:
        parser.print_help()
        return 0

    try:
        result = handler(args)
    except installer.InstallerError as exc:
        # `install` writes a report for a person, so its stdout is not JSON
        # and an error envelope there would be unparseable anyway.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        _print_json(_error_result("invalid_input", str(exc)))
        return 2
    except ConfigError as exc:
        # OPS credential problems keep their own error type, since a Skill/
        # agent may special-case them (e.g. point the user at `consent`
        # instead of the operator's environment); every other ConfigError
        # (a malformed $PATENT_CHECKER_CACHE_TTL, in practice) is generic.
        error_type = (
            "ops_not_configured"
            if str(exc) == service.OPS_NOT_CONFIGURED_MESSAGE
            else "config_error"
        )
        _print_json(_error_result(error_type, str(exc)))
        return 4
    except httpx.HTTPError as exc:
        _print_json(_error_result("external_api_error", str(exc)))
        return 3
    except OSError as exc:
        # A missing input file, an unreadable cache entry, a full disk: the
        # caller gets the same envelope as every other failure instead of a
        # traceback it cannot parse.
        _print_json(_error_result("io_error", _io_error_message(exc)))
        return 2

    if result is not None:
        _print_json(result)
    return 0
