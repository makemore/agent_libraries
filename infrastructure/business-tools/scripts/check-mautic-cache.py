#!/usr/bin/env python3
"""Opt-in LOCAL Mautic cache regression; default is a daemon-free preview.

Named exception: isolated-mautic-cache. Reuse smoke's real templates, private
synthetic envs, fresh volumes, internal networks and no ports. Only already-local
pinned images; no pulls, production paths, cloud or delivery. The supported CLI
creates one SYNTHETIC administrator, never a real password. Recreate fixture web
and cron twice to discard cache without losing installation. Output is booleans
only; headers/configuration/diagnostics never leave captured memory. Cleanup
irreversibly deletes only this invocation's exact project and fresh volumes.
Requires OpenTofu and Compose >=2.30. See tests/test_mautic_cache.py and Mautic README.
"""

import copy
import importlib.util
import json
from pathlib import Path
import secrets
import signal
import sys
import tempfile


SPEC = importlib.util.spec_from_file_location('cache_smoke', Path(__file__).with_name('smoke-images.py'))
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
ROLES = ('mautic', 'mautic-cron')
SITE_URL = 'https://marketing.makemoredigital.com'
PROXY = 'cache-proxy'
# Named HTTP-transport-only exception: cache-proxy simulates TLS termination for
# the cookie regression, since direct HTTP to web redirects before issuing cookies.
# Use the already trusted .2 peer, never change proxy trust, cookies or site_url.
# There is NO TLS connection or verification bypass here and no live TLS claim.
# Remove when this regression uses an approved isolated, certificate-valid TLS
# fixture. test_http_transport_only_proxy_preserves_defaults_and_isolation and the
# opt-in selected-image COOKIE probe verify this deliberately fixed setup.
CADDYFILE = '''{
    auto_https off
    admin off
}
:8080 {
    reverse_proxy mautic-web:80 {
        header_up Host marketing.makemoredigital.com
        header_up X-Forwarded-Proto https
    }
}
'''
# Named exec-entrypoint compatibility case: docker exec does not inherit exports
# made by /entrypoint.sh. Replay ONLY its non-secret DB-port fallback for CLI
# commands, preserving any configured port. Never persist it in Compose/config.
# Remove when upstream local.php safely handles an unset port. Regression below
# exercises the real image; tests/test_mautic_cache.py guards the narrow wrapper.
CLI = ['bash', '-c', 'export MAUTIC_DB_PORT="${MAUTIC_DB_PORT:-3306}"; exec php "$@"',
       'mautic-fixture-cli', '/var/www/html/bin/console']
# Verified against 6.0.9 app/bundles/InstallBundle/Command/InstallCommand.php.
# Public synthetic fixture ONLY: production credentials must never enter argv.
INSTALL = [*CLI, 'mautic:install',
           '--admin_email', 'fixture@example.invalid', '--admin_username', 'fixtureadmin',
           '--admin_password', 'synthetic-fixture-only', '--admin_firstname', 'Synthetic',
           '--admin_lastname', 'Fixture', '--force', SITE_URL]
STATE = r'''<?php
try {
    require '/var/www/html/config/local.php';
    $canonical = ($parameters['site_url'] ?? null) === 'https://marketing.makemoredigital.com';
    $prefix = $parameters['db_table_prefix'] ?? '';
    if (!preg_match('/^[a-zA-Z0-9_]*$/D', $prefix)) { exit(1); }
    $db = new PDO('mysql:host='.getenv('MAUTIC_DB_HOST').';dbname='.getenv('MAUTIC_DB_DATABASE'),
                  getenv('MAUTIC_DB_USER'), getenv('MAUTIC_DB_PASSWORD'));
    $oneUser = (int) $db->query("SELECT COUNT(*) FROM `{$prefix}users`")->fetchColumn() === 1;
    $admin = $db->query("SELECT COUNT(*) FROM `{$prefix}users` u JOIN `{$prefix}roles` r
        ON u.role_id = r.id WHERE u.username = 'fixtureadmin'
        AND u.email = 'fixture@example.invalid' AND r.is_admin = 1")->fetchColumn();
    require '/var/www/html/config/parameters_local.php';
    $proxy = $parameters === ['trusted_proxies' => ['172.30.251.2']];
    $tmpfs = preg_match('#^\S+ /var/www/html/var/cache tmpfs #m', file_get_contents('/proc/mounts')) === 1;
    $marker = '/var/www/html/var/cache/isolated-mautic-cache-marker';
    $fresh = !file_exists($marker);
    $written = file_put_contents($marker, 'synthetic-cache-marker') !== false;
    echo json_encode(['site_url' => $canonical, 'one_user' => $oneUser,
        'admin_role' => (int) $admin === 1, 'proxy_seed' => $proxy,
        'tmpfs' => $tmpfs, 'fresh_cache' => $fresh, 'marker_written' => $written]);
} catch (Throwable $e) { exit(1); }
'''
COOKIE = r'''<?php
// Headers stay inside PHP memory; no cookie value is written or printed.
exec('curl --fail --silent --max-time 30 --noproxy "*" --dump-header - --output /dev/null --header "Host: marketing.makemoredigital.com" http://172.30.251.2:8080/s/login', $headers, $status);
$cookies = preg_grep('/^Set-Cookie:/i', $headers);
$secure = count($cookies) > 0;
$httpOnly = false;
foreach ($cookies as $cookie) {
    $secure = $secure && preg_match('/;\s*secure\s*(;|$)/i', $cookie) === 1;
    $httpOnly = $httpOnly || preg_match('/;\s*httponly\s*(;|$)/i', $cookie) === 1;
}
echo json_encode(['login_ok' => $status === 0 && count(preg_grep('#^HTTP/\S+ 200\b#', $headers)) === 1,
    'secure_cookie' => $secure, 'httponly_session' => $httpOnly]);
'''
STATE_KEYS = {'site_url', 'one_user', 'admin_role', 'proxy_seed', 'tmpfs', 'fresh_cache', 'marker_written'}
COOKIE_KEYS = {'login_ok', 'secure_cookie', 'httponly_session'}


def with_proxy(model, images, directory):
    """Copy an isolated Mautic model; fixed inputs only, no new network or egress."""
    smoke.validate_images(images)
    smoke.require(set(model['services']) == set(smoke.GROUPS['mautic']))
    network = model['networks'].get('mautic-proxy', {})
    smoke.require(network.get('driver') == 'bridge' and network.get('internal') is True)
    smoke.require(network.get('ipam', {}).get('config') == [{'subnet': '172.30.251.0/29'}])
    smoke.require(all(n.get('internal') is True and not n.get('external')
                      for n in model['networks'].values()))
    peers = {name for name, service in model['services'].items()
             if 'mautic-proxy' in service.get('networks', {})}
    smoke.require(peers == {'mautic'})
    web = model['services']['mautic']
    smoke.require(web['networks']['mautic-proxy'] == {
        'ipv4_address': '172.30.251.3', 'aliases': ['mautic-web']})
    smoke.require(web.get('environment', {}).get('MAUTIC_SITE_URL') == SITE_URL)
    model = copy.deepcopy(model)
    model['services'][PROXY] = {
        'image': images['caddy'], 'platform': 'linux/amd64',
        'networks': {'mautic-proxy': {'ipv4_address': '172.30.251.2'}},
        'read_only': True, 'cap_drop': ['ALL'],
        # The pinned Caddy binary's file capability needs this even on port 8080.
        'cap_add': ['NET_BIND_SERVICE'], 'security_opt': ['no-new-privileges:true'],
        'mem_limit': '256m', 'pids_limit': 128, 'logging': {'driver': 'none'},
        'tmpfs': [path + ':rw,nosuid,nodev,noexec,size=16m' for path in ('/config', '/data', '/tmp')],
        'volumes': [{'type': 'bind', 'source': str(directory / 'cache-proxy.Caddyfile'),
                     'target': '/etc/caddy/Caddyfile', 'read_only': True,
                     'bind': {'create_host_path': False}}],
    }
    return model


def probe(compose, directory, role, source, keys):
    output = smoke.command([*compose, 'exec', '-T', '--user', '33:33', role, 'php'],
                           cwd=directory, input=source, timeout=60)
    result = json.loads(output)
    smoke.require(isinstance(result, dict) and set(result) == keys)
    smoke.require(all(type(value) is bool for value in result.values()))
    print(json.dumps({role: result}, sort_keys=True))
    smoke.require(all(result.values()))


def execute(model, sources, images, directory, wait_timeout):
    smoke.require(type(wait_timeout) is int and 1 <= wait_timeout <= 600)
    model = with_proxy(model, images, directory)
    # Public, fixed fixture config only; exclusive creation refuses existing files.
    smoke.write_private(directory / 'cache-proxy.Caddyfile', CADDYFILE)
    seed = smoke.add_fixture(model, sources, images['caddy'])
    path = directory / 'cache.json'
    smoke.write_private(path, json.dumps(smoke.compose_literal(model)))
    docker = smoke.local_docker(directory)
    compose = [*smoke.compose_base(directory, model['name'], docker), '--file', str(path)]
    smoke.command([*compose, 'config', '--no-env-resolution', '--quiet'], cwd=directory)
    smoke.preflight(model, docker, directory)  # Cleanup authority only after absence proof.
    passed, cleaned = False, False
    stage = 'fixture'
    try:
        smoke.command([*compose, 'run', '--rm', '--no-deps', '--pull=never', '-T', smoke.FIXTURE],
                      cwd=directory, input=seed, timeout=60)
        up = ['up', '-d', '--pull=never', '--wait', '--wait-timeout', str(wait_timeout)]
        stage = 'startup'
        print('phase=' + stage, flush=True)
        smoke.command([*compose, *up, *smoke.GROUPS['mautic'], PROXY],
                      cwd=directory, timeout=wait_timeout + 60)
        stage = 'install'
        print('phase=' + stage, flush=True)
        # Fresh fixture ONLY. Match the image's supported pre-install sequence;
        # initialize bookkeeping before the installer marks its migrations.
        smoke.command([*compose, 'exec', '-T', '--user', '33:33', 'mautic', *CLI,
                       'doctrine:migrations:sync-metadata-storage', '--no-interaction'],
                      cwd=directory, timeout=wait_timeout + 60)
        smoke.command([*compose, 'exec', '-T', '--user', '33:33', 'mautic', *INSTALL],
                      cwd=directory, timeout=wait_timeout + 60)
        stage = 'initial-state'
        for role in ROLES:
            probe(compose, directory, role, STATE, STATE_KEYS)
        for _ in range(2):
            stage = 'recreate'
            print('phase=' + stage, flush=True)
            # Fixture-only: never recreate the database or enable async workers.
            smoke.command([*compose, *up, '--force-recreate', '--no-deps', *ROLES],
                          cwd=directory, timeout=wait_timeout + 60)
            for role in ROLES:
                probe(compose, directory, role, STATE, STATE_KEYS)
            probe(compose, directory, 'mautic', COOKIE, COOKIE_KEYS)
        passed = True
    except (Exception, KeyboardInterrupt):
        print('failed_phase=' + stage, flush=True)
    finally:
        try:
            smoke.command([*compose, 'down', '--volumes'], cwd=directory, timeout=4200)
            smoke.require_absent(model['name'], docker, directory)
            cleaned = True
        except (Exception, KeyboardInterrupt):
            pass
    return passed and cleaned, cleaned


def main(argv=None):
    parser = smoke.QuietParser(description=__doc__)
    parser.add_argument('--images', type=Path, default=smoke.DEFAULT_IMAGES)
    parser.add_argument('--run', action='store_true', help='opt in to isolated local fixture writes')
    parser.add_argument('--wait-timeout', type=int, default=600)
    args = parser.parse_args(argv)
    if not 1 <= args.wait_timeout <= 600:
        parser.error('invalid wait')
    try:
        images = smoke.load_images(args.images)
        project = 'bt-smoke-' + secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix=project + '-') as temporary:
            directory = Path(temporary).resolve()
            original = smoke.render_model(images, directory, project)
            env_dir = directory / 'env'
            smoke.bootstrap.helper.write_env(smoke.bootstrap.generate('fixture@example.invalid', 'fixture'), env_dir)
            model, sources = smoke.isolated_model(original, 'mautic', images, project, env_dir)
            if not args.run:
                preview = with_proxy(model, images, directory)
                # Count the proxy but exclude the one-shot volume initialization helper.
                count = len({s['image'] for s in preview['services'].values()})
                print(f'mautic-cache: preview services={len(preview["services"])} images={count} recreations=2')
                return 0
            passed, cleaned = execute(model, sources, images, directory, args.wait_timeout)
            print(json.dumps({'passed': passed, 'cleaned': cleaned}, sort_keys=True))
            return 0 if passed else 1
    except (Exception, KeyboardInterrupt):
        print('mautic-cache: FAIL')
        return 1


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, smoke.interrupted)
    sys.exit(main())