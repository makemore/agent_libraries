"""Cold-restore safety regressions; no Docker runtime (optional offline config)."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bt_restore', ROOT / 'scripts/restore-databases.py')
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)
smoke = restore.smoke
PROJECT = 'bt-smoke-' + '1234567890abcdef' * 2
MARKER = 'synthetic-sensitive-diagnostic'


class RestoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.images = smoke.load_images(smoke.DEFAULT_IMAGES)
        self.env_dir = self.directory / 'env'
        smoke.bootstrap.helper.write_env(smoke.bootstrap.generate('restore@example.invalid', 'restore'), self.env_dir)
        smoke.write_private(self.env_dir / 'client.cnf', MARKER)
        # Orchestration fixture only; real-template preservation is checked below.
        self.original = {
            'services': {n: {'image': self.images[k], 'networks': {'private': None}}
                         for n, k in smoke.SERVICE_IMAGES.items()},
            'networks': {'private': {'driver': 'bridge'}},
        }
        for key, (group, name, target) in restore.DATABASES.items():
            leaf = 'temporal-postgres' if key == 'temporal_postgres' else key
            self.original['services'][name].update(
                volumes=[{'type': 'bind', 'source': f'/srv/business-tools/{group}/{leaf}',
                          'target': target, 'bind': {'create_host_path': False}}],
                healthcheck={'test': ['CMD', 'probe']}, stop_grace_period='90s',
                environment={'MARIADB_USER': 'mautic'})

    def plan(self):
        return restore.build_model(self.original, self.images, PROJECT, self.env_dir, tuple(restore.DATABASES))

    def test_named_exception_keeps_defaults_and_isolates_every_mount(self):
        before = copy.deepcopy(self.original)
        plan = self.plan()
        self.assertEqual(len(plan['volumes']), 6)
        for key, (_, name, target) in restore.DATABASES.items():
            for role in ('source', 'restored'):
                service = plan['services'][key + '-' + role]
                self.assertEqual(service['healthcheck'], self.original['services'][name]['healthcheck'])
                self.assertEqual(service['stop_grace_period'], '90s')
                self.assertEqual(service['volumes'][0]['target'], target)
                self.assertEqual(service['platform'], 'linux/amd64')
            transfer = plan['services'][key + '-transfer']
            self.assertEqual(transfer['image'], self.images['postgres'])
            self.assertEqual(transfer['network_mode'], 'none')
            self.assertEqual(transfer['entrypoint'], ['/bin/bash'])
            self.assertEqual(transfer['command'][:2], ['-euo', 'pipefail'])
            self.assertEqual(transfer['mem_limit'], '512m')
            self.assertTrue(transfer['volumes'][0]['read_only'])
            self.assertNotIn('read_only', transfer['volumes'][1])
        for service in plan['services'].values():
            self.assertNotIn('ports', service)
            for mount in service['volumes']:
                self.assertEqual(mount['type'], 'volume')
                self.assertTrue(mount['volume']['nocopy'])
        self.assertTrue(all(n['internal'] for n in plan['networks'].values()))
        self.assertEqual(self.original, before)
        self.assertLess(restore.TRANSFER.index('test -z'), restore.TRANSFER.index('tar --create'))
        for flag in ('--numeric-owner', '--acls', '--xattrs'):
            self.assertEqual(restore.TRANSFER.count(flag), 2)
        fixture = plan['services'][smoke.FIXTURE]
        self.assertEqual(fixture['mem_limit'], '64m')
        self.assertTrue(all(m['source'].endswith('-source') for m in fixture['volumes']))
        self.assertIn('chown 999:999', fixture['command'][-1])

    def test_nonempty_destination_refuses_before_any_archive_command(self):
        source, destination = self.directory / 'source', self.directory / 'destination'
        source.mkdir()
        destination.mkdir()
        (destination / '.owned').write_text('unchanged')
        script = restore.TRANSFER.replace('/source', shlex.quote(str(source)))
        script = script.replace('/destination', shlex.quote(str(destination)))
        # Any attempt to invoke tar returns a distinctive failure, never archives.
        script = 'tar() { exit 99; }; export -f tar\n' + script
        result = subprocess.run(['/bin/bash', '-euo', 'pipefail', '-c', script],
                                env={'PATH': '/usr/bin:/bin'}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((destination / '.owned').read_text(), 'unchanged')

    def test_refuses_changed_mount_unhealthy_contract_and_host_escape(self):
        service = self.original['services']['postiz-postgres']
        for change in ({'volumes': []}, {'healthcheck': {'disable': True}},
                       {'network_mode': 'host'}, {'privileged': True}):
            with mock.patch.dict(service, change), self.assertRaises(smoke.SmokeError):
                self.plan()
        with mock.patch.dict(service['volumes'][0], {'target': '/elsewhere'}), self.assertRaises(smoke.SmokeError):
            self.plan()

    def test_preview_never_executes_and_arguments_errors_are_quiet(self):
        output = io.StringIO()
        with mock.patch.object(smoke, 'render_model', return_value=self.original), \
                mock.patch.object(restore, 'execute') as execute, \
                mock.patch.object(smoke, 'command') as command, redirect_stdout(output):
            self.assertEqual(restore.main([]), 0)
        execute.assert_not_called()
        command.assert_not_called()
        self.assertIn('preview: databases=3 fresh_volumes=6', output.getvalue())
        for args in (['--database', MARKER], ['--wait-timeout', '0'], ['--wait-timeout', '601']):
            output = io.StringIO()
            with redirect_stderr(output), self.assertRaises(SystemExit):
                restore.main(args)
            self.assertNotIn(MARKER, output.getvalue())

    def execution(self, *, stopped='exited 0', query='PASS\n', failure=None, preflight=False):
        calls, output = [], io.StringIO()
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if failure and failure in args:
                raise RuntimeError(MARKER)
            if 'inspect' in args:
                return stopped
            if kwargs.get('input', '').startswith('SELECT'):
                return query
            return ''
        with mock.patch.object(smoke, 'command', side_effect=runner), \
                mock.patch.object(smoke, 'local_docker', return_value=['docker']), \
                mock.patch.object(smoke, 'preflight', side_effect=smoke.SmokeError if preflight else None), \
                mock.patch.object(smoke, 'require_absent'), redirect_stdout(output):
            if preflight:
                with self.assertRaises(smoke.SmokeError):
                    restore.execute(self.plan(), tuple(restore.DATABASES), self.directory, 600)
                result = False
            else:
                result = restore.execute(self.plan(), tuple(restore.DATABASES), self.directory, 600)
        self.assertNotIn(MARKER, output.getvalue())
        return result, calls, output.getvalue()

    def test_cold_transfer_order_queries_credentials_and_exact_cleanup(self):
        result, calls, output = self.execution()
        self.assertTrue(result)
        commands = [args for args, _ in calls]
        previous_end = -1
        for key in restore.DATABASES:
            source = next(i for i, a in enumerate(commands) if 'up' in a and a[-1] == key + '-source')
            stop = next(i for i, a in enumerate(commands) if 'stop' in a and a[-1] == key + '-source')
            transfer = next(i for i, a in enumerate(commands) if 'run' in a and a[-1] == key + '-transfer')
            restored = next(i for i, a in enumerate(commands) if 'up' in a and a[-1] == key + '-restored')
            self.assertLess(source, stop)
            self.assertLess(previous_end, source)
            self.assertIn('inspect', commands[stop + 1])
            self.assertLess(stop + 1, transfer)
            self.assertLess(transfer, restored)
            previous_end = next(i for i, a in enumerate(commands) if 'stop' in a and a[-1] == key + '-restored')
            self.assertIn(f'{key}: PASS', output)
        for args, kwargs in calls:
            self.assertNotIn(MARKER, ' '.join(args))
            self.assertFalse({'prune', '--remove-orphans', '--timeout', 'logs', 'cp'} & set(args))
            if 'up' in args or 'run' in args:
                self.assertIn('--pull=never', args)
            if 'up' in args:
                self.assertIn('--wait', args)
            if kwargs.get('input') == MARKER:
                self.assertIn('umask 077; set -C; cat > ' + restore.CLIENT, args)
        self.assertEqual(sum(k.get('input') == MARKER for _, k in calls), 2)
        self.assertTrue(any('--host=/var/run/postgresql' in ' '.join(a) for a in commands))
        self.assertEqual(commands[-1][-2:], ['down', '--volumes'])
        self.assertIn(PROJECT, commands[-1])
        self.assertEqual(stat.S_IMODE((self.directory / 'restore.json').stat().st_mode), 0o600)

    def test_nonclean_stop_prevents_transfer_but_still_cleans(self):
        result, calls, output = self.execution(stopped='exited 137')
        self.assertFalse(result)
        self.assertIn('restore stage failed: postgres-source: clean stop', output)
        self.assertFalse(any(a[-1].endswith('-transfer') for a, _ in calls))
        self.assertEqual(calls[-1][0][-2:], ['down', '--volumes'])

    def test_mismatched_query_never_passes_or_leaks_output(self):
        result, calls, output = self.execution(query=MARKER)
        self.assertFalse(result)
        self.assertNotIn('postgres: PASS', output)
        self.assertFalse(any(a[-1].endswith('-transfer') for a, _ in calls))

    def test_transfer_failure_does_not_initialize_destination(self):
        result, calls, _ = self.execution(failure='postgres-transfer')
        self.assertFalse(result)
        self.assertFalse(any('up' in a and a[-1] == 'postgres-restored' for a, _ in calls))

    def test_preflight_failure_has_no_cleanup_authority(self):
        _, calls, _ = self.execution(preflight=True)
        self.assertFalse(any('down' in a or 'up' in a or 'run' in a for a, _ in calls))

    def test_cleanup_failure_fails_drill_and_reports_only_safe_status(self):
        result, _, output = self.execution(failure='down')
        self.assertFalse(result)
        self.assertIn(f'project={PROJECT}: cleanup FAIL', output)


@unittest.skipUnless(shutil.which('tofu') and shutil.which('docker'), 'tofu and Compose client required')
class RealTemplateTests(unittest.TestCase):
    def test_real_database_defaults_survive_isolation_without_docker_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            images = smoke.load_images(smoke.DEFAULT_IMAGES)
            original = smoke.render_model(images, directory, PROJECT)
            env_dir = directory / 'env'
            smoke.bootstrap.helper.write_env(smoke.bootstrap.generate('restore@example.invalid', 'restore'), env_dir)
            plan = restore.build_model(original, images, PROJECT, env_dir, tuple(restore.DATABASES))
            for key, (_, name, _) in restore.DATABASES.items():
                for role in ('source', 'restored'):
                    changed = {'platform', 'ports', 'volumes', 'env_file'}
                    self.assertEqual({k: v for k, v in original['services'][name].items() if k not in changed},
                                     {k: v for k, v in plan['services'][key + '-' + role].items() if k not in changed})
            path = directory / 'restore.json'
            smoke.write_private(path, json.dumps(smoke.compose_literal(plan)))
            smoke.command([*smoke.compose_base(directory, PROJECT), '--file', str(path), '--profile', '*',
                           'config', '--no-env-resolution', '--quiet'], cwd=directory)


if __name__ == '__main__':
    unittest.main()