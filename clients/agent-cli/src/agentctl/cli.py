"""Command routing and presentation only; the REST server owns product policy."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import Store, default_directory, load_token
from .errors import CLIError
from .resources import ReadAPI
from .transport import Client


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default diagnostics echo rejected arguments, possibly a
        # mistakenly pasted credential. Help is public; parse failures are not.
        raise CLIError("Invalid arguments. Run studio --help or the command's --help.")


def parser():
    common = Parser(add_help=False, argument_default=argparse.SUPPRESS, allow_abbrev=False)
    common.add_argument("--json", action="store_true", help="Stable JSON output (errors on stderr)")
    common.add_argument("--config-dir", type=Path, help="Private directory for non-secret profiles")
    common.add_argument("--profile", help="Use a named profile without changing the selection")
    common.add_argument("--token-stdin", action="store_true", help="Read a provisioned token from stdin")
    common.add_argument("--timeout", type=float, help="Socket timeout in seconds (default 15, maximum 120)")

    def child(subcommands, name, **kwargs):
        return subcommands.add_parser(name, parents=[common], allow_abbrev=False, **kwargs)

    root = Parser(prog="studio", parents=[common], allow_abbrev=False,
                  description="Thin, metadata-only REST client for runtime and Studio.",
                  epilog="No server writes or login bypass. SDLC token access is not yet supported.")
    root.add_argument("--version", action="version", version=f"studio {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    profiles = child(commands, "profiles", help="Manage non-secret connection profiles")
    actions = profiles.add_subparsers(dest="action", required=True)
    add = child(actions, "add", help="Add a profile; existing profiles are never overwritten")
    add.add_argument("name")
    add.add_argument("--origin", required=True, help="HTTPS origin without credentials or API path")
    add.add_argument("--runtime-mount", help="Runtime API root, e.g. /api/agent-runtime/")
    add.add_argument("--studio-mount", help="Studio API root, e.g. /studio/api/ (NOT /studio/)")
    add.add_argument("--identity-path", help="Optional host-approved read-only identity endpoint")
    add.add_argument("--auth-scheme", choices=("Token", "Bearer"), default="Token")
    add.add_argument("--credential-file", help="Existing owner-only file; contents are never copied")
    add.add_argument("--allow-http-loopback", action="store_true",
                     help="Explicit local-development HTTP exception, loopback only")
    child(actions, "list", help="List safe profile metadata; no credential paths or contents")
    use = child(actions, "use", help="Select an existing profile")
    use.add_argument("name")
    child(commands, "whoami", help="GET the profile's explicitly configured identity endpoint")
    child(commands, "status", help="Probe configured read endpoints; not capability discovery")

    for group, names in (("runtime", ("agents", "runs")), ("studio", ("projects", "threads"))):
        group_parser = child(commands, group, help=f"Read existing {group} REST APIs")
        resources = group_parser.add_subparsers(dest="resource", required=True)
        for name in names:
            resource = child(resources, name, help=f"Inspect {name} metadata")
            operations = resource.add_subparsers(dest="action", required=True)
            listing = child(operations, "list", help="Read one page, or follow bounded pagination")
            if name != "projects":
                listing.add_argument("--all", dest="all_pages", action="store_true")
                listing.add_argument("--max-pages", type=int, default=10)
            if group == "runtime":
                listing.add_argument("--page", type=int)
                listing.add_argument("--page-size", type=int, help="If supported by the host paginator")
                listing.add_argument("--system" if name == "agents" else "--agent-key")
            if name == "threads":
                listing.add_argument("--project-id")
                listing.add_argument("--archived", choices=("true", "false"))
                listing.add_argument("--limit", type=int)
                listing.add_argument("--offset", type=int)
            if name != "runs":
                detail = child(operations, "get", help="Read metadata for an explicit ID (agent slug)")
                detail.add_argument("identifier")
    return root


def _execute(args, *, stdin, environ):
    store = Store(args.config_dir if hasattr(args, "config_dir") else default_directory())
    if args.command == "profiles":
        if args.action == "add":
            keys = ("origin", "runtime_mount", "studio_mount", "identity_path", "auth_scheme",
                    "credential_file", "allow_http_loopback")
            store.add(args.name, {key: getattr(args, key, None) for key in keys})
        elif args.action == "use":
            store.use(args.name)
        return {"items": store.list()}, 0

    profile = store.get(getattr(args, "profile", None))
    token = load_token(profile, token_stdin=getattr(args, "token_stdin", False),
                       stdin=stdin, environ=environ)
    client = Client(profile["origin"], token, scheme=profile["auth_scheme"],
                    allow_http_loopback=profile["allow_http_loopback"],
                    timeout=getattr(args, "timeout", 15))
    api = ReadAPI(client, runtime_mount=profile["runtime_mount"],
                  studio_mount=profile["studio_mount"], identity_path=profile["identity_path"])
    if args.command == "whoami":
        return api.whoami(), 0
    if args.command == "status":
        result = api.status()
        available = all(row["availability"] == "observed_available" for row in result["observations"])
        return result, 0 if available else 7
    if args.action == "get":
        return api.get(args.command, args.resource, args.identifier), 0
    query = {key: getattr(args, key) for key in (
        "page", "page_size", "system", "agent_key", "project_id", "archived", "limit", "offset",
    ) if getattr(args, key, None) is not None}
    return api.list(args.command, args.resource, query=query,
                    all_pages=getattr(args, "all_pages", False),
                    max_pages=getattr(args, "max_pages", 10)), 0


def _cell(value):
    if value is None:
        return "-"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=True)
    return "".join(char if char.isprintable() else " " for char in str(value))[:100]


def _display(data, stream, *, as_json):
    if as_json:
        print(json.dumps({"schema_version": 1, "data": data}, ensure_ascii=True, allow_nan=False), file=stream)
        return
    if "items" in data or "observations" in data:
        rows = data.get("items", data.get("observations", []))
        if not rows:
            print("No results.", file=stream)
        else:
            # Keep tables compact; JSON retains the complete metadata allowlist.
            preferred = ("selected", "name", "id", "slug", "title", "agent_key", "status",
                         "origin", "group", "resource", "availability", "visibility")
            columns = [key for key in preferred if any(key in row for row in rows)]
            cells = [[_cell(row.get(key)) for key in columns] for row in rows]
            widths = [max(len(key), *(len(row[i]) for row in cells)) for i, key in enumerate(columns)]
            print("  ".join(key.upper().ljust(width) for key, width in zip(columns, widths)), file=stream)
            for row in cells:
                print("  ".join(value.ljust(width) for value, width in zip(row, widths)), file=stream)
        if data.get("has_more"):
            print("More results available; use --all with an appropriate --max-pages bound.", file=stream)
        if "scope" in data:
            print(_cell(data["scope"]), file=stream)
    else:
        for key, value in data.items():
            print(f"{key}: {_cell(value)}", file=stream)


def main(argv=None, *, stdin=None, stdout=None, stderr=None, environ=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    as_json = "--json" in argv
    try:
        args = parser().parse_args(argv)
        data, code = _execute(args, stdin=stdin, environ=environ)
        _display(data, stdout, as_json=as_json)
        return code
    except CLIError as error:
        if as_json:
            print(json.dumps({"schema_version": 1, **error.payload()}, ensure_ascii=True), file=stderr)
        else:
            print(f"studio: {error}", file=stderr)
        return error.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except (OSError, UnicodeError, ValueError):
        # File paths and system exceptions can contain secrets; never echo them.
        error = CLIError("Local input/output failed.", code="io", exit_code=5)
        if as_json:
            print(json.dumps({"schema_version": 1, **error.payload()}), file=stderr)
        else:
            print(f"studio: {error}", file=stderr)
        return error.exit_code
