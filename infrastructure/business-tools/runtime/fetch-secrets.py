#!/usr/bin/env python3
"""Fetch one private payload, validate all fields, write least-privilege raw envs.

No credentials in Terraform, argv, stdout, or exception messages. Compose >=2.30.
"""
import base64
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import urllib.parse
import urllib.request

DEPLOYMENT_PATH = Path('/opt/business-tools/deployment.json')
ENV_DIR = Path('/run/business-tools')
METADATA_URL = 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'
LIMIT = 128 * 1024
FIELDS = {
    'postiz': {'db_password', 'jwt_secret', 'temporal_password'},
    'mautic': {'db_password', 'db_root_password'},
    'invoice_ninja': {'db_password', 'db_root_password', 'app_key', 'admin_email', 'admin_password'},
    'grafana': {'admin_user', 'admin_password', 'secret_key'},
}
# Add integrations only after checking the selected upstream release's contract.
EXTRA_KEYS = {
    'postiz': {
        'X_API_KEY', 'X_API_SECRET', 'X_URL', 'LINKEDIN_CLIENT_ID', 'LINKEDIN_CLIENT_SECRET',
        'FACEBOOK_APP_ID', 'FACEBOOK_APP_SECRET', 'THREADS_APP_ID', 'THREADS_APP_SECRET',
        'YOUTUBE_CLIENT_ID', 'YOUTUBE_CLIENT_SECRET', 'TIKTOK_CLIENT_ID', 'TIKTOK_CLIENT_SECRET',
        'PINTEREST_CLIENT_ID', 'PINTEREST_CLIENT_SECRET', 'REDDIT_CLIENT_ID', 'REDDIT_CLIENT_SECRET',
        'OPENAI_API_KEY', 'RESEND_API_KEY', 'EMAIL_FROM_ADDRESS', 'EMAIL_FROM_NAME',
        'EMAIL_PROVIDER', 'DISCORD_CLIENT_ID', 'DISCORD_CLIENT_SECRET', 'DISCORD_BOT_TOKEN_ID',
    },
    'invoice_ninja': {'MAIL_MAILER', 'MAIL_HOST', 'MAIL_PORT', 'MAIL_USERNAME', 'MAIL_PASSWORD',
                      'MAIL_ENCRYPTION', 'MAIL_FROM_ADDRESS', 'MAIL_FROM_NAME'},
    'mautic': set(),  # SMTP and application preferences live in Mautic's admin UI.
    'grafana': set(),
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate key')
        result[key] = value
    return result


def parse_json(data):
    return json.loads(data, object_pairs_hook=unique_object)


def load_deployment(path=DEPLOYMENT_PATH):
    data = parse_json(path.read_text())
    expected = {'project_id', 'bootstrap_secret_id', 'bootstrap_secret_version', 'data_device', 'data_mount'}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError('Invalid deployment')
    if not all(isinstance(value, str) for value in data.values()):
        raise ValueError('Invalid deployment')
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', data['project_id']):
        raise ValueError('Invalid project')
    if data['bootstrap_secret_id'] != 'business-tools-bootstrap-json':
        raise ValueError('Invalid secret identifier')
    if (data['bootstrap_secret_version'] != 'latest'
            or data['data_device'] != '/dev/disk/by-id/google-business-tools-data'
            or data['data_mount'] != '/srv/business-tools'):
        raise ValueError('Invalid deployment contract')
    return data


def safe_value(value):
    # Raw env does not interpolate $, quotes, backslashes or comments. CR/LF/NUL
    # and leading/trailing whitespace are rejected instead of silently changing it.
    return (isinstance(value, str) and 1 <= len(value) <= 8192
            and value == value.strip() and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def validate_credentials(data):
    if not isinstance(data, dict) or set(data) != set(FIELDS):
        raise ValueError('Invalid sections')
    for section, required in FIELDS.items():
        values = data[section]
        if not isinstance(values, dict) or set(values) - {'extra_env'} != required:
            raise ValueError('Invalid fields')
        for key in required:
            value = values[key]
            if not safe_value(value):
                raise ValueError('Invalid value')
            if key not in {'admin_email', 'admin_user', 'app_key'} and len(value) < 32:
                raise ValueError('Credential too short')
        extras = values.get('extra_env', {})
        if (not isinstance(extras, dict) or not set(extras) <= EXTRA_KEYS[section]
                or not all(safe_value(value) for value in extras.values())):
            raise ValueError('Invalid integration environment')
    # Temporal's upstream entrypoint substitutes this into YAML. Raw env safety
    # alone does not prevent quotes/backslashes from breaking that config.
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,}', data['postiz']['temporal_password']):
        raise ValueError('Temporal password must be unpadded base64url or hex')
    key = data['invoice_ninja']['app_key']
    if not key.startswith('base64:'):
        raise ValueError('Invalid app key')
    decoded = base64.b64decode(key[7:], validate=True)
    if len(decoded) != 32 or base64.b64encode(decoded).decode() != key[7:]:
        raise ValueError('Invalid app key')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data['invoice_ninja']['admin_email']):
        raise ValueError('Invalid administrator email')
    return data


def environment_files(credentials):
    data = validate_credentials(credentials)
    p, m, i, g = (data[name] for name in ('postiz', 'mautic', 'invoice_ninja', 'grafana'))
    result = {
        'postiz.env': {'DATABASE_URL': 'postgresql://postiz:' + urllib.parse.quote(p['db_password'], safe='')
                      + '@postiz-postgres:5432/postiz', 'JWT_SECRET': p['jwt_secret']},
        'postiz-db.env': {'POSTGRES_PASSWORD': p['db_password']},
        'temporal.env': {'POSTGRES_PWD': p['temporal_password']},
        'temporal-db.env': {'POSTGRES_PASSWORD': p['temporal_password']},
        'mautic.env': {'MAUTIC_DB_PASSWORD': m['db_password']},
        'mautic-db.env': {'MARIADB_PASSWORD': m['db_password'], 'MARIADB_ROOT_PASSWORD': m['db_root_password']},
        'invoice-ninja.env': {'APP_KEY': i['app_key'], 'DB_PASSWORD': i['db_password'],
                             'IN_USER_EMAIL': i['admin_email'], 'IN_PASSWORD': i['admin_password']},
        'invoice-db.env': {'MARIADB_PASSWORD': i['db_password'], 'MARIADB_ROOT_PASSWORD': i['db_root_password']},
        'grafana.env': {'GF_SECURITY_ADMIN_USER': g['admin_user'],
                        'GF_SECURITY_ADMIN_PASSWORD': g['admin_password'], 'GF_SECURITY_SECRET_KEY': g['secret_key']},
    }
    for section, filename in {'postiz': 'postiz.env', 'invoice_ninja': 'invoice-ninja.env'}.items():
        result[filename].update(data[section].get('extra_env', {}))
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(opener, url, headers):
    with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Response too large')
    return parse_json(data)


def fetch_credentials(deployment, opener=None):
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    identity = read_json(opener, METADATA_URL, {'Metadata-Flavor': 'Google'})
    token = identity['access_token']
    if (identity.get('token_type') != 'Bearer' or not isinstance(token, str)
            or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', token)):
        raise ValueError('Invalid identity')
    url = ('https://secretmanager.googleapis.com/v1/projects/' + deployment['project_id']
           + '/secrets/' + deployment['bootstrap_secret_id'] + '/versions/latest:access')
    payload = read_json(opener, url, {'Authorization': 'Bearer ' + token})
    return validate_credentials(parse_json(base64.b64decode(payload['payload']['data'], validate=True)))


def write_env(credentials, directory=ENV_DIR):
    files = environment_files(credentials)  # Validate ALL before touching any file.
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError('Unsafe env directory')
    directory.chmod(0o700)
    for name in files:
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Unsafe env target')
    for name, values in files.items():
        fd, temporary = tempfile.mkstemp(prefix='.env-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                for key, value in sorted(values.items()):
                    stream.write(key + '=' + value + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / name)
        finally:
            Path(temporary).unlink(missing_ok=True)


def main():
    try:
        if os.geteuid() != 0:
            raise PermissionError('Root required')
        write_env(fetch_credentials(load_deployment()))
    except Exception:
        print('Unable to prepare business-tools credentials; startup blocked.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())