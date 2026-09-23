"""Isolated smoke-harness tests: never start containers, pull images or use cloud.

The real-template tests need tofu and the Compose client, but NOT a Docker daemon.
Other tests mock every subprocess. No host data or persisted credentials are read.
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bt_smoke_images', ROOT / 'scripts/smoke-images.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
PROJECT = 'bt-smoke-' + '1234567890abcdef' * 2
MARKER = 'synthetic-sensitive-diagnostic'


def minimal_model(images):
    """Parser/orchestration fixture only; default-policy tests render real sources."""
    model = {
        'services': {name: {'image': images[key], 'networks': {'edge': None}}
                     for name, key in smoke.SERVICE_IMAGES.items()},
        'networks': {'edge': {'name': 'business-tools_edge', 'driver': 'bridge'}},
    }
    model['services']['actual']['volumes'] = [{
        'type': 'bind', 'source': '/srv/business-tools/actual-budget/data', 'target': '/data',
        'bind': {'create_host_path': False},
    }]
    return model


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.images = smoke.load_images(smoke.DEFAULT_IMAGES)
        self.model = minimal_model(self.images)

    def plan(self):
        return smoke.isolated_model(self.model, 'actual', self.images, PROJECT, self.directory)

    def test_release_refs_require_selected_repository_family_and_digest(self):
        self.assertEqual(set(smoke.validate_images(self.images)), set(smoke.IMAGE_RULES))
        valid = self.images['actual']
        invalid = [valid.split('@')[0], valid.replace('actualbudget/', 'other/'),
                   valid.replace(':26.9.0', ':latest'), valid.replace(':26.9.0', ':25.9.0'),
                   valid.split('@')[0] + '@sha256:' + 'a' * 64, valid + '\n',
                   valid.replace('@sha256:', '@sha512:')]
        for reference in invalid:
            with self.subTest(reference=reference), self.assertRaises(smoke.SmokeError):
                smoke.validate_images(self.images | {'actual': reference})
        for images in ({}, self.images | {'unknown': valid},
                       self.images | {'temporal_postgres': self.images['postgres']}):
            with self.assertRaises(smoke.SmokeError):
                smoke.validate_images(images)
        self.model['services']['actual']['image'] = self.images['grafana']
        with self.assertRaises(smoke.SmokeError):
            self.plan()

    def test_refuses_unknown_data_assets_envs_and_mount_mechanisms(self):
        original = copy.deepcopy(self.model)
        for source in ('/srv/business-tools/actual-budget/unknown',
                       '/srv/business-tools/actual-budget/../actual-budget/data',
                       '/srv/business-tools/backups', '/srv/business-tools/postiz/config',
                       '/opt/business-tools/../private', '/opt/business-tools/deployment.json',
                       '/run/business-tools/postiz.env', '/var/run/docker.sock', '/tmp/data', '/proc'):
            self.model = copy.deepcopy(original)
            self.model['services']['actual']['volumes'][0]['source'] = source
            with self.subTest(source=source), self.assertRaises(smoke.SmokeError):
                self.plan()
        for change in ({'type': 'volume'}, {'bind': {'create_host_path': True}},
                       {'bind': {'propagation': 'rshared'}}, {'volume': {'subpath': '../data'}}):
            self.model = copy.deepcopy(original)
            self.model['services']['actual']['volumes'][0].update(change)
            with self.assertRaises(smoke.SmokeError):
                self.plan()
        for path in ('/run/business-tools/../actual.env', '/tmp/postiz.env', 'postiz.env'):
            self.model = copy.deepcopy(original)
            self.model['services']['actual']['env_file'] = [{'path': path, 'format': 'raw'}]
            with self.assertRaises(smoke.SmokeError):
                self.plan()

    def test_refuses_namespace_network_and_profile_escapes(self):
        original = copy.deepcopy(self.model)
        for override in ({'network_mode': 'host'}, {'privileged': True}, {'devices': ['/dev/sda']},
                         {'volumes_from': ['other']}, {'profiles': ['mautic-workers']},
                         {'container_name': 'production'}, {'build': '.'},
                         {'platform': 'linux/arm64'}, {'depends_on': {'caddy': {}}},
                         {'secrets': ['production']}, {'configs': ['production']}):
            self.model = copy.deepcopy(original)
            self.model['services']['actual'].update(override)
            with self.assertRaises(smoke.SmokeError):
                self.plan()
        for override in ({'external': True}, {'driver': 'host'}, {'driver_opts': {'x': 'y'}},
                         {'ipam': {'driver': 'custom'}}):
            self.model = copy.deepcopy(original)
            self.model['networks']['edge'].update(override)
            with self.assertRaises(smoke.SmokeError):
                self.plan()
        with self.assertRaises(smoke.SmokeError):
            smoke.compose_base(self.directory, 'business-tools')

    def test_checked_files_refuse_traversal_symlinks_hardlinks_and_directories(self):
        file = self.directory / 'public'
        file.write_text('public')
        self.assertEqual(smoke.checked_file(self.directory, 'public'), str(file))
        (self.directory / 'alias').symlink_to(file)
        (self.directory / 'directory').mkdir()
        os.link(file, self.directory / 'hardlink')
        for relative in ('../public', str(file), './public', 'alias', 'public', 'hardlink', 'directory'):
            with self.subTest(relative=relative), self.assertRaises(smoke.SmokeError):
                smoke.checked_file(self.directory, relative)

    def test_preview_prints_counts_only_and_never_calls_execution(self):
        output = io.StringIO()
        with mock.patch.object(smoke, 'render_model', return_value=self.model), \
                mock.patch.object(smoke, 'execute_group') as execute, \
                mock.patch.object(smoke, 'command') as command, redirect_stdout(output):
            self.assertEqual(smoke.main([]), 0)
        execute.assert_not_called()
        command.assert_not_called()
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], 'groups=5')
        self.assertEqual(len(lines), 6)
        for group, line in zip(smoke.GROUPS, lines[1:]):
            self.assertRegex(line, '^' + group + r': services=[0-9]+ images=[0-9]+$')

    def test_run_needs_one_group_and_bounded_wait_with_quiet_errors(self):
        for args in (['--run'], ['--wait-timeout', '0'], ['--wait-timeout', '601'],
                     ['--group', MARKER], ['--wait-timeout', MARKER]):
            output = io.StringIO()
            with redirect_stderr(output), self.assertRaises(SystemExit) as exit_result:
                smoke.main(args)
            self.assertEqual(exit_result.exception.code, 2)
            self.assertNotIn(MARKER, output.getvalue())

    def test_published_ports_are_removed_only_in_named_smoke_exception(self):
        self.model['services']['actual']['ports'] = [{'published': '5006', 'target': 5006}]
        plan, _ = self.plan()
        self.assertNotIn('ports', plan['services']['actual'])
        self.assertTrue(self.model['services']['actual']['ports'])

    def test_main_one_group_and_cleanup_failure_are_reported_without_diagnostics(self):
        output = io.StringIO()
        with mock.patch.object(smoke, 'render_model', return_value=self.model), \
                mock.patch.object(smoke, 'execute_group', return_value=(False, False)) as execute, \
                redirect_stdout(output):
            self.assertEqual(smoke.main(['--run', '--group', 'actual']), 1)
        self.assertEqual(set(execute.call_args.args[0]['services']), {'actual'})
        self.assertEqual(execute.call_args.args[-1], 600)
        self.assertEqual(output.getvalue(), 'actual: cleanup FAIL\nactual: FAIL\n')
        output = io.StringIO()
        with mock.patch.object(smoke, 'load_images', side_effect=RuntimeError(MARKER)), redirect_stdout(output):
            self.assertEqual(smoke.main(['--group', 'actual']), 1)
        self.assertEqual(output.getvalue(), 'actual: FAIL\n')

    def test_subprocess_overrides_are_not_inherited_and_diagnostics_are_captured(self):
        result = subprocess.CompletedProcess(['tool'], 1, MARKER, MARKER)
        injected = {'COMPOSE_PROFILES': 'mautic-workers', 'COMPOSE_FILE': '/private',
                    'DOCKER_HOST': 'ssh://remote', 'TF_CLI_ARGS': MARKER, 'TF_LOG': 'TRACE'}
        with mock.patch.dict(os.environ, injected), mock.patch.object(smoke.subprocess, 'run', return_value=result) as run:
            with self.assertRaises(smoke.SmokeError) as error:
                smoke.command(['tool'], cwd=self.directory)
        self.assertEqual(str(error.exception), '')
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertTrue(set(run.call_args.kwargs['env']) <= {'PATH', 'HOME', 'TMPDIR'})
        self.assertFalse(set(injected) & set(run.call_args.kwargs['env']))

    def test_synthetic_raw_envs_use_existing_writer_without_optional_delivery_settings(self):
        credentials = smoke.bootstrap.generate('smoke@example.invalid', 'smoke')
        self.assertTrue(all('extra_env' not in section for section in credentials.values()))
        files = smoke.bootstrap.helper.environment_files(credentials)
        self.assertEqual(set(files), set(smoke.disk.ENV_FILES))
        self.assertTrue(files['invoice-ninja.env']['IN_USER_EMAIL'] == 'smoke@example.invalid')
        self.assertTrue(files['grafana.env']['GF_SECURITY_ADMIN_USER'] == 'smoke')
        self.assertTrue(files['mautic.env']['MAUTIC_DB_PASSWORD'] == files['mautic-db.env']['MARIADB_PASSWORD'])
        self.assertTrue(files['invoice-ninja.env']['DB_PASSWORD'] == files['invoice-db.env']['MARIADB_PASSWORD'])
        self.assertTrue(files['temporal.env']['POSTGRES_PWD'] == files['temporal-db.env']['POSTGRES_PASSWORD'])
        env_dir = self.directory / 'runtime'
        smoke.bootstrap.helper.write_env(credentials, env_dir)
        self.assertEqual(stat.S_IMODE(env_dir.stat().st_mode), 0o700)
        for filename, values in files.items():
            path = env_dir / filename
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(path.read_text() == ''.join(k + '=' + v + '\n' for k, v in sorted(values.items())))
            self.assertFalse(any(k.startswith(('MAIL_', 'GF_SMTP_', 'EMAIL_', 'MAUTIC_MESSENGER_')) for k in values))

    def test_render_uses_empty_tofu_workspace_real_templates_and_unresolved_compose(self):
        def runner(args, **kwargs):
            if args[0] == 'tofu':
                self.assertEqual(list(Path(kwargs['cwd']).iterdir()), [])
                self.assertEqual(args, ['tofu', 'console', '-no-color'])
                for relative in ['templates', *smoke.disk.APPS]:
                    self.assertIn(str(ROOT / relative / 'compose.yaml.tftpl'), kwargs['input'])
                return json.dumps(json.dumps(['services: {}'] * 6))
            self.assertEqual(args[-4:], ['config', '--no-env-resolution', '--format', 'json'])
            self.assertEqual(args.count('--file'), 6)
            self.assertIn(str(self.directory / 'empty.env'), args)
            return json.dumps(self.model)
        with mock.patch.object(smoke, 'command', side_effect=runner):
            self.assertEqual(smoke.render_model(self.images, self.directory, PROJECT), self.model)

    def test_local_only_context_and_image_platform_preflight(self):
        with mock.patch.object(smoke, 'command', side_effect=['desktop-linux', 'unix:///tmp/docker.sock']):
            self.assertEqual(smoke.local_docker(self.directory), ['docker', '--context', 'desktop-linux'])
        for endpoint in ('ssh://host', 'tcp://127.0.0.1:2375', 'https://remote'):
            with mock.patch.object(smoke, 'command', side_effect=['remote', endpoint]), \
                    self.assertRaises(smoke.SmokeError):
                smoke.local_docker(self.directory)
        plan, sources = self.plan()
        smoke.add_fixture(plan, sources, self.images['caddy'])
        with mock.patch.object(smoke, 'require_absent'), \
                mock.patch.object(smoke, 'command', return_value='linux/amd64') as command:
            smoke.preflight(plan, ['docker'], self.directory)
        self.assertTrue(all('--platform=linux/amd64' in call.args[0]
                            for call in command.call_args_list))
        for platform in ('linux/arm64', ''):
            with mock.patch.object(smoke, 'require_absent'), \
                    mock.patch.object(smoke, 'command', return_value=platform), self.assertRaises(smoke.SmokeError):
                smoke.preflight(plan, ['docker'], self.directory)

    def test_preexisting_name_or_label_is_refused_without_cleanup(self):
        for response in (PROJECT + '-data-actual-budget-data\n', 'unrelated-labelled-object\n'):
            replies = [response] if response.startswith(PROJECT) else ['', response]
            with mock.patch.object(smoke, 'command', side_effect=replies), self.assertRaises(smoke.SmokeError):
                smoke.require_absent(PROJECT, ['docker'], self.directory)
        plan, sources = self.plan()
        with mock.patch.object(smoke, 'local_docker', return_value=['docker']), \
                mock.patch.object(smoke, 'preflight', side_effect=smoke.SmokeError), \
                mock.patch.object(smoke, 'command', return_value='') as command, self.assertRaises(smoke.SmokeError):
            smoke.execute_group(plan, sources, self.images, self.directory, 600)
        self.assertFalse(any('down' in call.args[0] for call in command.call_args_list))

    def execution(self, failure=None, cleanup_failure=False):
        plan, sources = self.plan()
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if (failure and failure in args) or (cleanup_failure and 'down' in args):
                raise RuntimeError(MARKER)
            return ''
        with mock.patch.object(smoke, 'command', side_effect=runner), \
                mock.patch.object(smoke, 'local_docker', return_value=['docker']), \
                mock.patch.object(smoke, 'preflight'), mock.patch.object(smoke, 'require_absent'), \
                mock.patch.object(smoke, 'report_states', return_value=True):
            result = smoke.execute_group(plan, sources, self.images, self.directory, 600)
        return result, calls

    def test_selected_execution_uses_pinned_helper_wait_and_exact_project_cleanup(self):
        result, calls = self.execution()
        self.assertEqual(result, (True, True))
        commands = [args for args, _ in calls]
        start = next(args for args in commands if 'up' in args)
        self.assertEqual(start[start.index('up'):],
                         ['up', '-d', '--pull=never', '--wait', '--wait-timeout', '600', 'actual'])
        fixture = next(args for args in commands if 'run' in args)
        self.assertEqual(fixture[fixture.index('run'):],
                         ['run', '--rm', '--no-deps', '--pull=never', '-T', smoke.FIXTURE])
        self.assertEqual(commands[-1][-2:], ['down', '--volumes'])
        for args in commands:
            self.assertIn(PROJECT, args)
            self.assertFalse({'prune', '--remove-orphans', 'logs', '--force', '--timeout'} & set(args))
        self.assertEqual(calls[-1][1]['timeout'], 4200)
        self.assertEqual(stat.S_IMODE((self.directory / 'smoke.json').stat().st_mode), 0o600)

    def test_start_failure_still_cleans_only_owned_project(self):
        result, calls = self.execution(failure='up')
        self.assertEqual(result, (False, True))
        self.assertEqual(calls[-1][0][-2:], ['down', '--volumes'])

    def test_fixture_failure_never_starts_apps_and_still_cleans(self):
        result, calls = self.execution(failure='run')
        self.assertEqual(result, (False, True))
        self.assertFalse(any('up' in args for args, _ in calls))
        self.assertEqual(calls[-1][0][-2:], ['down', '--volumes'])

    def test_cleanup_failure_makes_successful_start_fail(self):
        self.assertEqual(self.execution(cleanup_failure=True)[0], (False, False))

    def test_remaining_project_objects_after_down_are_a_cleanup_failure(self):
        plan, sources = self.plan()
        with mock.patch.object(smoke, 'local_docker', return_value=['docker']), \
                mock.patch.object(smoke, 'command', return_value=''), \
                mock.patch.object(smoke, 'preflight'), \
                mock.patch.object(smoke, 'report_states', return_value=True), \
                mock.patch.object(smoke, 'require_absent', side_effect=smoke.SmokeError):
            self.assertEqual(smoke.execute_group(plan, sources, self.images, self.directory, 600),
                             (False, False))

    def test_metadata_is_strictly_allowlisted_and_never_prints_health_errors(self):
        plan, _ = self.plan()
        name = PROJECT + '-actual-1'
        output = io.StringIO()
        with mock.patch.object(smoke, 'command', return_value=json.dumps([name, 'running', 'healthy', 0])) as command, \
                redirect_stdout(output):
            self.assertTrue(smoke.report_states(['docker', 'compose'], plan, self.directory))
        self.assertEqual(output.getvalue(), f'{name} state=running health=healthy exit=0\n')
        self.assertEqual(command.call_args.args[0][-1],
                         '[{{json .Name}},{{json .State}},{{json .Health}},{{json .ExitCode}}]')
        for row in ([MARKER, 'running', 'healthy', 0], [name, 'running', MARKER, 0],
                    [name, 'running', 'healthy', MARKER]):
            output = io.StringIO()
            with mock.patch.object(smoke, 'command', return_value=json.dumps(row)), \
                    redirect_stdout(output), self.assertRaises(smoke.SmokeError):
                smoke.report_states(['docker', 'compose'], plan, self.directory)
            self.assertEqual(output.getvalue(), '')

    def test_missing_or_failed_post_wait_state_cannot_pass(self):
        plan, _ = self.plan()
        name = PROJECT + '-actual-1'
        for row in (None, [name, 'exited', 'healthy', 1], [name, 'running', 'unhealthy', 0],
                    [name, 'running', 'starting', 0]):
            response = json.dumps(row) if row else ''
            with mock.patch.object(smoke, 'command', return_value=response), redirect_stdout(io.StringIO()):
                self.assertFalse(smoke.report_states(['docker', 'compose'], plan, self.directory))

    def test_serialization_preserves_literal_dollars_without_mutating_model(self):
        original = {'environment': {'VALUE': '$LITERAL'}, 'command': ['test -z "$(ls -A /fixture/0)"']}
        result = smoke.compose_literal(original)
        self.assertEqual(result['environment']['VALUE'], '$$LITERAL')
        self.assertIn('$$(ls', result['command'][0])
        self.assertEqual(original['environment']['VALUE'], '$LITERAL')


@unittest.skipUnless(shutil.which('tofu') and shutil.which('docker'), 'tofu and Compose client required')
class RealTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name).resolve()
        cls.images = smoke.load_images(smoke.DEFAULT_IMAGES)
        cls.original = smoke.render_model(cls.images, cls.directory, PROJECT)
        cls.env_dir = cls.directory / 'env'
        smoke.bootstrap.helper.write_env(smoke.bootstrap.generate('smoke@example.invalid', 'smoke'), cls.env_dir)

    def plan(self, group):
        return smoke.isolated_model(self.original, group, self.images, PROJECT, self.env_dir)

    def test_all_default_settings_and_dependencies_survive_named_isolation_exception(self):
        before = copy.deepcopy(self.original)
        for group, names in smoke.GROUPS.items():
            plan, _ = self.plan(group)
            self.assertEqual(set(plan['services']), set(names))
            self.assertNotIn('caddy', plan['services'])
            self.assertNotIn('mautic-worker', plan['services'])
            for name, service in plan['services'].items():
                source = self.original['services'][name]
                changed = {'platform', 'ports', 'volumes', 'env_file'}
                self.assertEqual({k: v for k, v in source.items() if k not in changed},
                                 {k: v for k, v in service.items() if k not in changed})
                self.assertEqual(service['platform'], 'linux/amd64')
                self.assertFalse(service.get('ports'))
                self.assertGreater(int(service['mem_limit']), 0)
                self.assertTrue(set(service.get('depends_on', {})) <= set(names))
        self.assertEqual(before, self.original)
        actual = self.plan('actual')[0]['services']['actual']
        self.assertNotIn('environment', actual)
        self.assertEqual(actual['user'], '1001:1001')
        mautic = self.plan('mautic')[0]['services']
        self.assertNotIn('MAUTIC_MESSENGER_DSN_EMAIL', mautic['mautic']['environment'])
        self.assertNotIn('healthcheck', mautic['mautic-cron'])
        invoice = self.plan('invoice-ninja')[0]['services']['invoice-app']
        self.assertNotIn('test', invoice['healthcheck'])  # Inherit the image's probe.
        for key in ('QUEUE_CONNECTION', 'CACHE_DRIVER', 'SESSION_DRIVER'):
            self.assertEqual(invoice['environment'][key], 'redis')
        self.assertEqual(invoice['environment']['REQUIRE_HTTPS'], 'true')

    def test_all_networks_internal_no_global_names_and_reserved_trust_unchanged(self):
        for group in smoke.GROUPS:
            plan, _ = self.plan(group)
            for name, network in plan['networks'].items():
                self.assertTrue(network['internal'])
                self.assertNotIn('name', network)
                self.assertFalse(network.get('external'))
                self.assertEqual(network.get('ipam'), self.original['networks'][name].get('ipam'))
        for group, network, service, prefix in (
                ('mautic', 'mautic-proxy', 'mautic', '172.30.251.'),
                ('invoice-ninja', 'invoice-render', 'invoice-app', '172.30.252.')):
            plan, _ = self.plan(group)
            self.assertEqual(plan['networks'][network]['ipam']['config'][0]['subnet'], prefix + '0/29')
            self.assertEqual(plan['services'][service]['networks'][network]['ipv4_address'], prefix + '3')

    def test_only_fresh_volumes_public_assets_and_readonly_kernel_views_are_mounted(self):
        for group in smoke.GROUPS:
            plan, sources = self.plan(group)
            self.assertEqual(set(plan['volumes']), set(sources))
            for name, service in plan['services'].items():
                for original, mount in zip(self.original['services'][name].get('volumes', []), service.get('volumes', [])):
                    self.assertEqual(original['target'], mount['target'])
                    self.assertEqual(original.get('read_only'), mount.get('read_only'))
                    if mount['type'] == 'volume':
                        self.assertEqual(sources[mount['source']], original['source'])
                        self.assertTrue(mount['volume']['nocopy'])
                        self.assertTrue(plan['volumes'][mount['source']]['name'].startswith(PROJECT + '-'))
                    else:
                        self.assertTrue(mount['read_only'])
                        self.assertFalse(mount['bind']['create_host_path'])
                        self.assertIn(mount['source'], {'/proc', '/sys'} | {str(ROOT / p) for p in smoke.ASSETS})
                for env_file in service.get('env_file', []):
                    self.assertEqual(env_file['format'], 'raw')
                    self.assertEqual(Path(env_file['path']).parent, self.env_dir)
                    self.assertEqual(stat.S_IMODE(Path(env_file['path']).stat().st_mode), 0o600)

    def test_shared_data_stays_shared_and_each_invocation_has_unique_volume_names(self):
        plan, _ = self.plan('invoice-ninja')
        frontend = plan['services']['invoice-ninja']['volumes'][1:]
        backend = plan['services']['invoice-app']['volumes']
        self.assertEqual([v['source'] for v in frontend], [v['source'] for v in backend])
        other, _ = smoke.isolated_model(self.original, 'invoice-ninja', self.images,
                                        'bt-smoke-' + 'f' * 32, self.env_dir)
        self.assertFalse({v['name'] for v in plan['volumes'].values()} &
                         {v['name'] for v in other['volumes'].values()})

    def test_fresh_volume_helper_uses_production_ownership_and_exact_mautic_seed(self):
        for group in smoke.GROUPS:
            plan, sources = self.plan(group)
            seed = smoke.add_fixture(plan, sources, self.images['caddy'])
            fixture = plan['services'][smoke.FIXTURE]
            self.assertEqual(fixture['image'], self.images['caddy'])
            self.assertEqual(fixture['network_mode'], 'none')
            self.assertEqual(fixture['entrypoint'], ['/bin/sh'])
            self.assertEqual(fixture['platform'], 'linux/amd64')
            script = fixture['command'][-1]
            self.assertLess(script.rindex('test -z'), script.index('chown'))
            self.assertNotIn('chown -R', script)
            self.assertNotIn('/srv/', script)
            for mount in fixture['volumes']:
                self.assertEqual(mount['type'], 'volume')
                self.assertTrue(mount['target'].startswith('/fixture/'))
                uid, gid, mode = smoke.DIRECTORIES[sources[mount['source']]]
                self.assertIn(f'chown {uid}:{gid} {mount["target"]}', script)
                self.assertIn(f'chmod {mode:o} {mount["target"]}', script)
            if group == 'mautic':
                self.assertEqual(seed.encode(), smoke.mautic_seed.CONTENT)
                self.assertIn('parameters_local.php', script)
                self.assertNotIn(seed, script)  # Seed is stdin, never shell evaluation/argv.
            else:
                self.assertEqual(seed, '')
                self.assertNotIn('parameters_local.php', script)

    def test_transformed_documents_validate_without_env_resolution_or_containers(self):
        for group in smoke.GROUPS:
            plan, sources = self.plan(group)
            smoke.add_fixture(plan, sources, self.images['caddy'])
            path = self.directory / (group + '.json')
            smoke.write_private(path, json.dumps(smoke.compose_literal(plan)))
            invocation = smoke.compose_base(self.directory, PROJECT)
            smoke.command([*invocation, '--file', str(path), '--profile', smoke.FIXTURE,
                           'config', '--no-env-resolution', '--quiet'], cwd=self.directory)


if __name__ == '__main__':
    unittest.main()