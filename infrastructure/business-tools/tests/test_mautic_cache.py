"""Cache contract + isolated-fixture orchestration; never start/pull containers.

Real-template checks require tofu/Compose clients, not a daemon. All other calls
are mocked. Production persisted state is never opened, reset or synthesized.
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('mautic_cache', ROOT / 'scripts/check-mautic-cache.py')
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)
smoke = cache.smoke
PROJECT = 'bt-smoke-' + '1234567890abcdef' * 2
TMPFS = '/var/www/html/var/cache:rw,nosuid,nodev,noexec,size=256m,uid=33,gid=33,mode=0770'
MARKER = 'synthetic-private-diagnostic'


class CacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.images = smoke.load_images(smoke.DEFAULT_IMAGES)
        # Orchestration-only fixture; real-template tests below own default checks.
        self.original = {'services': {name: {'image': self.images[key], 'networks': {'edge': {}}}
                                     for name, key in smoke.SERVICE_IMAGES.items()},
                         'networks': {'edge': {'driver': 'bridge'}, 'mautic-proxy': {
                             'driver': 'bridge', 'internal': True,
                             'ipam': {'config': [{'subnet': '172.30.251.0/29'}]}}}}
        self.original['services']['caddy']['networks']['mautic-proxy'] = {'ipv4_address': '172.30.251.2'}
        self.original['services']['mautic']['networks']['mautic-proxy'] = {
            'ipv4_address': '172.30.251.3', 'aliases': ['mautic-web']}
        for role in cache.ROLES:
            self.original['services'][role].update(environment={'MAUTIC_SITE_URL': cache.SITE_URL},
                tmpfs=[TMPFS], volumes=[{
                'type': 'bind', 'source': '/srv/business-tools/mautic/config',
                'target': '/var/www/html/config', 'bind': {'create_host_path': False}}])

    def test_static_anchor_and_no_cookie_entrypoint_or_proxy_override(self):
        source = (ROOT / 'mautic/compose.yaml.tftpl').read_text()
        anchor = source.split('x-mautic-app: &mautic-app\n')[1].split('\nservices:')[0]
        self.assertIn('  tmpfs:\n    - ' + TMPFS, anchor)
        self.assertEqual(source.count(TMPFS), 1)
        self.assertEqual(source.count('<<: *mautic-app'), 3)
        self.assertNotRegex(source, r'(?m)^\s*(entrypoint|command|MAUTIC_COOKIE_SECURE|MAUTIC_TRUSTED_PROXIES):')
        self.assertNotIn('cookie_secure', source)
        self.assertEqual(smoke.mautic_seed.CONTENT,
                         b"<?php\n$parameters = ['trusted_proxies' => ['172.30.251.2']];\n")

    def test_preview_and_synthetic_setup_never_execute_or_contact_daemon(self):
        output = io.StringIO()
        generate = smoke.bootstrap.generate
        with mock.patch.object(smoke, 'render_model', return_value=self.original), \
                mock.patch.object(smoke.bootstrap, 'generate', wraps=generate) as credentials, \
                mock.patch.object(cache, 'execute') as execute, \
                mock.patch.object(smoke, 'command') as command, redirect_stdout(output):
            self.assertEqual(cache.main([]), 0)
        execute.assert_not_called()
        command.assert_not_called()
        credentials.assert_called_once_with('fixture@example.invalid', 'fixture')
        self.assertEqual(output.getvalue(), 'mautic-cache: preview services=4 images=3 recreations=2\n')

    def test_http_transport_only_proxy_preserves_defaults_and_isolation(self):
        original = copy.deepcopy(self.original)
        plan, _ = smoke.isolated_model(self.original, 'mautic', self.images, PROJECT, self.directory)
        before = copy.deepcopy(plan)
        model = cache.with_proxy(plan, self.images, self.directory)
        self.assertEqual(self.original, original)
        self.assertEqual(plan, before)
        self.assertEqual({k: v for k, v in model.items() if k != 'services'},
                         {k: v for k, v in plan.items() if k != 'services'})
        self.assertEqual({k: v for k, v in model['services'].items() if k != cache.PROXY}, plan['services'])
        self.assertEqual(model['services'][cache.PROXY], {
            'image': self.images['caddy'], 'platform': 'linux/amd64',
            'networks': {'mautic-proxy': {'ipv4_address': '172.30.251.2'}},
            'read_only': True, 'cap_drop': ['ALL'], 'cap_add': ['NET_BIND_SERVICE'],
            'security_opt': ['no-new-privileges:true'], 'mem_limit': '256m', 'pids_limit': 128,
            'logging': {'driver': 'none'},
            'tmpfs': [p + ':rw,nosuid,nodev,noexec,size=16m' for p in ('/config', '/data', '/tmp')],
            'volumes': [{'type': 'bind', 'source': str(self.directory / 'cache-proxy.Caddyfile'),
                         'target': '/etc/caddy/Caddyfile', 'read_only': True,
                         'bind': {'create_host_path': False}}],
        })
        self.assertTrue(all(n['internal'] for n in model['networks'].values()))
        for name, service in model['services'].items():
            self.assertFalse(service.get('ports'))
            self.assertFalse({'MAUTIC_COOKIE_SECURE', 'MAUTIC_TRUSTED_PROXIES'} & set(service.get('environment', {})))
            if name != cache.PROXY:
                self.assertTrue(all(v['type'] == 'volume' for v in service.get('volumes', [])))
        self.assertEqual(cache.CADDYFILE, '{\n    auto_https off\n    admin off\n}\n:8080 {\n'
                         '    reverse_proxy mautic-web:80 {\n'
                         '        header_up Host marketing.makemoredigital.com\n'
                         '        header_up X-Forwarded-Proto https\n    }\n}\n')
        self.assertIn('--header "Host: marketing.makemoredigital.com" http://172.30.251.2:8080/s/login', cache.COOKIE)
        self.assertNotIn('X-Forwarded-Proto', cache.COOKIE)  # Only the trusted proxy sets it.
        self.assertNotIn('--insecure', cache.COOKIE)
        self.assertNotIn('--location', cache.COOKIE)
        self.assertNotIn('127.0.0.1', cache.COOKIE)
        self.assertFalse(list(self.directory.iterdir()))  # Model/preview has no config writes.
        model['services']['mautic']['environment']['MAUTIC_SITE_URL'] = 'changed'
        model['networks']['mautic-proxy']['ipam']['config'][0]['subnet'] = 'changed'
        self.assertEqual(plan, before)  # Deep copy, including nested existing settings.

    def test_proxy_refuses_changed_target_trust_peer_subnet_or_egress_inputs(self):
        plan, _ = smoke.isolated_model(self.original, 'mautic', self.images, PROJECT, self.directory)
        cases = [
            (('networks', 'mautic-proxy', 'driver'), 'host'),
            (('networks', 'mautic-proxy', 'internal'), False),
            (('networks', 'mautic-proxy', 'external'), True),
            (('networks', 'mautic-proxy', 'ipam', 'config'), [{'subnet': '172.30.250.0/29'}]),
            (('networks', 'edge', 'internal'), False),
            (('services', 'mautic', 'networks', 'mautic-proxy', 'ipv4_address'), '172.30.251.2'),
            (('services', 'mautic', 'networks', 'mautic-proxy', 'aliases'), ['other-web']),
            (('services', 'mautic', 'environment', 'MAUTIC_SITE_URL'), 'http://localhost'),
            (('services', 'mautic-cron', 'networks', 'mautic-proxy'), {'ipv4_address': '172.30.251.2'}),
            (('services', cache.PROXY), {'image': self.images['caddy']}),
        ]
        for keys, value in cases:
            with self.subTest(keys=keys):
                changed = copy.deepcopy(plan)
                target = changed
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                before = copy.deepcopy(changed)
                with self.assertRaises(smoke.SmokeError):
                    cache.with_proxy(changed, self.images, self.directory)
                self.assertEqual(changed, before)
        with self.assertRaises(smoke.SmokeError):
            cache.with_proxy(plan, self.images | {'caddy': 'caddy:latest'}, self.directory)
        self.assertFalse(list(self.directory.iterdir()))

    def test_proxy_config_is_exclusive_and_existing_file_is_never_replaced(self):
        plan, sources = smoke.isolated_model(self.original, 'mautic', self.images, PROJECT, self.directory)
        config = self.directory / 'cache-proxy.Caddyfile'
        config.write_text('existing-public-fixture')
        with mock.patch.object(smoke, 'command') as command, \
                mock.patch.object(smoke, 'local_docker') as docker, self.assertRaises(FileExistsError):
            cache.execute(plan, sources, self.images, self.directory, 600)
        self.assertEqual(config.read_text(), 'existing-public-fixture')
        command.assert_not_called()
        docker.assert_not_called()

    def test_proxy_and_fixture_images_all_receive_local_platform_preflight(self):
        plan, sources = smoke.isolated_model(self.original, 'mautic', self.images, PROJECT, self.directory)
        model = cache.with_proxy(plan, self.images, self.directory)
        smoke.add_fixture(model, sources, self.images['caddy'])
        with mock.patch.object(smoke, 'require_absent') as absent, \
                mock.patch.object(smoke, 'command', return_value='linux/amd64\n') as command:
            smoke.preflight(model, ['docker'], self.directory)
        absent.assert_called_once_with(PROJECT, ['docker'], self.directory)
        self.assertEqual(command.call_count, 3)
        self.assertEqual({c.args[0][-1] for c in command.call_args_list},
                         {self.images[k] for k in ('caddy', 'mautic', 'mariadb')})
        for call in command.call_args_list:
            self.assertEqual(call.args[0][:-1], ['docker', 'image', 'inspect', '--platform=linux/amd64',
                             '--format', '{{.Os}}/{{.Architecture}}'])

    def test_arguments_and_errors_are_quiet_and_run_reports_cleanup_failure(self):
        for args in (['--wait-timeout', '0'], ['--wait-timeout', '601'], ['--password', MARKER],
                     ['--proxy-url', MARKER], ['--site-url', MARKER], ['--caddyfile', MARKER]):
            output = io.StringIO()
            with redirect_stderr(output), self.assertRaises(SystemExit) as result:
                cache.main(args)
            self.assertEqual(result.exception.code, 2)
            self.assertNotIn(MARKER, output.getvalue())
        with mock.patch.object(smoke, 'render_model', return_value=self.original), \
                mock.patch.object(cache, 'execute', return_value=(False, False)) as execute, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cache.main(['--run', '--wait-timeout', '123']), 1)
        self.assertEqual(execute.call_args.args[-1], 123)
        self.assertEqual(set(execute.call_args.args[0]['services']), set(smoke.GROUPS['mautic']))
        self.assertEqual(json.loads(output.getvalue()), {'passed': False, 'cleaned': False})
        with mock.patch.object(smoke, 'load_images', side_effect=RuntimeError(MARKER)), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cache.main([]), 1)
        self.assertEqual(output.getvalue(), 'mautic-cache: FAIL\n')

    def execution(self, failure=None, cleanup_failure=False):
        plan, sources = smoke.isolated_model(self.original, 'mautic', self.images, PROJECT, self.directory)
        before, calls = copy.deepcopy(plan), []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            source = kwargs.get('input')
            if failure == 'interrupt' and 'exec' in args:
                raise KeyboardInterrupt()
            if ((failure and failure in args) or (cleanup_failure and 'down' in args)
                    or (failure == 'probe' and source == cache.STATE)
                    or (failure == 'cookie' and source == cache.COOKIE)):
                raise RuntimeError(MARKER)
            keys = cache.STATE_KEYS if source == cache.STATE else cache.COOKIE_KEYS
            return json.dumps(dict.fromkeys(keys, True)) if source in (cache.STATE, cache.COOKIE) else ''
        def check_preflight(model, docker, directory):
            self.assertEqual(set(model['services']), {*smoke.GROUPS['mautic'], cache.PROXY, smoke.FIXTURE})
            self.assertEqual(json.loads((directory / 'cache.json').read_text()), smoke.compose_literal(model))
            self.assertEqual((directory / 'cache-proxy.Caddyfile').read_text(), cache.CADDYFILE)
            self.assertEqual(model['services'][cache.PROXY]['volumes'][0]['source'],
                             str(directory / 'cache-proxy.Caddyfile'))
            self.assertFalse(any('run' in args or 'up' in args for args, _ in calls))
        with tempfile.TemporaryDirectory(dir=self.directory) as temporary, \
                mock.patch.object(smoke, 'command', side_effect=runner), \
                mock.patch.object(smoke, 'local_docker', return_value=['docker']), \
                mock.patch.object(smoke, 'preflight', side_effect=check_preflight) as preflight, \
                mock.patch.object(smoke, 'require_absent', side_effect=smoke.SmokeError if failure == 'absence' else None) as absent, \
                redirect_stdout(io.StringIO()) as output:
            result = cache.execute(plan, sources, self.images, Path(temporary), 600)
        preflight.assert_called_once()
        if not cleanup_failure:
            absent.assert_called_once_with(PROJECT, ['docker'], Path(temporary))
        self.assertEqual(plan, before)
        self.assertNotIn(MARKER, output.getvalue())
        return result, calls

    def test_supported_install_twice_recreate_boolean_probes_and_exact_cleanup(self):
        result, calls = self.execution()
        self.assertEqual(result, (True, True))
        commands = [args for args, _ in calls]
        fixture = next((args, kw) for args, kw in calls if 'run' in args)
        self.assertEqual(fixture[0][-6:], ['run', '--rm', '--no-deps', '--pull=never', '-T', smoke.FIXTURE])
        self.assertEqual(fixture[1]['input'].encode(), smoke.mautic_seed.CONTENT)
        install = next(args for args in commands if 'mautic:install' in args)
        self.assertEqual(install[install.index('exec'):],
                         ['exec', '-T', '--user', '33:33', 'mautic', *cache.INSTALL])
        self.assertEqual(cache.INSTALL[len(cache.CLI) + 1:], ['--admin_email', 'fixture@example.invalid',
                         '--admin_username', 'fixtureadmin', '--admin_password', 'synthetic-fixture-only',
                         '--admin_firstname', 'Synthetic', '--admin_lastname', 'Fixture', '--force', cache.SITE_URL])
        sync = next(args for args in commands if 'doctrine:migrations:sync-metadata-storage' in args)
        self.assertEqual(sync[sync.index('exec'):], ['exec', '-T', '--user', '33:33',
                         'mautic', *cache.CLI, 'doctrine:migrations:sync-metadata-storage', '--no-interaction'])
        self.assertLess(commands.index(sync), commands.index(install))
        starts = [args[args.index('up'):] for args in commands if 'up' in args]
        up = ['up', '-d', '--pull=never', '--wait', '--wait-timeout', '600']
        recreate = [*up, '--force-recreate', '--no-deps', *cache.ROLES]
        self.assertEqual(starts, [[*up, *smoke.GROUPS['mautic'], cache.PROXY], recreate, recreate])
        self.assertEqual(sum(kw.get('input') == cache.STATE for _, kw in calls), 6)
        self.assertEqual(sum(kw.get('input') == cache.COOKIE for _, kw in calls), 2)
        for args in commands:
            self.assertIn(PROJECT, args)
            self.assertFalse({'logs', 'prune', '--remove-orphans', 'mautic-worker'} & set(args))
        self.assertEqual(commands[-1][-2:], ['down', '--volumes'])
        self.assertEqual(calls[-1][1]['timeout'], 4200)
        cookies = [args for args, kw in calls if kw.get('input') == cache.COOKIE]
        self.assertTrue(all(args[args.index('exec'):] == ['exec', '-T', '--user', '33:33', 'mautic', 'php']
                            for args in cookies))

    def test_fixture_start_install_recreate_probe_and_interrupt_failures_cleanup(self):
        for phase in ('run', 'up', cache.PROXY, 'doctrine:migrations:sync-metadata-storage', 'mautic:install',
                      '--force-recreate', 'probe', 'cookie', 'interrupt'):
            with self.subTest(phase=phase):
                result, calls = self.execution(failure=phase)
                self.assertEqual(result, (False, True))
                self.assertEqual(calls[-1][0][-2:], ['down', '--volumes'])
        self.assertEqual(self.execution(cleanup_failure=True)[0], (False, False))
        self.assertEqual(self.execution(failure='absence')[0], (False, False))

    def test_exec_compatibility_replays_only_entrypoint_port_without_persisting_defaults(self):
        self.assertEqual(cache.CLI, ['bash', '-c',
                         'export MAUTIC_DB_PORT="${MAUTIC_DB_PORT:-3306}"; exec php "$@"',
                         'mautic-fixture-cli', '/var/www/html/bin/console'])
        self.assertNotIn('MAUTIC_DB_PORT', (ROOT / 'mautic/compose.yaml.tftpl').read_text())

    def test_preflight_failure_never_authorizes_cleanup(self):
        plan, sources = smoke.isolated_model(self.original, 'mautic', self.images, PROJECT, self.directory)
        with mock.patch.object(smoke, 'local_docker', return_value=['docker']), \
                mock.patch.object(smoke, 'preflight', side_effect=smoke.SmokeError), \
                mock.patch.object(smoke, 'command', return_value='') as command, self.assertRaises(smoke.SmokeError):
            cache.execute(plan, sources, self.images, self.directory, 600)
        self.assertFalse(any('down' in call.args[0] for call in command.call_args_list))

    def test_probe_rejects_unexpected_nonboolean_false_or_sensitive_results(self):
        valid = dict.fromkeys(cache.COOKIE_KEYS, True)
        for response in ({}, valid | {'unexpected': True}, valid | {'secure_cookie': MARKER},
                         valid | {'secure_cookie': 1}, valid | {'secure_cookie': False}, [True]):
            with mock.patch.object(smoke, 'command', return_value=json.dumps(response)), \
                    redirect_stdout(io.StringIO()) as output, self.assertRaises(smoke.SmokeError):
                cache.probe(['docker', 'compose'], self.directory, 'mautic', cache.COOKIE, cache.COOKIE_KEYS)
            self.assertNotIn(MARKER, output.getvalue())


@unittest.skipUnless(shutil.which('tofu') and shutil.which('docker'), 'tofu and Compose client required')
class RealTemplateTests(unittest.TestCase):
    def test_three_roles_private_cache_preserves_retained_state_and_other_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            images = smoke.load_images(smoke.DEFAULT_IMAGES)
            original = smoke.render_model(images, directory, PROJECT)
            env_dir = directory / 'env'
            smoke.bootstrap.helper.write_env(smoke.bootstrap.generate('fixture@example.invalid', 'fixture'), env_dir)
            web = original['services']['mautic']
            for name in (*cache.ROLES, 'mautic-worker'):
                role = original['services'][name]
                self.assertEqual(role['tmpfs'], [TMPFS])
                self.assertEqual(role['volumes'], web['volumes'])
                self.assertEqual(len(role['volumes']), 4)
                for key in ('entrypoint', 'command', 'user'):
                    self.assertIsNone(role.get(key))  # Compose may render inherited image values as null.
                self.assertFalse({'MAUTIC_COOKIE_SECURE', 'MAUTIC_TRUSTED_PROXIES'} & set(role['environment']))
                self.assertEqual(role['environment']['MAUTIC_SITE_URL'], cache.SITE_URL)
                self.assertGreater(int(role['mem_limit']), 0)
            self.assertEqual({v['source'] for v in web['volumes']},
                             {'/srv/business-tools/mautic/' + p for p in ('config', 'logs', 'media/files', 'media/images')})
            self.assertTrue(all(v['type'] == 'bind' and v['bind']['create_host_path'] is False for v in web['volumes']))
            self.assertEqual({v['target'] for v in web['volumes']},
                             {'/var/www/html/' + p for p in ('config', 'var/logs', 'docroot/media/files', 'docroot/media/images')})
            self.assertEqual(original['services']['mautic-worker']['profiles'], ['mautic-workers'])
            self.assertEqual(web['networks']['mautic-proxy']['ipv4_address'], '172.30.251.3')
            self.assertEqual(web['healthcheck']['test'], ['CMD', 'curl', '--fail', '--silent', '--output',
                             '/dev/null', '--max-time', '5', 'http://localhost/'])
            plan, sources = smoke.isolated_model(original, 'mautic', images, PROJECT, env_dir)
            self.assertEqual(len(sources), 5)  # Config, logs, two media directories, DB; never cache.
            self.assertTrue(all(n['internal'] for n in plan['networks'].values()))
            before = copy.deepcopy(plan)
            proxied = cache.with_proxy(plan, images, directory)
            self.assertEqual(plan, before)
            self.assertEqual(proxied['networks'], plan['networks'])
            self.assertEqual(proxied['volumes'], plan['volumes'])
            self.assertEqual({k: v for k, v in proxied['services'].items() if k != cache.PROXY}, plan['services'])
            proxy = proxied['services'][cache.PROXY]
            production_proxy = original['services']['caddy']
            for key in ('read_only', 'cap_drop', 'cap_add', 'security_opt', 'pids_limit'):
                self.assertEqual(proxy[key], production_proxy[key])
            self.assertEqual(int(production_proxy['mem_limit']), 256 * 1024 * 1024)
            self.assertEqual(proxy['networks']['mautic-proxy'], production_proxy['networks']['mautic-proxy'])
            self.assertFalse({'entrypoint', 'command', 'user', 'ports', 'env_file'} & set(proxy))
            for name, service in plan['services'].items():
                changed = {'platform', 'ports', 'volumes', 'env_file'}
                self.assertEqual({k: v for k, v in service.items() if k not in changed},
                                 {k: v for k, v in original['services'][name].items() if k not in changed})
                self.assertFalse(service.get('ports'))
                self.assertTrue(all(v['type'] == 'volume' and v['volume']['nocopy'] for v in service['volumes']))


if __name__ == '__main__':
    unittest.main()