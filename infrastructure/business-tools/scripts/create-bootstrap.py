#!/usr/bin/env python3
"""Create a NEW bootstrap payload in a protected file, never on stdout.

Use once for an empty deployment; rerunning never rotates stored credentials.
Upload via gcloud secrets versions add --data-file=<path>, never command values.
"""
import argparse
import base64
import getpass
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap_credentials', ROOT / 'runtime/fetch-secrets.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def generate(email, username):
    data = {section: {key: secrets.token_hex(32) for key in keys}
            for section, keys in helper.FIELDS.items()}
    data['invoice_ninja']['admin_email'] = email
    data['invoice_ninja']['app_key'] = 'base64:' + base64.b64encode(secrets.token_bytes(32)).decode()
    data['grafana']['admin_user'] = username
    return helper.validate_credentials(data)


def write_new(path, data):
    # Caller explicitly chooses an existing private directory outside the repo.
    parent = path.parent.resolve(strict=True)
    info = parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Use an owned private directory (0700)')
    helper.validate_credentials(data)
    fd = os.open(parent / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        email = getpass.getpass('Invoice administrator email (hidden): ')
        username = getpass.getpass('Grafana administrator username (hidden): ')
        write_new(args.output, generate(email, username))
    except Exception:
        print('Bootstrap creation failed; no credential values were printed.', file=sys.stderr)
        return 1
    print('Created protected bootstrap file. Upload with --data-file; keep a secured recovery copy.')
    return 0


if __name__ == '__main__':
    sys.exit(main())