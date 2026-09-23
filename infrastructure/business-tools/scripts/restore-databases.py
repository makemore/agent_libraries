#!/usr/bin/env python3
"""Opt-in LOCAL cold physical restore drill; default is a daemon-free preview.

Requires OpenTofu, Compose >=2.30 and already-local pinned linux/amd64 images.
--run checks postgres (17), temporal_postgres (16), and mariadb (11.4, Mautic's
service) sequentially; --database selects one. Never pulls or accesses cloud.

Named exception: isolated-cold-restore. Reuse smoke's guarded unique project
namespace, real Compose rendering, private synthetic envs and fresh-volume UID
setup. Retain DB entrypoints/settings/health/stop periods; remove public ports
and make networks internal. No existing host data or destination is accepted.
Stop and verify exit 0 before a network-none GNU tar stream copies a READONLY
source into an empty destination, preserving numeric owners, ACLs and xattrs.
No destination DB runs before extraction. Credentials travel by files/stdin,
never argv or diagnostics. Cleanup removes only this invocation's owned project
and synthetic volumes, irreversibly; cleanup failures make the drill fail.

This complements tests/test_backup_archive.py, NOT a full-app/key/media archive,
TLS restore, production lifecycle test or proof of all ACL/xattr variants.
tests/test_restore_databases.py covers the local exception and safety guards.
"""

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import secrets
import signal
import sys
import tempfile


SPEC = importlib.util.spec_from_file_location('restore_smoke', Path(__file__).with_name('smoke-images.py'))
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
DATABASES = {
    'postgres': ('postiz', 'postiz-postgres', '/var/lib/postgresql/data'),
    'temporal_postgres': ('postiz', 'postiz-temporal-postgres', '/var/lib/postgresql/data'),
    'mariadb': ('mautic', 'mautic-mariadb', '/var/lib/mysql'),
}
TRANSFER = '''test -d /source
test ! -L /source
test -d /destination
test ! -L /destination
contents=$(ls -A /destination)
test -z "$contents"
tar --version | grep -q 'GNU tar'
tar --create --gzip --file - --numeric-owner --acls --xattrs --one-file-system --directory /source . |
tar --extract --gzip --file - --numeric-owner --same-owner --same-permissions --acls --xattrs --directory /destination
'''
CLIENT = '/tmp/bt-restore-client.cnf'


def build_model(original, images, project, env_dir, databases):
    plan = {'name': project, 'services': {}, 'volumes': {}, 'networks': {}}
    sources = {}
    for key in databases:
        group, name, target = DATABASES[key]
        isolated, data = smoke.isolated_model(original, group, images, project, env_dir)
        service = isolated['services'][name]
        smoke.require(not service.get('depends_on'))
        health = service.get('healthcheck', {})
        smoke.require(health.get('test') and health['test'][0] != 'NONE' and not health.get('disable'))
        smoke.require(len(service['volumes']) == 1)
        mount = service['volumes'][0]
        smoke.require(mount['type'] == 'volume' and mount['target'] == target and not mount.get('read_only'))
        for role in ('source', 'restored'):
            volume = key + '-' + role
            clone = copy.deepcopy(service)
            clone['volumes'][0]['source'] = volume
            plan['services'][volume] = clone
            plan['volumes'][volume] = {'name': project + '-' + volume}
        sources[key + '-source'] = data[mount['source']]
        plan['networks'].update({n: isolated['networks'][n] for n in service['networks']})
    smoke.add_fixture(plan, sources, images['postgres'])
    for key in databases:
        helper = copy.deepcopy(plan['services'][smoke.FIXTURE])
        helper['entrypoint'] = ['/bin/bash']
        helper['command'] = ['-euo', 'pipefail', '-c', TRANSFER]
        # The 64 MiB initialization helper is too small for MariaDB's cold tar
        # stream (observed SIGKILL/137). This test-only helper also charges file
        # cache; production backup runs on the host, not in this container.
        helper['mem_limit'] = '512m'
        helper['volumes'] = [
            {'type': 'volume', 'source': key + '-source', 'target': '/source',
             'read_only': True, 'volume': {'nocopy': True}},
            {'type': 'volume', 'source': key + '-restored', 'target': '/destination',
             'volume': {'nocopy': True}},
        ]
        plan['services'][key + '-transfer'] = helper
    return plan


def sql(compose, directory, service, statement):
    if service.startswith('mariadb-'):
        client = (f'exec mariadb --defaults-extra-file={CLIENT} --batch --skip-column-names '
                  '--database="$MARIADB_DATABASE"')
    else:
        client = ('exec psql --no-psqlrc --no-password --host=/var/run/postgresql '
                  '--username="$POSTGRES_USER" --dbname="$POSTGRES_DB" '
                  '--set=ON_ERROR_STOP=1 --tuples-only --no-align')
    return smoke.command([*compose, 'exec', '-T', service, '/bin/sh', '-eu', '-c', client],
                         cwd=directory, input=statement)


def stop_clean(compose, docker, directory, project, service):
    # No -t/--timeout: preserve production stop_grace_period (including future increases).
    smoke.command([*compose, 'stop', service], cwd=directory, timeout=4200)
    state = smoke.command([*docker, 'container', 'inspect', '--format',
                           '{{.State.Status}} {{.State.ExitCode}}', project + '-' + service + '-1'],
                          cwd=directory).strip()
    smoke.require(state == 'exited 0')


def execute(plan, databases, directory, wait_timeout):
    smoke.require(type(wait_timeout) is int and 1 <= wait_timeout <= 600)
    path = directory / 'restore.json'
    smoke.write_private(path, json.dumps(smoke.compose_literal(plan)))
    docker = smoke.local_docker(directory)
    # Include helpers for cleanup even if an interrupted `run --rm` leaves one behind.
    compose = [*smoke.compose_base(directory, plan['name'], docker), '--file', str(path), '--profile', '*']
    smoke.command([*compose, 'config', '--no-env-resolution', '--quiet'], cwd=directory)
    smoke.preflight(plan, docker, directory)  # No cleanup authority before absence/image checks.
    passed, cleaned = False, False
    stage = 'fixture'
    try:
        smoke.command([*compose, 'run', '--rm', '--no-deps', '--pull=never', '-T', smoke.FIXTURE],
                      cwd=directory)
        for key in databases:
            sentinel = secrets.token_hex(16)
            check = ("SELECT CASE WHEN COUNT(*) = 1 AND MIN(value) = '" + sentinel + "' "
                     "THEN 'PASS' ELSE 'FAIL' END FROM restore_drill;\n")
            for role in ('source', 'restored'):
                service = key + '-' + role
                if role == 'restored':
                    stage = key + ': cold transfer'
                    smoke.command([*compose, 'run', '--rm', '--no-deps', '--pull=never', '-T',
                                   key + '-transfer'], cwd=directory, timeout=600)
                stage = service + ': startup'
                smoke.command([*compose, 'up', '-d', '--no-deps', '--pull=never', '--wait',
                               '--wait-timeout', str(wait_timeout), service],
                              cwd=directory, timeout=wait_timeout + 60)
                if key == 'mariadb':
                    # A fresh protected client file OUTSIDE the DB volume, sent only via stdin.
                    stage = service + ': client configuration'
                    smoke.command([*compose, 'exec', '-T', service, '/bin/sh', '-eu', '-c',
                                   f'umask 077; set -C; cat > {CLIENT}'], cwd=directory,
                                  input=(directory / 'env/client.cnf').read_text())
                if role == 'source':
                    stage = service + ': insert'
                    sql(compose, directory, service,
                        'CREATE TABLE restore_drill (value VARCHAR(64) PRIMARY KEY);\n'
                        f"INSERT INTO restore_drill VALUES ('{sentinel}');\n")
                stage = service + ': query'
                smoke.require(sql(compose, directory, service, check).strip() == 'PASS')
                stage = service + ': clean stop'
                stop_clean(compose, docker, directory, plan['name'], service)
            print(f'{key}: PASS')
        passed = True
    except (Exception, KeyboardInterrupt):
        # Only our fixed stage identifiers, never subprocess diagnostics or exceptions.
        print('restore stage failed: ' + stage)
    finally:
        try:
            smoke.command([*compose, 'down', '--volumes'], cwd=directory, timeout=4200)
            smoke.require_absent(plan['name'], docker, directory)
            cleaned = True
        except (Exception, KeyboardInterrupt):
            pass
    print(f'project={plan["name"]}: cleanup {"PASS (synthetic volumes removed)" if cleaned else "FAIL"}')
    return passed and cleaned


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'restore: FAIL (invalid arguments; use --help)\n')


def main(argv=None):
    parser = QuietParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--images', type=Path, default=smoke.DEFAULT_IMAGES, help='public release images JSON')
    parser.add_argument('--database', choices=DATABASES, help='omit to check all three sequentially')
    parser.add_argument('--run', action='store_true', help='opt in to fresh local Docker writes')
    parser.add_argument('--wait-timeout', type=int, default=600, help='health wait seconds, 1..600')
    args = parser.parse_args(argv)
    if not 1 <= args.wait_timeout <= 600:
        parser.error('invalid wait')
    databases = (args.database,) if args.database else tuple(DATABASES)
    try:
        images = smoke.load_images(args.images)
        project = 'bt-smoke-' + secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix=project + '-restore-') as temporary:
            directory = Path(temporary).resolve()
            original = smoke.render_model(images, directory, project)
            credentials = smoke.bootstrap.generate('restore@example.invalid', 'restore')
            env_dir = directory / 'env'
            smoke.bootstrap.helper.write_env(credentials, env_dir)
            plan = build_model(original, images, project, env_dir, databases)
            if 'mariadb' in databases:
                user = plan['services']['mariadb-source']['environment']['MARIADB_USER']
                smoke.require(user == 'mautic')
                smoke.write_private(env_dir / 'client.cnf', '[client]\nprotocol=socket\nuser=mautic\npassword='
                                    + credentials['mautic']['db_password'] + '\n')
            if not args.run:
                print(f'preview: databases={len(databases)} fresh_volumes={len(plan["volumes"])}')
                for key in databases:
                    print(f'{key}: source -> cold tar -> restored (no Docker writes)')
                return 0
            passed = execute(plan, databases, directory, args.wait_timeout)
            print(f'restore: {"PASS" if passed else "FAIL"}')
            return 0 if passed else 1
    except (Exception, KeyboardInterrupt):
        print('restore: FAIL')
        return 1


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, smoke.interrupted)
    sys.exit(main())