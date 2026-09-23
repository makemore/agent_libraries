#!/usr/bin/env python3
"""Approved business-tools target only: preview, empty-DB proof, then install.

No credentials travel through SSH arguments or stdout. Local protected recovery
files supply HTTP credentials; the VM proof uses its container environment.
Never retries or resumes a partial install. Defaults to a read-only preview.
"""
import argparse
import json
from pathlib import Path
import subprocess

from private_admin import Client, HOSTS, protected_json, require
from mautic_admin import initialize, _login

PROOF = r'''
import json, re, subprocess
from pathlib import Path
root = Path('/srv/business-tools/mautic/config')
seed = root / 'parameters_local.php'
local = root / 'local.php'
assert not root.is_symlink() and root.stat().st_uid == 33
assert root.stat().st_mode & 0o777 == 0o700
assert set(p.name for p in root.iterdir()) == {'parameters_local.php', 'local.php'}
for path in (seed, local):
    assert path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1
    # The image creates local.php as 0755 inside this 0700 directory. Preserve
    # that supported default; other identities cannot traverse the directory.
    # Refuse group/world writes. The separate proxy seed retains exact 0600.
    assert path.stat().st_uid == 33 and path.stat().st_mode & 0o022 == 0
assert seed.stat().st_mode & 0o777 == 0o600
assert seed.read_bytes() == b"<?php\n$parameters = ['trusted_proxies' => ['172.30.251.2']];\n"
text = local.read_text()
assert not re.search(r"['\"]site_url['\"]\s*=>", text)
assert re.search(r"['\"]db_driver['\"]\s*=>\s*['\"]pdo_mysql['\"]", text)
for key, env in {'db_host':'MAUTIC_DB_HOST', 'db_user':'MAUTIC_DB_USER',
                 'db_name':'MAUTIC_DB_DATABASE', 'db_password':'MAUTIC_DB_PASSWORD'}.items():
    assert re.search(r"['\"]" + key + r"['\"]\s*=>\s*getenv\(['\"]" + env + r"['\"]\)", text)
php = "$db=new PDO('mysql:host=mautic-mariadb;dbname=mautic','mautic',getenv('MAUTIC_DB_PASSWORD')); echo json_encode(['tables'=>(int)$db->query('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()')->fetchColumn()]);"
p = subprocess.run(['/opt/business-tools/compose.sh','exec','-T','mautic','php','-r',php],
                   capture_output=True, text=True, timeout=30)
assert p.returncode == 0 and json.loads(p.stdout) == {'tables': 0}
print(json.dumps({'fresh_install_verified': True}))
'''


def fresh_proof():
    result = subprocess.run([
        'gcloud', 'compute', 'ssh', 'business-tools', '--project=makemoredigital2025',
        '--zone=europe-west2-b', '--tunnel-through-iap', '--quiet', '--command=sudo python3 -'],
        input=PROOF, capture_output=True, text=True, timeout=90)
    require(result.returncode == 0)
    require(json.loads(result.stdout) == {'fresh_install_verified': True})
    return True


def verify_session_security(client):
    cookies = list(client.cookies)
    require(bool(cookies) and all(cookie.secure for cookie in cookies))
    require(all(any(key.lower() == 'httponly' for key in cookie._rest) for cookie in cookies))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recovery-directory', type=Path)
    parser.add_argument('--tunnel-port', type=int, default=18443)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        client = Client(HOSTS['mautic'], args.tunnel_port)
        preview = initialize(client, None, None)
        if not args.apply:
            print(json.dumps(preview))
            return 0
        require(args.recovery_directory is not None)
        credentials = protected_json(args.recovery_directory / 'admin-access.json')['mautic']
        if preview['installer_locked']:
            # Reruns verify the existing account, never reset/reinstall it.
            _login(client, credentials)
            verify_session_security(client)
            print(json.dumps({'login_verified': True, 'installer_locked': True,
                              'secure_httponly_session': True, 'reinstalled': False}))
            return 0
        password = protected_json(args.recovery_directory / 'bootstrap.json')['mautic']['db_password']
        result = initialize(client, credentials, password, apply=True, fresh_install_verified=fresh_proof())
        verify_session_security(client)
        print(json.dumps(result))
        return 0
    except Exception:
        print('Mautic setup stopped; manual review required. No values printed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())