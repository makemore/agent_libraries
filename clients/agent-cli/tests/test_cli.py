"""CLI routing, output, and real HTTP over a private ephemeral loopback server."""

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import tomllib
from agentctl import __version__
from agentctl.cli import main, parser
from agentctl.config import Store


@pytest.fixture
def invoke(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / "profiles"
    monkeypatch.setenv("AGENTCTL_CONFIG_DIR", str(directory))
    monkeypatch.delenv("AGENTCTL_TOKEN", raising=False)

    def run(*args, token="synthetic-cli-credential"):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(args), stdout=out, stderr=err, environ={"AGENTCTL_TOKEN": token})
        return code, out.getvalue(), err.getvalue()

    run.directory = directory
    return run


@pytest.fixture
def server():
    # Named loopback HTTP exception: transport framing is tested without TLS or
    # real credentials. All ordinary profiles continue to require HTTPS.
    paths = []
    replies = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            status, payload = replies.get(self.path, (404, {"error": "synthetic-server-secret"}))
            if self.headers.get("Authorization") != "Token synthetic-cli-credential":
                status, payload = 401, {"error": "synthetic-server-secret"}
            content = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass  # Never log request headers, targets or credentials.

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", replies, paths
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def connect(invoke, origin):
    code, _, error = invoke("profiles", "add", "local", "--origin", origin,
                            "--runtime-mount", "/custom/runtime/", "--studio-mount", "/studio/api/",
                            "--identity-path", "/identity/", "--allow-http-loopback")
    assert code == 0 and not error


def test_profiles_are_local_metadata_only_and_no_overwrite(invoke):
    code, output, error = invoke("--json", "profiles", "list")
    assert code == 0 and not error
    assert json.loads(output) == {"schema_version": 1, "data": {"items": []}}
    assert not invoke.directory.exists()
    assert invoke("profiles", "add", "remote", "--origin", "https://api.example.test")[0] == 0
    previous = (invoke.directory / "config.json").read_bytes()
    assert invoke("profiles", "add", "remote", "--origin", "https://other.example.test")[0] == 2
    assert (invoke.directory / "config.json").read_bytes() == previous
    assert b"synthetic-cli-credential" not in previous


@pytest.mark.parametrize("args", [
    [], ["--token", "synthetic-mistaken-secret"], ["--timeout", "synthetic-mistaken-secret"],
    ["runtime", "runs", "get", "synthetic-mistaken-secret"],
    ["studio", "threads", "list", "--archived", "synthetic-mistaken-secret"],
    ["profiles", "add", "x", "--origin", "https://api.example.test", "--auth-scheme", "secret"],
    ["--prof", "x", "profiles", "list"],
])
def test_argument_errors_are_safe_json_and_do_not_read_config(invoke, args):
    code, output, error = invoke(*args, "--json")
    assert code == 2 and not output
    assert json.loads(error)["error"]["code"] == "configuration"
    assert "synthetic" not in error
    assert not invoke.directory.exists()


@pytest.mark.parametrize("args", [
    ["--profile", "chosen", "--json", "runtime", "agents", "list"],
    ["runtime", "--profile", "chosen", "agents", "--json", "list"],
    ["runtime", "agents", "list", "--profile", "chosen", "--json"],
])
def test_global_options_work_at_any_command_level(args):
    options = parser().parse_args(args)
    assert options.profile == "chosen" and options.json is True


def test_real_http_cli_projects_and_metadata_redaction(invoke, server):
    origin, replies, paths = server
    connect(invoke, origin)
    replies["/studio/api/workspace/projects/"] = (200, [{
        "id": "host:7", "name": "Project synthetic-cli-credential\u001b\n\u202e",
        "target": {"private_key": "synthetic-server-secret"}, "revision": 0,
    }])
    code, output, error = invoke("studio", "projects", "list", "--json")
    assert code == 0 and not error
    rows = json.loads(output)["data"]["items"]
    assert rows[0]["id"] == "host:7" and "target" not in rows[0]
    assert "synthetic" not in output and "\x1b" not in rows[0]["name"]
    assert paths == ["/studio/api/workspace/projects/"]
    code, output, error = invoke("studio", "projects", "list")
    assert code == 0 and "NAME" in output and not error
    assert "synthetic" not in output and "\x1b" not in output


def test_real_http_cli_thread_filters_and_pagination(invoke, server):
    origin, replies, paths = server
    connect(invoke, origin)
    query = "?project_id=host%3A7&archived=false&limit=1"
    base = "/studio/api/workspace/threads/"
    replies[base + query] = (200, {"results": [{"id": "one", "title": "First"}], "count": 2, "has_more": True})
    replies[base + query + "&offset=1"] = (200, {"results": [{"id": "two"}], "count": 2, "has_more": False})
    code, output, error = invoke("studio", "threads", "list", "--project-id", "host:7",
                                "--archived", "false", "--limit", "1", "--all", "--json")
    assert code == 0 and not error
    assert json.loads(output)["data"]["pages"] == 2
    assert len(paths) == 2


def test_auth_failure_is_not_anonymous_retry_and_omits_body(invoke, server):
    origin, replies, paths = server
    connect(invoke, origin)
    replies["/custom/runtime/agents/"] = (200, [])
    code, output, error = invoke("runtime", "agents", "list", "--json", token="wrong-synthetic-credential")
    assert code == 3 and not output and len(paths) == 1
    assert json.loads(error)["error"]["status"] == 401
    assert "synthetic" not in error


def test_status_failure_is_nonzero_and_identity_is_explicit(invoke, server):
    origin, replies, paths = server
    connect(invoke, origin)
    replies["/custom/runtime/agents/?page_size=1"] = (200, [])
    replies["/identity/"] = (200, {"pk": 7, "email": "cli@example.test", "config": {"secret": "omitted"}})
    code, output, error = invoke("status", "--json")
    assert code == 7 and not error and len(paths) == 3
    assert len(json.loads(output)["data"]["observations"]) == 3
    code, output, error = invoke("whoami", "--json")
    assert code == 0 and not error
    assert json.loads(output)["data"] == {"id": 7, "email": "cli@example.test"}


def test_cli_stdin_credential_is_not_saved(invoke, monkeypatch):
    invoke("profiles", "add", "remote", "--origin", "https://api.example.test", "--runtime-mount", "/api/")
    from agentctl import cli

    def read(self, path, query=None):
        assert self.redact("synthetic-stdin-value") == "[REDACTED]"
        return []

    monkeypatch.setattr(cli.Client, "get", read)
    out, err = io.StringIO(), io.StringIO()
    code = main(["runtime", "agents", "list", "--token-stdin", "--json"],
                stdin=io.StringIO("synthetic-stdin-value"), stdout=out, stderr=err, environ={})
    assert code == 0 and not err.getvalue()
    assert "synthetic" not in (invoke.directory / "config.json").read_text()


def test_local_configuration_used_not_cwd(invoke, tmp_path, monkeypatch):
    Store(invoke.directory).add("remote", {"origin": "https://api.example.test"})
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text('{"token":"synthetic-untrusted-config"}')
    code, output, error = invoke("profiles", "list", "--json")
    assert code == 0 and not error and "synthetic" not in output


@pytest.mark.parametrize("arguments", [["--help"], ["runtime", "agents", "--help"]])
def test_help_uses_studio_command(arguments, capsys):
    with pytest.raises(SystemExit) as caught:
        main(arguments)
    assert caught.value.code == 0
    output = capsys.readouterr()
    assert output.out.startswith("usage: studio")
    assert "agentctl" not in output.out and not output.err


def test_version_uses_studio_command(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out == f"studio {__version__}\n"


def test_argument_errors_use_studio_command(invoke):
    code, output, error = invoke("unsupported-command")
    assert code == 2 and not output
    assert error.startswith("studio: ") and "studio --help" in error
    assert "agentctl" not in error


def test_only_studio_console_entrypoint_is_declared():
    metadata = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert metadata["project"]["scripts"] == {"studio": "agentctl:main"}

