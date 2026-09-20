"""Render REAL Terraform templates and validate the merged Compose model.

Configuration only: synthetic pins, no pulls, cloud calls, daemons or app writes.
Caddy syntax check uses an already-local image if available, with network=none.
"""
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
APPS = ('agentic-social', 'mautic', 'actual-budget', 'invoice-ninja', 'observability')


def render(public=False):
    tool = shutil.which('tofu') or shutil.which('terraform')
    if not tool:
        raise unittest.SkipTest('Terraform/OpenTofu required for template validation')
    templates = [ROOT / 'templates/compose.yaml.tftpl', ROOT / 'templates/Caddyfile.tftpl']
    templates += [ROOT / app / 'compose.yaml.tftpl' for app in APPS]
    keys = set(re.findall(r'images\.([a-z_]+)', '\n'.join(p.read_text() for p in templates)))
    values = {
        'images': {name: 'example.invalid/' + name + '@sha256:' + 'a' * 64 for name in keys},
        'domains': {key: key + '.makemoredigital.com' for key in ('social', 'marketing', 'budget', 'invoices', 'metrics')},
        'public_enabled': public,
    }
    # Parse JSON as HCL through jsondecode, avoiding ad-hoc template substitutions.
    args = 'jsondecode(' + json.dumps(json.dumps(values)) + ')'
    expressions = [f'templatefile({json.dumps(str(path))}, {args})' for path in templates]
    with tempfile.TemporaryDirectory() as temporary:
        process = subprocess.run([tool, '-chdir=' + temporary, 'console'],
                                 input='jsonencode([' + ','.join(expressions) + '])\n',
                                 text=True, capture_output=True, timeout=30)
    if process.returncode:
        raise AssertionError('Template rendering failed: ' + process.stderr)
    return json.loads(json.loads(process.stdout))


class StackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = render()
        if not shutil.which('docker'):
            raise unittest.SkipTest('Docker Compose required')
        # No environment file resolution, no server needed, no real credentials.
        with tempfile.TemporaryDirectory() as temporary:
            command = ['docker', 'compose', '--project-name', 'business-tools-config-test']
            for index, contents in enumerate([cls.rendered[0], *cls.rendered[2:]]):
                path = Path(temporary) / f'{index}.yaml'
                path.write_text(contents)
                command += ['--file', str(path)]
            command += ['--profile', 'mautic-workers', 'config', '--no-env-resolution', '--format', 'json']
            process = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if process.returncode:
                raise AssertionError('Compose validation failed: ' + process.stderr)
            cls.model = json.loads(process.stdout)

    def test_only_proxy_publishes_ports(self):
        services = self.model['services']
        self.assertEqual(len(services), 18)
        for name, service in services.items():
            self.assertNotIn('network_mode', service)
            self.assertNotIn('restart', service)
            # Compose emits byte counts as decimal strings in some releases.
            self.assertGreater(int(service['mem_limit']), 0)
            self.assertEqual(service['logging']['driver'], 'local')
            if name != 'caddy':
                self.assertFalse(service.get('ports'), name)
        ports = services['caddy']['ports']
        self.assertEqual({p['published'] for p in ports}, {'80', '443', '8443'})
        self.assertEqual(next(p['host_ip'] for p in ports if p['published'] == '8443'), '127.0.0.1')

    def test_defaults_are_inherited_except_explicit_security_controls(self):
        services = self.model['services']
        self.assertNotIn('environment', services['actual'])
        self.assertNotIn('MAUTIC_MESSENGER_DSN_EMAIL', services['mautic']['environment'])
        self.assertEqual(services['mautic-worker']['profiles'], ['mautic-workers'])
        self.assertEqual(services['invoice-app']['environment']['QUEUE_CONNECTION'], 'redis')
        self.assertEqual(services['grafana']['environment']['GF_AUTH_ANONYMOUS_ENABLED'], 'false')

    def test_every_persistent_mount_is_prepared(self):
        spec = importlib.util.spec_from_file_location('bt_disk_contract', ROOT / 'runtime/prepare-disk.py')
        disk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(disk)
        directories = {'/srv/business-tools/' + item[0] for item in disk.DIRECTORIES}
        for service in self.model['services'].values():
            for volume in service.get('volumes', []):
                self.assertEqual(volume['type'], 'bind')
                self.assertFalse(volume.get('bind', {}).get('create_host_path', False))
                source = volume['source']
                self.assertNotIn('docker.sock', source)
                if source.startswith('/srv/business-tools/'):
                    self.assertIn(source, directories)
                elif source.startswith('/opt/business-tools/'):
                    relative = source.removeprefix('/opt/business-tools/')
                    self.assertTrue(relative == 'Caddyfile' or (ROOT / relative).is_file())
                else:
                    self.assertIn(source, ('/proc', '/sys'))

    def test_private_databases_and_metrics(self):
        names = ('postiz-postgres', 'postiz-redis', 'postiz-temporal-postgres',
                 'mautic-mariadb', 'invoice-db', 'invoice-redis', 'prometheus', 'node-exporter')
        for name in names:
            for network in self.model['services'][name]['networks']:
                self.assertTrue(self.model['networks'][network]['internal'], (name, network))

    def test_public_gate_is_closed_and_private_route_exists(self):
        caddy = self.rendered[1]
        self.assertEqual(caddy.count('respond "Private initialization in progress" 503'), 5)
        self.assertEqual(caddy.count('reverse_proxy'), 6)  # IAP plus narrow Invoice renderer.
        self.assertEqual(caddy.count('@renderer remote_ip 172.30.252.3'), 1)
        self.assertNotIn('client_ip', '\n'.join(line for line in caddy.splitlines() if not line.strip().startswith('#')))
        opened = render(public=True)[1]  # Named deliberate post-setup scenario.
        self.assertNotIn('initialization in progress', opened)
        self.assertEqual(opened.count('reverse_proxy'), 10)

    def test_proxy_networks_are_two_peer_internal_bridges(self):
        services = self.model['services']
        for network, app, subnet in (('mautic-proxy', 'mautic', '172.30.251.'),
                                     ('invoice-render', 'invoice-app', '172.30.252.')):
            self.assertTrue(self.model['networks'][network]['internal'])
            self.assertEqual(self.model['networks'][network]['ipam']['config'][0]['subnet'], subnet + '0/29')
            self.assertEqual({name for name, service in services.items() if network in service['networks']}, {'caddy', app})
            self.assertEqual(services['caddy']['networks'][network]['ipv4_address'], subnet + '2')
            self.assertEqual(services[app]['networks'][network]['ipv4_address'], subnet + '3')
        self.assertEqual(services['caddy']['networks']['invoice-render']['aliases'], ['invoices.makemoredigital.com'])
        self.assertIn('mautic-web', services['mautic']['networks']['mautic-proxy']['aliases'])
        self.assertIn('reverse_proxy mautic-web:80', self.rendered[1])
        self.assertNotIn('reverse_proxy mautic:80', self.rendered[1])
        spec = importlib.util.spec_from_file_location('bt_proxy_seed', ROOT / 'runtime/prepare-mautic.py')
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        self.assertIn(services['caddy']['networks']['mautic-proxy']['ipv4_address'].encode(), seed.CONTENT)

    def test_mautic_first_install_is_locked_and_every_start_checks_only(self):
        bootstrap = (ROOT / 'runtime/bootstrap.sh').read_text()
        seed_call = 'python3 /opt/business-tools/prepare-mautic.py'
        self.assertEqual(bootstrap.count(seed_call), 1)
        self.assertLess(bootstrap.index('flock --exclusive --nonblock 8'), bootstrap.index(seed_call))
        self.assertLess(bootstrap.index('python3 /opt/business-tools/prepare-disk.py\n'), bootstrap.index(seed_call))
        self.assertLess(bootstrap.index(seed_call), bootstrap.index('flock --unlock 8'))
        unit = (ROOT / 'runtime/business-tools.service').read_text()
        self.assertIn('ExecStartPre=/usr/bin/flock --shared --nonblock --no-fork /run/business-tools-data.lock /usr/bin/python3 /opt/business-tools/prepare-mautic.py --check', unit)

    def test_pipeboard_example_has_only_token_free_remote_endpoints(self):
        config = json.loads((ROOT.parent / 'pipeboard/mcp.example.json').read_text())
        self.assertEqual(config, {'mcpServers': {
            'pipeboard-google-ads': {'type': 'http', 'url': 'https://google-ads.mcp.pipeboard.co/'},
            'pipeboard-meta-ads': {'type': 'http', 'url': 'https://meta-ads.mcp.pipeboard.co/'},
        }})
        self.assertNotIn('pipeboard', self.model['services'])

    def test_caddy_adapter_with_already_local_image(self):
        # Named parser-compatibility test, not a smoke test of production pins.
        # Never pull a mutable tag: resolve an already-local image to its image ID.
        probe = subprocess.run(['docker', 'image', 'inspect', 'caddy:2.10.2', '--format', '{{.Id}}'],
                               text=True, capture_output=True, timeout=10)
        if probe.returncode:
            self.skipTest('No already-local Caddy 2.10.2 parser image')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'Caddyfile'
            path.write_text(self.rendered[1])
            process = subprocess.run(['docker', 'run', '--rm', '--pull=never', '--network=none',
                                      '--mount', f'type=bind,source={path},target=/config-test,readonly',
                                      probe.stdout.strip(), 'caddy', 'adapt', '--config', '/config-test',
                                      '--adapter', 'caddyfile'], text=True, capture_output=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            adapted = json.loads(process.stdout)
            servers = adapted['apps']['http']['servers']
            self.assertEqual({port for server in servers.values() for port in server['listen']}, {':443', ':8443'})
            # Assert the adapter retained transport-peer ACL, not a spoofable header.
            def objects(value):
                if isinstance(value, dict):
                    yield value
                    for child in value.values():
                        yield from objects(child)
                elif isinstance(value, list):
                    for child in value:
                        yield from objects(child)
            peers = [obj['remote_ip'] for obj in objects(adapted) if 'remote_ip' in obj]
            self.assertEqual(peers, [{'ranges': ['172.30.252.3']}])


if __name__ == '__main__':
    unittest.main()