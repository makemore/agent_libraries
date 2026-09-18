"""Opt-in core client -> forced-command launcher -> actual Raise SDK stdio.

Run pytest with the library root .venv, setting RUN_RAISE_MCP_INTEROP=1 and
absolute non-secret RAISE_MCP_BACKEND / RAISE_MCP_PYTHON paths. No Raise imports
occur in this interpreter. Missing external checkout/SDK environment skips.
See packages/python/agent_runtime_core/deploy/mcp/README.md for the exact recipe.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_runtime_core.mcp import ssh_gateway
from agent_runtime_core.mcp.capabilities import ClientInfo
from agent_runtime_core.mcp.client import MCPClient
from agent_runtime_core.mcp.protocol import MCP_PROTOCOL_VERSION
from agent_runtime_core.mcp.transports.stdio import StdioTransport

_TOOLS = {
    "list_profiles", "list_contacts", "get_contact", "create_contact",
    "add_contact_method", "log_interaction", "contact_timeline", "get_interaction",
}

# Named test-only exception: separate processes need shared, disposable SQLite
# files instead of raise.settings.test's :memory: databases. Preserve BOTH test
# aliases and all other supported test defaults. Never import host/base settings,
# load .env, set USE_SQLITE, or change production database/authorization defaults.
# This is permanent test isolation, not a deployment workaround.
_SETTINGS = """
from importlib import import_module
from pathlib import Path
import sys

def deny_network(event, args):
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}:
        raise RuntimeError("Network unavailable in isolated MCP fixture")

sys.addaudithook(deny_network)
globals().update({k: v for k, v in vars(import_module("raise.settings.test")).items()
                  if k.isupper()})
if any(db["ENGINE"] != "django.db.backends.sqlite3" or db["NAME"] != ":memory:"
       for db in DATABASES.values()):
    raise RuntimeError("Expected isolated Raise SQLite test settings")
DATABASES = {alias: {**db, "NAME": str(Path(__file__).with_name(alias + ".sqlite3"))}
             for alias, db in DATABASES.items()}
"""

_BOOTSTRAP = """
import json
import os
import sys
from pathlib import Path
import django

os.umask(0o077)
django.setup()
from django.conf import settings
from django.core.management import call_command
from accounts.models import Profile, User

directory = Path(__file__).resolve().parent
for alias, db in settings.DATABASES.items():
    if db["ENGINE"] != "django.db.backends.sqlite3" or Path(db["NAME"]).parent != directory:
        raise RuntimeError("Fixture database isolation failed")
    call_command("migrate", database=alias, interactive=False, verbosity=0)

for email, charity, profile in [
    ("interop@example.test", "Interop Charity", "Interop Profile"),
    ("other@example.test", "Other Charity", "Other Profile"),
]:
    User.objects.create_user(email=email)
    call_command("bootstrap_raise", user_email=email, charity_name=charity,
                 profile_name=profile, verbosity=0)
call_command("issue_mcp_token", user_email="interop@example.test", token_file=sys.argv[1])
# An explicit metadata-only file, never the credential or a serialized user/token.
Path(sys.argv[2]).write_text(json.dumps({
    "profile_id": str(Profile.objects.get(display_name="Interop Profile").pk),
    "denied_profile_id": str(Profile.objects.get(display_name="Other Profile").pk),
}), encoding="utf-8")
"""

_VERIFY_AND_REVOKE = """
import django
django.setup()
from crm.models import Contact
from rest_framework.authtoken.models import Token

if Contact.objects.count() != 1:
    raise RuntimeError("Idempotency stored-state check failed")
if Contact.objects.get().display_name != "Interop donor":
    raise RuntimeError("Contact stored-state check failed")
# Named authorization scenario: documented admin revocation in the DISPOSABLE
# database only. No shared factory or production permissions are changed.
tokens = Token.objects.filter(user__email="interop@example.test")
if tokens.count() != 1:
    raise RuntimeError("Expected one fixture token")
tokens.delete()
"""


def _require(condition: bool, message: str) -> None:
    # Do not allow pytest assertion introspection to dump MCP payloads/locals.
    if not condition:
        pytest.fail(message, pytrace=False)


def _run_isolated(command: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    try:
        result = subprocess.run(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail(
            "Isolated Raise subprocess could not complete (output discarded)", pytrace=False,
        )
    return result.returncode


def _configured_raise() -> tuple[Path, Path]:
    if os.environ.get("RUN_RAISE_MCP_INTEROP") != "1":
        pytest.skip("Set RUN_RAISE_MCP_INTEROP=1 and explicit Raise checkout/interpreter paths")
    if os.name != "posix" or sys.platform not in {"darwin", "linux"}:
        pytest.skip("POSIX SSH stdio integration requires macOS or Linux")
    backend_value = os.environ.get("RAISE_MCP_BACKEND")
    python_value = os.environ.get("RAISE_MCP_PYTHON")
    if not backend_value or not python_value:
        pytest.skip("Explicit RAISE_MCP_BACKEND and RAISE_MCP_PYTHON paths are required")
    backend, interpreter = Path(backend_value), Path(python_value)
    _require(backend.is_absolute() and interpreter.is_absolute(), "Raise paths must be absolute")
    # Do NOT resolve the interpreter symlink: that would bypass its virtualenv.
    backend = backend.resolve()
    required = ["manage.py", "raise/settings/test.py", "crm/mcp_server.py"]
    if not all((backend / item).is_file() for item in required):
        pytest.skip("Optional Raise checkout is unavailable")
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        pytest.skip("Optional Raise interpreter is unavailable")
    if not (interpreter.parent.parent / "pyvenv.cfg").is_file():
        pytest.skip("A separate Raise virtualenv interpreter is required")
    return backend, interpreter


async def _round_trip(
    route_path: Path, env: dict[str, str], backend: Path,
    interpreter: Path, metadata: dict, verify_script: Path,
) -> None:
    transport = StdioTransport(
        [sys.executable, "-m", "agent_runtime_core.mcp.ssh_gateway", "--config", str(route_path)],
        cwd=str(route_path.parent), env={**env, "SSH_ORIGINAL_COMMAND": "raise-dev"},
        # Local test containment only, not a promise about remote SSH descendants.
        own_process_group=True, shutdown_timeout=1.0,
    )
    client = MCPClient(
        transport, client_info=ClientInfo(name="core-raise-interop", version="1"),
        default_timeout=20.0, close_timeout=5.0,
    )
    try:
        await transport.start()
        await client.start()
        initialized = await client.initialize()
        _require(
            initialized.protocol_version == MCP_PROTOCOL_VERSION, "Protocol negotiation failed",
        )
        _require(initialized.server_info.name == "Raise CRM", "Unexpected MCP server")
        tools = await client.list_tools()
        _require(len(tools.tools) == 8 and {tool.name for tool in tools.tools} == _TOOLS,
                 "Expected eight Raise MCP tools")
        _require(all("user_id" not in tool.input_schema.get("properties", {})
                     for tool in tools.tools), "Tool schema exposes caller identity selection")

        profiles = await client.call_tool("list_profiles")
        _require(not profiles.is_error and isinstance(profiles.structured_content, dict),
                 "list_profiles failed")
        listed = profiles.structured_content["profiles"]
        _require(len(listed) == 1 and listed[0]["profile_id"] == metadata["profile_id"],
                 "Profile grant scope mismatch")
        profile = {"profile_id": metadata["profile_id"]}
        arguments = {
            **profile, "contact": {"display_name": "Interop donor"},
            "idempotency_key": "core-raise-interop-contact",
        }
        created = await client.call_tool("create_contact", arguments)
        _require(not created.is_error and isinstance(created.structured_content, dict),
                 "create_contact failed")
        retried = await client.call_tool("create_contact", arguments)
        _require(not retried.is_error and retried.structured_content == created.structured_content,
                 "create_contact idempotency failed")
        contact_id = created.structured_content["id"]
        readback = await client.call_tool("get_contact", {**profile, "contact_id": contact_id})
        _require(not readback.is_error and isinstance(readback.structured_content, dict),
                 "get_contact readback failed")
        _require(readback.structured_content["id"] == contact_id
                 and readback.structured_content["display_name"] == "Interop donor",
                 "Contact readback mismatch")
        contacts = await client.call_tool("list_contacts", {**profile, "search": "Interop donor"})
        _require(not contacts.is_error and isinstance(contacts.structured_content, dict),
                 "list_contacts failed")
        _require(len(contacts.structured_content["items"]) == 1, "Duplicate contact readback")

        # Real existing profile in another charity, not just an invalid UUID.
        denied = await client.call_tool("list_contacts", {
            "profile_id": metadata["denied_profile_id"],
        })
        _require(denied.is_error, "Cross-profile authorization denial failed")
        status = await asyncio.to_thread(
            _run_isolated, [str(interpreter), str(verify_script)], cwd=backend, env=env,
        )
        _require(status == 0, "Stored-state verification / isolated token revocation failed")
        revoked = await client.call_tool("list_profiles")
        _require(revoked.is_error, "Live token revocation was not enforced on the same connection")
        revoked_read = await client.call_tool("get_contact", {**profile, "contact_id": contact_id})
        _require(revoked_read.is_error, "Revoked token could still read the contact")
    finally:
        await client.close()
    _require(transport.is_closed and transport._proc is not None
             and transport._proc.returncode is not None, "Local Raise child was not reaped")


def test_raise_stdio_interop_isolated_sqlite():
    backend, interpreter = _configured_raise()
    # TemporaryDirectory removes the issued credential even on assertion failure;
    # unlike pytest tmp_path alone it does not retain tokens in failure artifacts.
    with TemporaryDirectory(prefix="core-raise-mcp-") as temporary:
        directory = Path(temporary).resolve()
        directory.chmod(0o700)
        settings_file = directory / "core_raise_interop_settings.py"
        settings_file.write_text(textwrap.dedent(_SETTINGS), encoding="utf-8")
        core_path = Path(ssh_gateway.__file__).resolve().parents[2]
        # Explicit non-secret allowlist; never merge os.environ, load .env or
        # copy host DB/cloud credentials. Python selectors are trusted test setup.
        env = {
            "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
            "DJANGO_SETTINGS_MODULE": "core_raise_interop_settings",
            "PYTHONPATH": os.pathsep.join(map(str, (directory, core_path, backend))),
        }
        available = _run_isolated(
            [str(interpreter), "-c", "import django; from mcp.server import MCPServer"],
            cwd=directory, env=env,
        )
        if available != 0:
            pytest.skip("Optional Raise Django / matching MCP SDK environment is unavailable")

        token_path = directory / "client.token"
        metadata_path = directory / "fixture_ids.json"
        bootstrap = directory / "bootstrap.py"
        bootstrap.write_text(textwrap.dedent(_BOOTSTRAP), encoding="utf-8")
        status = _run_isolated(
            [str(interpreter), str(bootstrap), str(token_path), str(metadata_path)],
            cwd=backend, env=env,
        )
        _require(status == 0, "Isolated Raise bootstrap failed (output discarded)")
        token_stat = token_path.lstat()  # Never read/export the credential in the root client.
        _require(stat.S_ISREG(token_stat.st_mode) and stat.S_IMODE(token_stat.st_mode) == 0o600
                 and token_stat.st_uid == os.geteuid(), "Fixture token is not owner-only")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        route_path = directory / "routes.json"
        with route_path.open("x", encoding="utf-8") as route:
            route_path.chmod(0o600)
            json.dump({"services": {"raise-dev": {
                "command": [str(interpreter), str(backend / "manage.py"), "run_mcp",
                            "--token-file", str(token_path)],
                "cwd": str(backend),
            }}}, route)
        verify_script = directory / "verify_and_revoke.py"
        verify_script.write_text(textwrap.dedent(_VERIFY_AND_REVOKE), encoding="utf-8")
        try:
            asyncio.run(_round_trip(route_path, env, backend, interpreter, metadata, verify_script))
        except Exception:
            pytest.fail("Isolated Raise MCP round trip failed (payloads withheld)", pytrace=False)