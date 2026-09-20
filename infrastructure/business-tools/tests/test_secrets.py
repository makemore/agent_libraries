"""Synthetic credentials only. Never queries metadata, cloud APIs or host state."""
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = load('bt_secrets', 'runtime/fetch-secrets.py')
generator = load('bt_generator', 'scripts/create-bootstrap.py')


def fixture():
    # No optional app settings or credentials for real accounts.
    data = {section: {key: 'synthetic-not-a-credential-' + key * 4 for key in keys}
            for section, keys in helper.FIELDS.items()}
    data['invoice_ninja']['admin_email'] = 'operator@example.invalid'
    data['invoice_ninja']['app_key'] = 'base64:' + base64.b64encode(bytes(range(32))).decode()
    return data


class SecretTests(unittest.TestCase):
    def test_default_mapping_and_least_privilege(self):
        data = fixture()
        files = helper.environment_files(data)
        self.assertEqual(len(files), 9)
        self.assertEqual(set(files['postiz.env']), {'DATABASE_URL', 'JWT_SECRET'})
        self.assertEqual(set(files['mautic.env']), {'MAUTIC_DB_PASSWORD'})
        self.assertEqual(set(files['invoice-db.env']), {'MARIADB_PASSWORD', 'MARIADB_ROOT_PASSWORD'})
        self.assertEqual(files['temporal.env']['POSTGRES_PWD'], files['temporal-db.env']['POSTGRES_PASSWORD'])
        self.assertEqual(files['invoice-ninja.env']['DB_PASSWORD'], files['invoice-db.env']['MARIADB_PASSWORD'])
        self.assertNotIn('GF_SECURITY_ADMIN_PASSWORD', files['postiz.env'])

    def test_special_password_is_url_encoded_not_interpolated(self):
        data = fixture()
        data['postiz']['db_password'] += ':@/$#"\\+%='
        env = helper.environment_files(data)
        url = urllib.parse.urlsplit(env['postiz.env']['DATABASE_URL'])
        self.assertEqual(url.hostname, 'postiz-postgres')
        self.assertEqual(urllib.parse.unquote(url.password), data['postiz']['db_password'])
        with tempfile.TemporaryDirectory() as temporary:
            helper.write_env(data, Path(temporary))
            self.assertEqual((Path(temporary) / 'postiz-db.env').read_text(),
                             'POSTGRES_PASSWORD=' + data['postiz']['db_password'] + '\n')

    def test_optional_integration_cannot_override_auth_or_database(self):
        data = fixture()
        data['postiz']['extra_env'] = {'FACEBOOK_APP_ID': 'synthetic-id'}
        self.assertEqual(helper.environment_files(data)['postiz.env']['FACEBOOK_APP_ID'], 'synthetic-id')
        for key in ('DATABASE_URL', 'DISABLE_REGISTRATION', 'NODE_OPTIONS', 'MAIN_URL'):
            data['postiz']['extra_env'] = {key: 'invalid'}
            with self.assertRaises(ValueError):
                helper.environment_files(data)

    def test_reject_missing_unknown_duplicate_fields(self):
        for section in helper.FIELDS:
            data = fixture()
            del data[section]
            with self.assertRaises(ValueError):
                helper.validate_credentials(data)
        data = fixture()
        data['grafana']['unknown'] = 'value'
        with self.assertRaises(ValueError):
            helper.validate_credentials(data)
        with self.assertRaises(ValueError):
            helper.parse_json('{"a": 1, "a": 2}')

    def test_reject_injection_invalid_key_email_and_short_password(self):
        for value in ('short', 'a' * 40 + '\nOTHER=unsafe', 'a' * 40 + '\r', 'a' * 40 + '\0', 123):
            data = fixture()
            data['mautic']['db_password'] = value
            with self.assertRaises(ValueError):
                helper.validate_credentials(data)
        for key, value in (('app_key', 'base64:abc'), ('admin_email', 'not-an-email')):
            data = fixture()
            data['invoice_ninja'][key] = value
            with self.assertRaises(ValueError):
                helper.validate_credentials(data)

    def test_invalid_payload_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper.write_env(fixture(), root)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            data = fixture()
            data['grafana']['secret_key'] = 'bad'
            with self.assertRaises(ValueError):
                helper.write_env(data, root)
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in root.iterdir()))

    def test_temporal_password_rejects_yaml_quoting_hazards(self):
        for suffix in ('"', "'", '\\', ':', '#', ' '):
            with self.subTest(suffix=suffix):
                data = fixture()
                data['postiz']['temporal_password'] = 'a' * 64 + suffix
                with self.assertRaises(ValueError):
                    helper.validate_credentials(data)

    def test_symlink_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            target.write_text('unchanged')
            (root / 'postiz.env').symlink_to(target)
            with self.assertRaises(ValueError):
                helper.write_env(fixture(), root)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_fetch_never_redirects_and_rejects_oversize(self):
        self.assertIsNone(helper.NoRedirect().redirect_request(None, None, None, None, None, None))
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'X' * (helper.LIMIT + 1)
        with self.assertRaises(ValueError):
            helper.read_json(opener, 'https://example.invalid/', {})

    def test_errors_never_print_remote_diagnostics(self):
        output = io.StringIO()
        with mock.patch.object(helper.os, 'geteuid', return_value=0), \
                mock.patch.object(helper, 'load_deployment', side_effect=ValueError('sensitive-remote-body')), \
                contextlib.redirect_stderr(output):
            self.assertEqual(helper.main(), 1)
        self.assertNotIn('sensitive-remote-body', output.getvalue())

    def test_generator_exclusive_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bootstrap.json'
            data = generator.generate('operator@example.invalid', 'operator')
            generator.write_new(path, data)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                generator.write_new(path, fixture())
            self.assertEqual(json.loads(path.read_text()), data)


if __name__ == '__main__':
    unittest.main()