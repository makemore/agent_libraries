"""Private, metadata-only profiles and noninteractive credential loading.

Profiles contain only the documented fields; omitted/null optional fields use
Token authentication, no HTTP exception, and no assumed API mounts. The first
profile becomes selected. All profiles are validated on every load, including
unselected profiles. config.json is written atomically with mode 0600 inside an
owned mode-0700 directory; symlinks and insecure existing files are rejected.

Credentials are never persisted here. Source precedence is explicit stdin,
profile credential_file, then AGENTCTL_TOKEN, without fallback on invalid input.
Tokens are at most 8192 printable ASCII characters, with no whitespace. One
terminal LF/CRLF from a file or stdin is accepted. Credential files must be owned, regular,
private (0400/0600), and have no symlink components. Relative credential paths
are made absolute when adding a profile. Permission checks fail closed without
POSIX ownership support. Diagnostics never interpolate input.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .errors import CLIError

_PROFILE_KEYS = frozenset({
    "origin", "runtime_mount", "studio_mount", "identity_path", "auth_scheme",
    "allow_http_loopback", "credential_file",
})
_TOKEN_LIMIT = 8192
_CONFIG_LIMIT = 1024 * 1024


def _configuration_error():
    return CLIError("Invalid or unavailable local configuration.", code="configuration", exit_code=2)


def _token_error():
    return CLIError("A valid credential is required.", code="auth", exit_code=3)


def _ascii_text(value):
    return isinstance(value, str) and bool(value) and all(33 <= ord(c) <= 126 for c in value)


def validate_origin(origin, allow_http_loopback=False) -> str:
    """Return a canonical HTTPS origin; HTTP requires explicit literal loopback."""
    if (type(allow_http_loopback) is not bool or not _ascii_text(origin)
            or len(origin) > 2048 or any(c in origin for c in "\\%@?#")):
        raise _configuration_error()
    try:
        parts = urlsplit(origin)
        host = parts.hostname
        port = parts.port
        if (parts.scheme not in {"https", "http"} or not host
                or parts.path not in {"", "/"} or not re.fullmatch(
                    r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?", parts.netloc
                )):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            if (len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ) or re.fullmatch(r"[0-9.]+", host)):
                raise ValueError
        if "[" in parts.netloc and not isinstance(address, ipaddress.IPv6Address):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parts.scheme == "http" and not (
            allow_http_loopback and (host == "localhost" or (
                address and address.is_loopback and not getattr(address, "ipv4_mapped", None)
            ))
        ):
            raise ValueError
        canonical_host = str(address) if address else host
        if isinstance(address, ipaddress.IPv6Address):
            canonical_host = f"[{canonical_host}]"
        default_port = 443 if parts.scheme == "https" else 80
        suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parts.scheme}://{canonical_host}{suffix}"
    except (ValueError, UnicodeError):
        raise _configuration_error() from None


def _validate_path(path) -> str:
    if (not _ascii_text(path) or len(path) > 8192 or not path.startswith("/")
            or "//" in path or any(c in path for c in "\\%?#:")
            or any(segment in {".", ".."} for segment in path.split("/"))):
        raise _configuration_error()
    return path


def validate_mount(path) -> str:
    """Validate an absolute, unescaped mount path with a required final slash."""
    result = _validate_path(path)
    if not result.endswith("/"):
        raise _configuration_error()
    return result


def _absolute_path(value, *, allow_relative=False):
    if (not isinstance(value, (str, Path)) or not str(value)
            or not str(value).isprintable()):
        raise _configuration_error()
    path = Path(value)
    if ".." in path.parts or (not path.is_absolute() and not allow_relative):
        raise _configuration_error()
    return path.absolute()


def _no_symlinks(path):
    for component in (*reversed(path.parents), path):
        try:
            if stat.S_ISLNK(component.lstat().st_mode):
                raise _configuration_error()
        except FileNotFoundError:
            continue


def _private_stat(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    permissions = stat.S_IMODE(info.st_mode)
    allowed = {0o700} if directory else {0o400, 0o600}
    if (not kind(info.st_mode) or permissions not in allowed
            or not hasattr(os, "getuid") or info.st_uid != os.getuid()
            or (not directory and info.st_nlink != 1)):
        raise _configuration_error()


def _check_private_file(path):
    _no_symlinks(path)
    _private_stat(path.lstat())


def _read_private(path, limit):
    _check_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        _private_stat(os.fstat(fd))
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise _configuration_error()
        return value
    finally:
        os.close(fd)


def _profile(profile, *, creating=False, check_file=True):
    if (not isinstance(profile, dict) or not profile.keys() <= _PROFILE_KEYS
            or "origin" not in profile):
        raise _configuration_error()
    result = {key: profile.get(key) for key in _PROFILE_KEYS}
    allow_http = result["allow_http_loopback"]
    result["allow_http_loopback"] = False if allow_http is None else allow_http
    result["origin"] = validate_origin(result["origin"], result["allow_http_loopback"])
    scheme = result["auth_scheme"]
    if scheme is not None and scheme not in ("Token", "Bearer"):
        raise _configuration_error()
    result["auth_scheme"] = scheme or "Token"
    for key in ("runtime_mount", "studio_mount"):
        if result[key] is not None:
            result[key] = validate_mount(result[key])
    if result["identity_path"] is not None:
        result["identity_path"] = _validate_path(result["identity_path"])
    if result["credential_file"] is not None:
        path = _absolute_path(result["credential_file"], allow_relative=creating)
        if check_file:
            _check_private_file(path)
        result["credential_file"] = str(path)
    return result


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
        raise _configuration_error()
    return name


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _configuration_error()
        result[key] = value
    return result


def default_directory() -> Path:
    """Use AGENTCTL_CONFIG_DIR, else the OS user config location; never cwd.

    Explicit config and XDG paths must be absolute. No files are read here.
    """
    try:
        if "AGENTCTL_CONFIG_DIR" in os.environ:
            return _absolute_path(os.environ["AGENTCTL_CONFIG_DIR"])
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA")
            return (_absolute_path(base) if base else Path.home() / "AppData" / "Local") / "agentctl"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "agentctl"
        base = os.environ.get("XDG_CONFIG_HOME")
        return (_absolute_path(base) if base else Path.home() / ".config") / "agentctl"
    except (OSError, ValueError, RuntimeError):
        raise _configuration_error() from None


class Store:
    """A local config.json store. Reading an absent store does not create it."""

    def __init__(self, directory: Path):
        self.directory = _absolute_path(directory, allow_relative=True)
        self.path = self.directory / "config.json"

    def _directory(self, *, create=False):
        _no_symlinks(self.directory)
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.directory.exists():
            return False
        _private_stat(self.directory.lstat(), directory=True)
        return True

    def _load(self):
        empty = {"version": 1, "selected": None, "profiles": {}}
        try:
            if not self._directory():
                return empty
            # lexists notices broken symlinks, which must not be treated as absent.
            if not os.path.lexists(self.path):
                return empty
            data = json.loads(_read_private(self.path, _CONFIG_LIMIT).decode("utf-8"),
                              object_pairs_hook=_unique_object)
            if (not isinstance(data, dict) or set(data) != {"version", "selected", "profiles"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["profiles"], dict)):
                raise _configuration_error()
            # Profile inspection/selection must still work after a credential is
            # revoked or its file removed. Validate the selected source on use.
            profiles = {_name(name): _profile(value, check_file=False)
                        for name, value in data["profiles"].items()}
            selected = data["selected"]
            if selected is not None and _name(selected) not in profiles:
                raise _configuration_error()
            return {"version": 1, "selected": selected, "profiles": profiles}
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise _configuration_error() from None

    def _write(self, data):
        temporary = None
        try:
            self._directory(create=True)
            if os.path.lexists(self.path):
                _check_private_file(self.path)
            content = json.dumps(data, ensure_ascii=True, sort_keys=True).encode("utf-8")
            if len(content) > _CONFIG_LIMIT:
                raise _configuration_error()
            fd, temporary = tempfile.mkstemp(prefix=".config-", dir=self.directory)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, ValueError, UnicodeError):
            raise _configuration_error() from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def add(self, name, profile: dict) -> None:
        name = _name(name)
        try:
            profile = _profile(profile, creating=True)
        except (OSError, ValueError, UnicodeError):
            raise _configuration_error() from None
        data = self._load()
        if name in data["profiles"]:
            raise CLIError("Profile already exists; use a new name. Existing profiles are not overwritten.")
        first = not data["profiles"]
        data["profiles"][name] = profile
        if first:
            data["selected"] = name
        self._write(data)

    def use(self, name) -> None:
        name = _name(name)
        data = self._load()
        if name not in data["profiles"]:
            raise _configuration_error()
        data["selected"] = name
        self._write(data)

    def list(self) -> list[dict]:
        data = self._load()
        return [dict({key: value for key, value in profile.items() if key != "credential_file"},
                     name=name, selected=name == data["selected"])
                for name, profile in sorted(data["profiles"].items())]

    def get(self, name=None) -> dict:
        data = self._load()
        selected = data["selected"] if name is None else _name(name)
        if selected is None or selected not in data["profiles"]:
            raise CLIError("No matching profile. Use profiles add/list/use or --profile.")
        return dict(data["profiles"][selected])


def _validate_token(token) -> str:
    if not _ascii_text(token) or len(token) > _TOKEN_LIMIT:
        raise _token_error()
    return token


def _read_stdin(stream):
    chunks = []
    length = 0
    while length <= _TOKEN_LIMIT + 2:
        chunk = stream.read(_TOKEN_LIMIT + 3 - length)
        if type(chunk) not in (str, bytes):
            raise _token_error()
        if not chunk:
            break
        text = chunk.decode("ascii") if isinstance(chunk, bytes) else chunk
        chunks.append(text)
        length += len(text)
    return "".join(chunks)


def load_token(profile, *, token_stdin=False, stdin=None, environ=None) -> str:
    """Read one bounded credential from the highest-priority selected source."""
    try:
        profile = _profile(profile, check_file=False)
        if type(token_stdin) is not bool:
            raise _configuration_error()
        if token_stdin:
            stream = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
            token = _read_stdin(stream)
        elif profile["credential_file"] is not None:
            token = _read_private(Path(profile["credential_file"]), _TOKEN_LIMIT + 2)
        else:
            environment = os.environ if environ is None else environ
            token = environment.get("AGENTCTL_TOKEN")
        if isinstance(token, bytes):
            token = token.decode("ascii")
        if isinstance(token, str) and (token_stdin or profile["credential_file"] is not None):
            token = token[:-2] if token.endswith("\r\n") else token.removesuffix("\n")
        return _validate_token(token)
    except (OSError, ValueError, UnicodeError):
        raise _token_error() from None
