#!/usr/bin/env python3
"""Fetch bootstrap credentials without printing credentials or remote errors."""

import base64
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.request


DEPLOYMENT_PATH = Path("/opt/ai-gateway/deployment.json")
ENV_PATH = Path("/run/ai-gateway/bootstrap.env")
METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
KEYS = (
    "BIFROST_ADMIN_USERNAME",
    "BIFROST_ADMIN_PASSWORD",
    "BIFROST_ENCRYPTION_KEY",
    "BIFROST_SETUP_TOKEN",
)
SAFE_VALUE = re.compile(r"[A-Za-z0-9_./+=:@-]+")
MAX_RESPONSE_BYTES = 128 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_json(data):
    return json.loads(data, object_pairs_hook=unique_object)


def load_deployment(path=DEPLOYMENT_PATH):
    deployment = parse_json(path.read_text(encoding="utf-8"))
    expected = {
        "project_id", "bootstrap_secret_id", "bootstrap_secret_version",
        "data_device", "data_mount",
    }
    if not isinstance(deployment, dict) or set(deployment) != expected:
        raise ValueError("Invalid deployment")
    if not all(isinstance(value, str) for value in deployment.values()):
        raise ValueError("Invalid deployment")
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", deployment["project_id"]):
        raise ValueError("Invalid project")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", deployment["bootstrap_secret_id"]):
        raise ValueError("Invalid secret identifier")
    if (
        deployment["bootstrap_secret_version"] != "latest"
        or deployment["data_device"] != "/dev/disk/by-id/google-ai-gateway-data"
        or deployment["data_mount"] != "/srv/ai-gateway"
    ):
        raise ValueError("Invalid runtime contract")
    return deployment


def validate_credentials(credentials):
    if not isinstance(credentials, dict) or set(credentials) != set(KEYS):
        raise ValueError("Invalid credential keys")
    for key in KEYS:
        value = credentials[key]
        if not isinstance(value, str) or SAFE_VALUE.fullmatch(value) is None:
            raise ValueError("Invalid credential value")
        if key == "BIFROST_ADMIN_USERNAME":
            if not 1 <= len(value) <= 128:
                raise ValueError("Invalid username length")
        elif len(value) < 32:
            raise ValueError("Invalid credential length")
    return credentials


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a metadata or authorization header to a redirect target.
        return None


def read_json(opener, url, headers):
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=20) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Response too large")
    return parse_json(data)


def fetch_credentials(deployment, opener=None):
    if opener is None:
        # Ignore host proxy settings: the metadata token must stay on this VM.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
    identity = read_json(opener, METADATA_URL, {"Metadata-Flavor": "Google"})
    token = identity["access_token"]
    if (
        identity.get("token_type") != "Bearer"
        or not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is None
    ):
        raise ValueError("Invalid identity response")
    url = (
        "https://secretmanager.googleapis.com/v1/projects/"
        + deployment["project_id"] + "/secrets/"
        + deployment["bootstrap_secret_id"] + "/versions/latest:access"
    )
    envelope = read_json(opener, url, {"Authorization": "Bearer " + token})
    payload = base64.b64decode(envelope["payload"]["data"], validate=True)
    return validate_credentials(parse_json(payload))


def write_env(credentials, path=ENV_PATH):
    credentials = validate_credentials(credentials)
    directory = path.parent
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("Unsafe runtime directory")
    directory.chmod(0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Unsafe environment destination")
    descriptor, temporary = tempfile.mkstemp(prefix=".bootstrap-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            os.fchmod(stream.fileno(), 0o600)
            for key in KEYS:
                stream.write(key + "=" + credentials[key] + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(deployment_path=DEPLOYMENT_PATH, env_path=ENV_PATH):
    try:
        if os.geteuid() != 0:
            raise PermissionError("Root required")
        deployment = load_deployment(deployment_path)
        credentials = fetch_credentials(deployment)
        write_env(credentials, env_path)
    except Exception:
        # Do not include exceptions: HTTP bodies and decoding errors can contain
        # tokens or payloads. A failed fetch never touches the previous env file.
        print("Unable to prepare AI gateway credentials.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())