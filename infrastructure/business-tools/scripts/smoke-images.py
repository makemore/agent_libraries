#!/usr/bin/env python3
"""Opt-in, selected-image LOCAL startup smoke; preview is the default.

Requires OpenTofu and Compose >=2.30. Preview only renders/validates configuration,
without contacting the Docker daemon. --run requires exactly one --group; invoke
groups sequentially in the operator's chosen order. Images must already be local
linux/amd64 pins (including Caddy, used ONLY as a network-none /bin/sh helper).

Named exception: isolated-startup. Keep production settings, image entrypoints,
healthchecks, resource limits and identities. Replace /srv binds with fresh named
volumes (Linux ownership works on Docker Desktop), /run envs with private generated
credentials, and /opt assets with their checked-in read-only sources. Remove ports
and Caddy; make EVERY application network internal to block SMTP/provider/ACME
egress. Preserve reserved subnets/addresses; Docker overlap errors fail, never
renumber. Check host/VPN route collisions separately before opting in.

Image counts in preview include the Caddy shell helper; service counts exclude it.
This proves startup only, NOT TLS, accounts/authentication, delivery, PDF rendering,
real database restore or the production merged/systemd lifecycle. Per-app execution
is a local test exception, not a deployment mode. Tests/test_smoke_images.py verifies
the exception and preservation of all other defaults. No persistent demo overrides
are made. Cleanup removes only this invocation's exact project and fresh volumes;
failure is reported without logs, environments or exception details. Invoice's
unchanged graceful shutdown can take over an hour, independently of the 600s wait.
"""

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGES = ROOT / 'releases/2026-09-20.tfvars.json'
GROUPS = {
    'actual': ('actual',),
    'observability': ('grafana', 'prometheus', 'node-exporter'),
    'mautic': ('mautic', 'mautic-cron', 'mautic-mariadb'),
    'postiz': ('postiz', 'postiz-postgres', 'postiz-redis', 'postiz-temporal',
               'postiz-temporal-postgres'),
    'invoice-ninja': ('invoice-ninja', 'invoice-app', 'invoice-db', 'invoice-redis'),
}
SERVICE_IMAGES = {
    **{name: name for name in ('actual', 'grafana', 'prometheus', 'mautic', 'postiz', 'caddy')},
    'node-exporter': 'node_exporter', 'mautic-cron': 'mautic', 'mautic-worker': 'mautic',
    'mautic-mariadb': 'mariadb', 'postiz-postgres': 'postgres', 'postiz-redis': 'redis',
    'postiz-temporal': 'temporal', 'postiz-temporal-postgres': 'temporal_postgres',
    'invoice-ninja': 'invoice_nginx', 'invoice-app': 'invoice_ninja',
    'invoice-db': 'mariadb', 'invoice-redis': 'redis',
}
# Repository and tag-family guards are not a registry provenance verification.
IMAGE_RULES = {
    'actual': ('actualbudget/actual-server', r'26\.9\.[0-9]+'),
    'grafana': ('grafana/grafana', r'12\.4\.[0-9]+'),
    'prometheus': ('prom/prometheus', r'v3\.[0-9]+\.[0-9]+'),
    'node_exporter': ('prom/node-exporter', r'v1\.[0-9]+\.[0-9]+'),
    'mautic': ('mautic/mautic', r'6(?:\.[0-9]+){0,2}(?:-[0-9]{8})?-apache'),
    'mariadb': ('mariadb', r'11\.4(?:\.[0-9]+)?'),
    'postiz': ('ghcr.io/gitroomhq/postiz-app', r'v2\.(?:1[2-9]|[2-9][0-9]|[1-9][0-9]{2,})\.[0-9]+'),
    'postgres': ('postgres', r'17(?:\.[0-9]+)?-bookworm'),
    'temporal_postgres': ('postgres', r'16(?:\.[0-9]+)?-bookworm'),
    'redis': ('redis', r'7\.2(?:\.[0-9]+)?-bookworm'),
    'temporal': ('temporalio/auto-setup', r'1\.28\.1'),
    'invoice_ninja': ('invoiceninja/invoiceninja-debian', r'5\.13\.[0-9]+'),
    'invoice_nginx': ('nginx', r'1\.30(?:\.[0-9]+)?-alpine'),
    'caddy': ('caddy', r'2\.[0-9]+\.[0-9]+'),
}
DOMAINS = {key: key + '.makemoredigital.com'
           for key in ('social', 'marketing', 'budget', 'invoices', 'metrics')}
ASSETS = {
    'invoice-ninja/nginx.conf', 'observability/prometheus.yml',
    'observability/provisioning/datasources/prometheus.yaml',
}
# Fail closed on new Compose mechanisms that could bypass mount/network isolation.
SERVICE_FIELDS = set('image environment env_file expose networks volumes depends_on '
                     'healthcheck stop_grace_period mem_limit pids_limit logging user '
                     'read_only cap_drop cap_add security_opt command entrypoint tmpfs '
                     'ports platform'.split())
PROJECT_PATTERN = r'bt-smoke-[0-9a-f]{32}'
FIXTURE = 'smoke-fixture'


class SmokeError(Exception):
    """Intentionally contains no command, configuration or diagnostic data."""


def require(condition):
    if not condition:
        raise SmokeError()


def load_helper(relative):
    spec = importlib.util.spec_from_file_location('smoke_' + Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


disk = load_helper('runtime/prepare-disk.py')
bootstrap = load_helper('scripts/create-bootstrap.py')
mautic_seed = load_helper('runtime/prepare-mautic.py')
DIRECTORIES = {str(disk.DATA_MOUNT / path): (uid, gid, mode)
               for path, uid, gid, mode in disk.DIRECTORIES}


def validate_images(images):
    require(isinstance(images, dict) and images.keys() == IMAGE_RULES.keys())
    for key, (repository, tag) in IMAGE_RULES.items():
        reference = images[key]
        require(isinstance(reference, str))
        match = re.fullmatch(re.escape(repository) + r'(?::(' + tag + r'))?@sha256:([a-f0-9]{64})',
                             reference)
        require(match is not None and len(set(match[2])) > 1)
    return images


def load_images(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size < 65536)
    data = json.loads(path.read_text())
    require(isinstance(data, dict) and set(data) == {'images'})
    return validate_images(data['images'])


def command(args, *, cwd, input=None, timeout=60):
    """Capture everything; never propagate subprocess diagnostics or host overrides."""
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR') if key in os.environ}
    result = subprocess.run(args, cwd=cwd, env=environment, input=input,
                            text=True, capture_output=True, timeout=timeout)
    require(result.returncode == 0)
    return result.stdout


def write_private(path, content):
    with path.open('x', encoding='utf-8') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)


def compose_base(directory, project, docker=('docker',)):
    require(re.fullmatch(PROJECT_PATTERN, project) is not None)
    return [*docker, 'compose', '--project-name', project, '--project-directory', str(directory),
            '--env-file', str(directory / 'empty.env')]


def render_model(images, directory, project):
    """Render ALL real fragments, then merge without resolving any runtime envs."""
    validate_images(images)
    templates = [ROOT / 'templates/compose.yaml.tftpl']
    templates += [ROOT / app / 'compose.yaml.tftpl' for app in disk.APPS]
    values = {'images': images, 'domains': DOMAINS, 'public_enabled': False}
    args = 'jsondecode(' + json.dumps(json.dumps(values)) + ')'
    expressions = [f'templatefile({json.dumps(str(path))}, {args})' for path in templates]
    # No root .tf files, backend, tfvars, provider initialization or state is read.
    with tempfile.TemporaryDirectory(prefix='bt-smoke-tofu-') as empty:
        output = command(['tofu', 'console', '-no-color'], cwd=empty,
                         input='jsonencode([' + ','.join(expressions) + '])\n', timeout=30)
    rendered = json.loads(json.loads(output))
    require(len(rendered) == len(templates))
    write_private(directory / 'empty.env', '')
    invocation = compose_base(directory, project)
    for index, contents in enumerate(rendered):
        path = directory / f'source-{index}.yaml'
        write_private(path, contents)
        invocation += ['--file', str(path)]
    # This profile is used for CONFIG ONLY so the complete source is validated.
    # The worker is never included in the executable smoke project.
    output = command([*invocation, '--profile', 'mautic-workers', 'config',
                      '--no-env-resolution', '--format', 'json'], cwd=directory)
    return json.loads(output)


def checked_file(root, relative):
    """Only exact relative paths and real files, never symlink/traversal escapes."""
    path = Path(relative)
    require(not path.is_absolute() and path.parts and '..' not in path.parts)
    require(str(path) == relative)
    candidate = root / path
    for component in (*path.parents, path):
        require(not (root / component).is_symlink())
    info = candidate.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    require(candidate.resolve().is_relative_to(root.resolve()))
    return str(candidate)


def isolated_model(model, group, images, project, env_dir):
    """Pure model transformation apart from checking allowlisted local files."""
    validate_images(images)
    require(group in GROUPS and re.fullmatch(PROJECT_PATTERN, project) is not None)
    require(set(model['services']) == set(SERVICE_IMAGES))
    require(not model.get('volumes') and not model.get('secrets') and not model.get('configs'))
    services, volumes, data_sources, networks = {}, {}, {}, {}
    for name in GROUPS[group]:
        service = copy.deepcopy(model['services'][name])
        require(set(service) <= SERVICE_FIELDS)
        require(service['image'] == images[SERVICE_IMAGES[name]])
        require(service.get('platform', 'linux/amd64') == 'linux/amd64')
        require(set(service.get('depends_on', {})) <= set(GROUPS[group]))
        require(service.get('networks'))
        service['platform'] = 'linux/amd64'
        service.pop('ports', None)
        for mount in service.get('volumes', []):
            require(mount['type'] == 'bind' and not mount.get('bind', {}).get('create_host_path'))
            require(set(mount) <= {'type', 'source', 'target', 'read_only', 'bind'})
            require(set(mount.get('bind', {})) <= {'create_host_path'})
            source = mount['source']
            if source in DIRECTORIES:
                folder = 'actual-budget' if group == 'actual' else group
                require(source.startswith(str(disk.DATA_MOUNT / folder) + '/'))
                key = 'data-' + source.removeprefix(str(disk.DATA_MOUNT) + '/').replace('/', '-')
                require(key not in data_sources or data_sources[key] == source)
                data_sources[key] = source
                volumes[key] = {'name': project + '-' + key}
                mount.update(type='volume', source=key, volume={'nocopy': True})
                mount.pop('bind', None)
            elif source.startswith(str(disk.SOURCE_ROOT) + '/'):
                relative = source.removeprefix(str(disk.SOURCE_ROOT) + '/')
                require(relative in ASSETS and mount.get('read_only') is True)
                mount['source'] = checked_file(ROOT, relative)
            else:
                # Kernel views are the only retained host binds, not host data.
                require(name == 'node-exporter' and source in ('/proc', '/sys'))
                require(mount['target'] == '/host' + source and mount.get('read_only') is True)
        for env_file in service.get('env_file', []):
            require(set(env_file) <= {'path', 'format', 'required'})
            require(env_file.get('format') == 'raw' and env_file.get('required', True))
            filename = env_file['path'].removeprefix(str(disk.RUNTIME_ROOT) + '/')
            require(filename in disk.ENV_FILES)
            require(env_file['path'] == str(disk.RUNTIME_ROOT / filename))
            env_file['path'] = checked_file(env_dir, filename)
        for network in service['networks']:
            settings = copy.deepcopy(model['networks'][network])
            require(set(settings) <= {'name', 'driver', 'internal', 'ipam', 'external'})
            require(not settings.get('external') and settings.get('driver', 'bridge') == 'bridge')
            ipam = settings.get('ipam', {})
            require(set(ipam) <= {'driver', 'config'} and ipam.get('driver', 'default') == 'default')
            settings.pop('name', None)  # Compose's global name must not escape the project.
            settings['internal'] = True
            networks[network] = settings
        services[name] = service
    return {'name': project, 'services': services, 'networks': networks, 'volumes': volumes}, data_sources


def add_fixture(model, data_sources, image):
    """Fresh-volume ONLY setup; no application paths or host binds in the helper."""
    mounts, checks, initialization = [], [], []
    seed_input = ''
    for index, (key, source) in enumerate(sorted(data_sources.items())):
        target = f'/fixture/{index}'
        mounts.append({'type': 'volume', 'source': key, 'target': target,
                       'volume': {'nocopy': True}})
        checks += [f'test -d {target}', f'test ! -L {target}',
                   f'contents=$(ls -A {target})', 'test -z "$contents"']
        uid, gid, mode = DIRECTORIES[source]
        initialization += [f'chown {uid}:{gid} {target}', f'chmod {mode:o} {target}']
        if source == str(mautic_seed.CONFIG):
            require(mautic_seed.FILENAME == 'parameters_local.php')
            path = target + '/' + mautic_seed.FILENAME
            initialization += [f'cat > {path}', f'chown {uid}:{gid} {path}', f'chmod 600 {path}']
            seed_input = mautic_seed.CONTENT.decode('utf-8')
    require(mounts)
    model['services'][FIXTURE] = {
        'image': image, 'platform': 'linux/amd64', 'profiles': [FIXTURE],
        'network_mode': 'none', 'entrypoint': ['/bin/sh'],
        'command': ['-eu', '-c', '\n'.join(['set -C', 'umask 077', *checks, *initialization])],
        'stdin_open': True, 'user': '0:0', 'read_only': True,
        'cap_drop': ['ALL'], 'cap_add': ['CHOWN', 'DAC_OVERRIDE', 'FOWNER'],
        'security_opt': ['no-new-privileges:true'], 'mem_limit': '64m', 'pids_limit': 64,
        'logging': {'driver': 'none'}, 'volumes': mounts,
    }
    return seed_input


def compose_literal(value):
    """A merged model is already interpolated; preserve its literal dollar signs."""
    if isinstance(value, str):
        return value.replace('$', '$$')
    if isinstance(value, list):
        return [compose_literal(item) for item in value]
    if isinstance(value, dict):
        return {key: compose_literal(item) for key, item in value.items()}
    return value


def local_docker(directory):
    context = command(['docker', 'context', 'show'], cwd=directory).strip()
    require(re.fullmatch(r'[a-zA-Z0-9_.-]+', context) is not None)
    endpoint = command(['docker', 'context', 'inspect', context, '--format',
                        '{{.Endpoints.docker.Host}}'], cwd=directory).strip()
    require(endpoint.startswith('unix:///'))  # Refuse remote engines, even an active context.
    return ['docker', '--context', context]


def require_absent(project, docker, directory):
    """Check exact project labels AND its reserved name prefix, without adoption."""
    require(re.fullmatch(PROJECT_PATTERN, project) is not None)
    for kind, listing, field in (('container', 'ls', 'Names'), ('network', 'ls', 'Name'),
                                  ('volume', 'ls', 'Name')):
        args = [*docker, kind, listing]
        if kind == 'container':
            args += ['--all']
        named = command([*args, '--format', '{{.' + field + '}}'], cwd=directory).splitlines()
        require(not any(name == project or name.startswith((project + '-', project + '_'))
                        for name in named))
        labelled = command([*args, '--filter', 'label=com.docker.compose.project=' + project,
                            '--format', '{{.' + field + '}}'], cwd=directory)
        require(not labelled.strip())


def preflight(model, docker, directory):
    """Only narrow metadata queries; reject all pre-existing project objects."""
    require_absent(model['name'], docker, directory)
    for image in sorted({service['image'] for service in model['services'].values()}):
        # Containerd can retain multiple platforms: inspect the same one Compose
        # will run, not the host-native image or an unqualified manifest index.
        platform = command([*docker, 'image', 'inspect', '--platform=linux/amd64',
                            '--format', '{{.Os}}/{{.Architecture}}',
                            image], cwd=directory).strip()
        require(platform == 'linux/amd64')


def report_states(compose, model, directory):
    # Request ONLY these fields. No raw inspect, health errors, commands or logs.
    rows = command([*compose, 'ps', '--all', '--format',
                    '[{{json .Name}},{{json .State}},{{json .Health}},{{json .ExitCode}}]'],
                   cwd=directory)
    allowed = {model['name'] + '-' + name + '-1' for name in model['services'] if name != FIXTURE}
    records, seen, ready = [], set(), True
    for line in rows.splitlines():
        name, state, health, exit_code = json.loads(line)
        require(name in allowed and name not in seen)
        require(state in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'})
        require(health in {'', 'none', 'starting', 'healthy', 'unhealthy'})
        require(type(exit_code) is int and -1 <= exit_code <= 255)
        seen.add(name)
        ready = ready and state == 'running' and health in {'', 'none', 'healthy'} and exit_code == 0
        records.append(f'{name} state={state} health={health or "none"} exit={exit_code}')
    for record in sorted(records):
        print(record)
    return ready and seen == allowed


def execute_group(model, data_sources, images, directory, wait_timeout):
    require(type(wait_timeout) is int and 1 <= wait_timeout <= 600)
    model = copy.deepcopy(model)
    seed_input = add_fixture(model, data_sources, images['caddy'])
    path = directory / 'smoke.json'
    write_private(path, json.dumps(compose_literal(model)))
    docker = local_docker(directory)
    compose = [*compose_base(directory, model['name'], docker), '--file', str(path)]
    command([*compose, 'config', '--no-env-resolution', '--quiet'], cwd=directory)
    preflight(model, docker, directory)  # No cleanup authority until absence is established.
    passed, cleaned = False, False
    try:
        command([*compose, 'run', '--rm', '--no-deps', '--pull=never', '-T', FIXTURE],
                cwd=directory, input=seed_input, timeout=60)
        command([*compose, 'up', '-d', '--pull=never', '--wait', '--wait-timeout', str(wait_timeout),
                 *[name for name in model['services'] if name != FIXTURE]],
                cwd=directory, timeout=wait_timeout + 60)
        passed = True
    except (Exception, KeyboardInterrupt):
        pass
    finally:
        try:
            passed = bool(report_states(compose, model, directory)) and passed
        except (Exception, KeyboardInterrupt):
            passed = False
        try:
            # No --remove-orphans, prune, force removal or shortened app stop timeout.
            command([*compose, 'down', '--volumes'], cwd=directory, timeout=4200)
            require_absent(model['name'], docker, directory)
            cleaned = True
        except (Exception, KeyboardInterrupt):
            pass
    return passed and cleaned, cleaned


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo untrusted arguments/paths.
        self.exit(2, 'smoke: FAIL (invalid arguments; use --help)\n')


def main(argv=None):
    parser = QuietParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--images', type=Path, default=DEFAULT_IMAGES, help='public release images JSON')
    parser.add_argument('--group', choices=GROUPS, help='one group; omitted previews all groups')
    parser.add_argument('--run', action='store_true', help='opt in to isolated local Docker writes')
    parser.add_argument('--wait-timeout', type=int, default=600, help='health wait seconds, 1..600')
    args = parser.parse_args(argv)
    if (args.run and not args.group) or not 1 <= args.wait_timeout <= 600:
        parser.error('invalid selection')
    groups = (args.group,) if args.group else tuple(GROUPS)
    try:
        images = load_images(args.images)
        project = 'bt-smoke-' + secrets.token_hex(16)
        with tempfile.TemporaryDirectory(prefix=project + '-') as temporary:
            directory = Path(temporary).resolve()
            model = render_model(images, directory, project)
            env_dir = directory / 'env'
            bootstrap.helper.write_env(bootstrap.generate('smoke@example.invalid', 'smoke'), env_dir)
            plans = [(group, *isolated_model(model, group, images, project, env_dir)) for group in groups]
            if not args.run:
                print(f'groups={len(plans)}')
                for group, plan, _ in plans:
                    count = len({s['image'] for s in plan['services'].values()} | {images['caddy']})
                    print(f'{group}: services={len(plan["services"])} images={count}')
                return 0
            group, plan, sources = plans[0]
            passed, cleaned = execute_group(plan, sources, images, directory, args.wait_timeout)
            if not cleaned:
                print(f'{group}: cleanup FAIL')
            print(f'{group}: {"PASS" if passed else "FAIL"}')
            return 0 if passed else 1
    except (Exception, KeyboardInterrupt):
        print(f'{args.group or "smoke"}: FAIL')
        return 1


def interrupted(signum, frame):
    raise KeyboardInterrupt()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    sys.exit(main())