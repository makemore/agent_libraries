"""Isolated profile/token tests: only temporary metadata and synthetic tokens."""

import io
import json
import os
import stat
from pathlib import Path

import pytest
from agentctl import config
from agentctl.config import (
    Store,
    default_directory,
    load_token,
    validate_mount,
    validate_origin,
)
from agentctl.errors import CLIError


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in ("AGENTCTL_TOKEN", "AGENTCTL_CONFIG_DIR", "XDG_CONFIG_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path.resolve() / "home"))


@pytest.fixture
def profile():
    return {"origin": "https://api.example.test"}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path.resolve() / "profiles")


def private_file(path, content=b"synthetic-file-token"):
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def assert_safe(error, *, code="configuration", exit_code=2):
    assert error.code == code
    assert error.exit_code == exit_code
    assert str(error) in {
        "Invalid or unavailable local configuration.", "A valid credential is required.",
    }
    assert error.status is None


@pytest.mark.parametrize(("origin", "expected"), [
    ("https://EXAMPLE.test/", "https://example.test"),
    ("HTTPS://example.test:443", "https://example.test"),
    ("https://example.test:8443/", "https://example.test:8443"),
    ("https://127.0.0.1", "https://127.0.0.1"),
    ("https://[0:0:0:0:0:0:0:1]:443/", "https://[::1]"),
])
def test_canonical_https_origin(origin, expected):
    assert validate_origin(origin) == expected


@pytest.mark.parametrize("origin", [
    None, 1, [], "", "example.test", "ftp://example.test", "http://localhost",
    "http://127.0.0.1", "https://user:pass@example.test", "https://example.test/api/",
    "https://example.test?", "https://example.test#", "https://example.test\\evil",
    "https://example.test/%2e", " https://example.test", "https://example.test\n",
    "https://example.test\x00", "https://example.test\x1b[31m", "https://éxample.test",
    "https://example.test:", "https://example.test:0", "https://example.test:65536",
    "https://example.test:abc", "https://bad_host.test", "https://-bad.test",
    "https://example.test.", "https://127.1", "https://[::1", "https://[::1]evil",
    "https://[::1%25zone]", "https://[v1.localhost]", "https:////example.test",
], ids=lambda value: "invalid-origin")
def test_invalid_origins_are_static_errors(origin):
    with pytest.raises(CLIError) as caught:
        validate_origin(origin)
    assert_safe(caught.value)


@pytest.mark.parametrize(("origin", "expected"), [
    ("http://localhost:80/", "http://localhost"),
    ("http://LOCALHOST:8123", "http://localhost:8123"),
    ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
    ("http://127.12.34.56", "http://127.12.34.56"),
    ("http://[::1]:8123/", "http://[::1]:8123"),
])
def test_explicit_http_loopback_exception(origin, expected):
    # Local development exception only; ordinary profiles remain HTTPS-only.
    assert validate_origin(origin, allow_http_loopback=True) == expected


@pytest.mark.parametrize("origin", [
    "http://example.test", "http://localhost.example.test", "http://sub.localhost",
    "http://localhost.", "http://127.1", "http://2130706433", "http://0x7f000001",
    "http://0.0.0.0", "http://192.168.1.1", "http://[::]", "http://[::ffff:127.0.0.1]",
])
def test_http_exception_does_not_allow_nonliteral_or_nonloopback_hosts(origin):
    with pytest.raises(CLIError):
        validate_origin(origin, allow_http_loopback=True)


@pytest.mark.parametrize("value", [1, "true", [], None])
def test_http_opt_in_requires_boolean(value):
    with pytest.raises(CLIError):
        validate_origin("https://example.test", allow_http_loopback=value)


@pytest.mark.parametrize("path", ["/", "/custom/runtime/", "/v1/agent-api/"])
def test_mounts_are_preserved(path):
    assert validate_mount(path) == path


@pytest.mark.parametrize("path", [
    None, 1, "", "runtime/", "/runtime", "//evil.test/", "https://evil.test/",
    "/a//b/", "/./", "/a/../", "/%2e%2e/", "/a?b/", "/a#b/", "/a\\b/",
    "/a b/", "/a\r\n/", "/a\x7f/", "/a\x1b[31m/", "/https:evil/",
])
def test_invalid_mounts(path):
    with pytest.raises(CLIError) as caught:
        validate_mount(path)
    assert_safe(caught.value)


def test_reading_absent_store_does_not_create_it(store):
    assert store.list() == []
    with pytest.raises(CLIError):
        store.get()
    assert not store.directory.exists()


def test_add_select_list_reload_and_private_metadata(store, profile):
    store.add("production", profile)
    loaded = Store(store.directory).get()
    assert loaded == dict(profile, runtime_mount=None, studio_mount=None, identity_path=None,
                          auth_scheme="Token", allow_http_loopback=False, credential_file=None)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.add("other", dict(profile, origin="https://other.example.test", auth_scheme="Bearer",
                            runtime_mount="/custom/", identity_path="/identity"))
    assert store.get()["origin"] == profile["origin"]
    store.use("other")
    assert Store(store.directory).get()["auth_scheme"] == "Bearer"
    assert store.get("production")["origin"] == profile["origin"]
    rows = store.list()
    assert [(row["name"], row["selected"]) for row in rows] == [("other", True), ("production", False)]
    assert all("credential_file" not in row for row in rows)
    assert {path.name for path in store.directory.iterdir()} == {"config.json"}


def test_no_overwrite_or_implicit_reselection(store, profile):
    store.add("one", profile)
    original = store.path.read_bytes()
    with pytest.raises(CLIError):
        store.add("one", dict(profile, origin="https://changed.example.test"))
    with pytest.raises(CLIError):
        store.use("missing")
    assert store.path.read_bytes() == original


def test_unselected_store_requires_explicit_name(store, profile):
    store.add("one", profile)
    data = json.loads(store.path.read_bytes())
    data["selected"] = None
    store.path.write_text(json.dumps(data))
    with pytest.raises(CLIError):
        store.get()
    assert store.get("one")["origin"] == profile["origin"]
    store.add("two", profile)
    assert not any(row["selected"] for row in store.list())


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a\n", "a\x1b[31m", "a" * 65, None, []])
def test_unsafe_profile_names(store, profile, name):
    with pytest.raises(CLIError) as caught:
        store.add(name, profile)
    assert_safe(caught.value)
    assert not store.directory.exists()


@pytest.mark.parametrize("changes", [
    {"token": "synthetic-forbidden-value"}, {"password": "synthetic-forbidden-value"},
    {"origin": None}, {"auth_scheme": "Basic"}, {"auth_scheme": ""}, {"auth_scheme": []},
    {"runtime_mount": "/bad"}, {"studio_mount": "/../"}, {"identity_path": "https://evil.test/"},
    {"identity_path": "/who?token=x"}, {"allow_http_loopback": 1}, {"allow_http_loopback": "true"},
    {"credential_file": []}, {"credential_file": "bad\npath"},
])
def test_profiles_reject_unknown_or_invalid_fields(store, profile, changes):
    with pytest.raises(CLIError) as caught:
        store.add("one", dict(profile, **changes))
    assert_safe(caught.value)


def test_optional_null_fields_and_explicit_http_exception(store, profile):
    nullable = dict.fromkeys(config._PROFILE_KEYS)
    store.add("default", dict(nullable, **profile))
    assert store.get()["auth_scheme"] == "Token"
    # Deliberate local-development exception; do not change shared profile defaults.
    store.add("local", {"origin": "http://127.0.0.1:8123", "allow_http_loopback": True})
    assert store.get("local")["allow_http_loopback"] is True
    assert store.get()["allow_http_loopback"] is False


@pytest.mark.parametrize("content", [
    b"\xff", b"not json\x1b[31m", b"[]", b"null", b"{}", b'{"version":1,"version":1}',
    b'{"version":true,"selected":null,"profiles":{}}',
    b'{"version":1,"selected":[],"profiles":{}}',
    b'{"version":1,"selected":"missing","profiles":{}}',
    b'{"version":1,"selected":null,"profiles":[],"secret":"synthetic"}',
    b"[" * 2000 + b"]" * 2000,
], ids=lambda value: "malformed-config")
def test_malformed_config_has_static_diagnostics(store, content):
    store.directory.mkdir(mode=0o700)
    private_file(store.path, content)
    with pytest.raises(CLIError) as caught:
        store.list()
    assert_safe(caught.value)


def test_all_loaded_profiles_validated_even_when_not_selected(store, profile):
    store.add("one", profile)
    data = json.loads(store.path.read_bytes())
    data["profiles"]["unselected"] = dict(profile, token="synthetic-forbidden-value")
    store.path.write_text(json.dumps(data))
    for read in (store.list, store.get, lambda: store.get("one")):
        with pytest.raises(CLIError) as caught:
            read()
        assert_safe(caught.value)


def test_duplicate_nested_config_fields_rejected(store):
    store.directory.mkdir(mode=0o700)
    private_file(store.path, b'{"version":1,"selected":"one","profiles":{"one":'
                 b'{"origin":"https://a.test","origin":"https://b.test"}}}')
    with pytest.raises(CLIError):
        store.get()


def test_oversize_config_rejected(store):
    store.directory.mkdir(mode=0o700)
    private_file(store.path, b" " * (config._CONFIG_LIMIT + 1))
    with pytest.raises(CLIError):
        store.list()


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o700, 0o1600])
def test_insecure_config_file_not_read_or_repaired(store, profile, mode):
    store.add("one", profile)
    store.path.chmod(mode)
    with pytest.raises(CLIError):
        store.list()
    with pytest.raises(CLIError):
        store.add("two", profile)
    assert stat.S_IMODE(store.path.stat().st_mode) == mode


def test_insecure_directory_not_repaired(store, profile):
    store.directory.mkdir(mode=0o755)
    store.directory.chmod(0o755)
    with pytest.raises(CLIError):
        store.add("one", profile)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o755


@pytest.mark.parametrize("broken", [False, True])
def test_config_symlinks_rejected(store, profile, tmp_path, broken):
    store.directory.mkdir(mode=0o700)
    target = tmp_path.resolve() / "target"
    if not broken:
        private_file(target, b"{}")
    store.path.symlink_to(target)
    with pytest.raises(CLIError):
        store.add("one", profile)
    assert store.path.is_symlink()


def test_directory_and_ancestor_symlinks_rejected(tmp_path, profile):
    target = tmp_path.resolve() / "real"
    target.mkdir(mode=0o700)
    alias = tmp_path.resolve() / "alias"
    alias.symlink_to(target, target_is_directory=True)
    for path in (alias, alias / "child"):
        with pytest.raises(CLIError):
            Store(path).add("one", profile)


def test_atomic_write_preserves_previous_file_on_failure(store, profile, monkeypatch):
    store.add("one", profile)
    original = store.path.read_bytes()

    def fail_replace(source, destination):
        assert Path(destination) == store.path
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        raise OSError("synthetic unsafe filesystem diagnostic")

    monkeypatch.setattr(config.os, "replace", fail_replace)
    with pytest.raises(CLIError) as caught:
        store.add("two", profile)
    assert_safe(caught.value)
    assert store.path.read_bytes() == original
    assert {path.name for path in store.directory.iterdir()} == {"config.json"}


def test_credentials_are_external_absolute_and_not_persisted(store, profile, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    token_file = private_file(root / "credential")
    monkeypatch.chdir(root)
    store.add("one", dict(profile, credential_file="credential"))
    loaded = store.get()
    assert loaded["credential_file"] == str(token_file)
    assert load_token(loaded, environ={}) == token_file.read_text()
    assert token_file.read_bytes() not in store.path.read_bytes()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_token_source_precedence_and_no_fallback(profile, tmp_path):
    token_file = private_file(tmp_path.resolve() / "credential")
    configured = dict(profile, credential_file=str(token_file))
    environment = {"AGENTCTL_TOKEN": "synthetic-environment-token"}
    assert load_token(configured, token_stdin=True, stdin=io.StringIO("synthetic-stdin-token"),
                      environ=environment) == "synthetic-stdin-token"
    assert load_token(configured, environ=environment) == "synthetic-file-token"
    assert load_token(profile, environ=environment) == "synthetic-environment-token"
    with pytest.raises(CLIError):
        load_token(configured, token_stdin=True, stdin=io.StringIO(""), environ=environment)
    token_file.write_text("invalid\nsecond-line")
    with pytest.raises(CLIError):
        load_token(configured, environ=environment)


def test_explicit_stdin_does_not_read_lower_priority_sources(profile, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Lower-priority source was read")

    monkeypatch.setattr(config, "_read_private", forbidden)
    configured = dict(profile, credential_file="/nonexistent-synthetic-credential")
    assert load_token(configured, token_stdin=True, stdin=io.BytesIO(b"synthetic-stdin-token"),
                      environ={}) == "synthetic-stdin-token"


@pytest.mark.parametrize("value", ["", " ", "abc def", "abc\n", "abc\r\n", "abc\t", "\x00",
                                        "\x1b[31m", "\x7f", "é", "x" * 8193, None],
                         ids=lambda value: "invalid-token")
def test_invalid_tokens_are_not_stripped_or_echoed(profile, value):
    with pytest.raises(CLIError) as caught:
        load_token(profile, environ={"AGENTCTL_TOKEN": value})
    assert_safe(caught.value, code="auth", exit_code=3)


def test_token_limit_and_stdin_read_bound(profile):
    token = "x" * 8192
    assert load_token(profile, environ={"AGENTCTL_TOKEN": token}) == token

    class ShortReads:
        def __init__(self):
            self.remaining = io.StringIO("x" * 8193)
            self.requested = []

        def read(self, count):
            self.requested.append(count)
            return self.remaining.read(min(count, 1000))

    stream = ShortReads()
    with pytest.raises(CLIError):
        load_token(profile, token_stdin=True, stdin=stream, environ={})
    assert max(stream.requested) == 8195
    assert stream.remaining.tell() == 8193


def test_invalid_stdin_encoding_and_missing_token(profile):
    with pytest.raises(CLIError) as caught:
        load_token(profile, token_stdin=True, stdin=io.BytesIO(b"\xff"), environ={})
    assert_safe(caught.value, code="auth", exit_code=3)
    with pytest.raises(CLIError) as caught:
        load_token(profile, environ={})
    assert_safe(caught.value, code="auth", exit_code=3)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700, 0o000])
def test_insecure_credentials_rejected(store, profile, tmp_path, mode):
    token_file = private_file(tmp_path.resolve() / "credential")
    token_file.chmod(mode)
    configured = dict(profile, credential_file=str(token_file))
    with pytest.raises(CLIError):
        store.add("one", configured)
    with pytest.raises(CLIError):
        load_token(configured, environ={"AGENTCTL_TOKEN": "synthetic-fallback"})


def test_readonly_credential_and_revalidation(store, profile, tmp_path):
    token_file = private_file(tmp_path.resolve() / "credential")
    token_file.chmod(0o400)
    store.add("one", dict(profile, credential_file=str(token_file)))
    assert load_token(store.get(), environ={}) == "synthetic-file-token"
    token_file.chmod(0o644)
    assert len(store.list()) == 1
    with pytest.raises(CLIError):
        load_token(store.get(), environ={})


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_single_line_secret_provider_output(profile, tmp_path, ending):
    expected = "synthetic-provider-output"
    assert load_token(profile, token_stdin=True, stdin=io.StringIO(expected + ending)) == expected
    path = private_file(tmp_path.resolve() / "credential", (expected + ending).encode())
    assert load_token(dict(profile, credential_file=str(path)), environ={}) == expected


def test_stale_other_credential_does_not_block_profile_selection(store, profile, tmp_path):
    path = private_file(tmp_path.resolve() / "credential")
    store.add("stale", dict(profile, credential_file=str(path)))
    path.chmod(0o644)  # Explicit insecure-source failure, not a general fixture.
    store.add("active", profile)
    store.use("active")
    assert load_token(store.get(), environ={"AGENTCTL_TOKEN": "synthetic-new-token"})


def test_symlink_nonregular_hardlinked_and_unowned_credentials(profile, tmp_path, monkeypatch):
    root = tmp_path.resolve()
    token_file = private_file(root / "credential")
    link = root / "link"
    link.symlink_to(token_file)
    for path in (link, root):
        with pytest.raises(CLIError):
            load_token(dict(profile, credential_file=str(path)), environ={})
    hardlink = root / "hardlink"
    os.link(token_file, hardlink)
    with pytest.raises(CLIError):
        load_token(dict(profile, credential_file=str(hardlink)), environ={})
    owned_file = private_file(root / "owned")
    monkeypatch.setattr(config.os, "getuid", lambda: owned_file.stat().st_uid + 1)
    with pytest.raises(CLIError):
        load_token(dict(profile, credential_file=str(owned_file)), environ={})


def test_environment_token_used_only_when_no_explicit_source(profile, monkeypatch):
    monkeypatch.setenv("AGENTCTL_TOKEN", "synthetic-environment-token")
    assert load_token(profile) == "synthetic-environment-token"


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_default_directory_is_user_specific_not_cwd(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(config.sys, "platform", platform)
    directory = default_directory()
    assert directory.is_absolute()
    assert directory.is_relative_to(tmp_path.resolve() / "home")
    assert not directory.exists()


def test_config_directory_override_and_xdg(monkeypatch, tmp_path):
    explicit = tmp_path.resolve() / "explicit"
    monkeypatch.setenv("AGENTCTL_CONFIG_DIR", str(explicit))
    assert default_directory() == explicit
    monkeypatch.setenv("AGENTCTL_CONFIG_DIR", "relative")
    with pytest.raises(CLIError):
        default_directory()
    monkeypatch.delenv("AGENTCTL_CONFIG_DIR")
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.resolve() / "xdg"))
    assert default_directory() == tmp_path.resolve() / "xdg" / "agentctl"
