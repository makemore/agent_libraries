"""Real SDK round trips, including a separately launched stdio process."""

import os
import sys
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import async_to_sync, sync_to_async
from django.core.management import call_command
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from accounts.models import Profile, ProfileGrant, User
from crm.mcp_server import build_server
from crm.models import AuditRecord, Charity, Contact, Interaction


class MCPTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token_file = Path(self.directory.name) / 'client.token'
        self.user = User.objects.create_user(email='mcp@example.test')
        self.charity = Charity.objects.create(name='MCP charity', default_currency='GBP')
        self.profile = Profile.objects.create(charity=self.charity, display_name='Fundraiser')
        self.grant = ProfileGrant.objects.create(user=self.user, profile=self.profile, role='fundraiser')
        call_command('issue_mcp_token', user_email=self.user.email, token_file=str(self.token_file), stdout=StringIO())
        self.server = build_server(self.token_file)

    def test_discovery_and_contact_interaction_round_trip(self):
        async_to_sync(self.round_trip)()
        self.assertEqual(Contact.objects.count(), 1)
        self.assertEqual(Interaction.objects.count(), 1)
        self.assertEqual(set(AuditRecord.objects.values_list('created_by_id', 'acting_profile_id')),
                         {(self.user.id, self.profile.id)})

    async def round_trip(self):
        async with Client(self.server) as client:
            tools = (await client.list_tools()).tools
            self.assertEqual({tool.name for tool in tools}, {
                'list_profiles', 'list_contacts', 'get_contact', 'create_contact',
                'add_contact_method', 'log_interaction', 'contact_timeline', 'get_interaction',
            })
            for tool in tools:
                self.assertNotIn('user_id', tool.input_schema.get('properties', {}))
                self.assertIsNotNone(tool.output_schema)
            profiles = await client.call_tool('list_profiles')
            self.assertFalse(profiles.is_error)
            self.assertEqual(profiles.structured_content['profiles'][0]['profile_id'], str(self.profile.id))
            profile = {'profile_id': str(self.profile.id)}
            args = {**profile, 'contact': {'display_name': 'Jane Supporter'}, 'idempotency_key': 'create-jane'}
            created = await client.call_tool('create_contact', args)
            self.assertFalse(created.is_error)
            retried = await client.call_tool('create_contact', args)
            self.assertEqual(created.structured_content, retried.structured_content)
            contact_id = created.structured_content['id']
            method = await client.call_tool('add_contact_method', {
                **profile, 'contact_id': contact_id, 'kind': 'email', 'value': 'jane@example.test',
                'is_primary': True, 'idempotency_key': 'email-jane',
            })
            self.assertFalse(method.is_error)
            logged = await client.call_tool('log_interaction', {
                **profile, 'idempotency_key': 'log-jane', 'interaction': {
                    'participants': [{'contact_id': contact_id, 'role': 'recipient'}],
                    'channel': 'email', 'direction': 'outbound', 'occurred_at': timezone.now().isoformat(),
                    'subject': 'Scholarship appeal', 'body': 'Original correspondence', 'summary': 'Sent proposal',
                },
            })
            self.assertFalse(logged.is_error)
            timeline = await client.call_tool('contact_timeline', {**profile, 'contact_id': contact_id})
            item = timeline.structured_content['items'][0]
            self.assertNotIn('body', item)
            self.assertEqual(item['participants'][0]['name'], 'Jane Supporter')
            self.assertEqual(item['participants'][0]['address'], 'jane@example.test')
            detail = await client.call_tool('get_interaction', {**profile, 'interaction_id': logged.structured_content['id']})
            self.assertEqual(detail.structured_content['body'], 'Original correspondence')
            contact = await client.call_tool('get_contact', {**profile, 'contact_id': contact_id})
            self.assertEqual(contact.structured_content['methods'][0]['value'], 'jane@example.test')
            contacts = await client.call_tool('list_contacts', {**profile, 'search': 'Jane'})
            self.assertEqual(len(contacts.structured_content['items']), 1)

    def test_live_revocation_profile_scope_and_readonly_tools(self):
        other_charity = Charity.objects.create(name='Other', default_currency='GBP')
        other_profile = Profile.objects.create(charity=other_charity, display_name='Other')
        other_contact = Contact.objects.create(charity=other_charity, display_name='Not visible')
        async_to_sync(self.revocation)(other_profile.id, other_contact.id)

    async def revocation(self, other_profile_id, other_contact_id):
        async with Client(self.server) as client:
            denied = await client.call_tool('list_contacts', {'profile_id': str(other_profile_id)})
            self.assertTrue(denied.is_error)
            denied = await client.call_tool('get_contact', {
                'profile_id': str(self.profile.id), 'contact_id': str(other_contact_id),
            })
            self.assertTrue(denied.is_error)
            await sync_to_async(ProfileGrant.objects.filter(pk=self.grant.id).update)(role='viewer')
            denied = await client.call_tool('create_contact', {
                'profile_id': str(self.profile.id), 'contact': {'display_name': 'No write'}, 'idempotency_key': 'denied',
            })
            self.assertTrue(denied.is_error)
            read = await client.call_tool('list_contacts', {'profile_id': str(self.profile.id)})
            self.assertFalse(read.is_error)
            await sync_to_async(ProfileGrant.objects.filter(pk=self.grant.id).update)(is_active=False)
            denied = await client.call_tool('list_contacts', {'profile_id': str(self.profile.id)})
            self.assertTrue(denied.is_error)
            await sync_to_async(lambda: self.user.auth_token.delete())()
            denied = await client.call_tool('list_profiles')
            self.assertTrue(denied.is_error)

    def test_tool_validation_and_unexpected_errors_are_safe(self):
        async_to_sync(self.validation)()

    async def validation(self):
        async with Client(self.server) as client:
            for arguments in ({'profile_id': 'invalid'}, {'profile_id': str(self.profile.id), 'limit': 101}):
                self.assertTrue((await client.call_tool('list_contacts', arguments)).is_error)
            result = await client.call_tool('create_contact', {
                'profile_id': str(self.profile.id), 'idempotency_key': 'invalid',
                'contact': {'display_name': 'No injection', 'charity_id': str(uuid4())},
            })
            self.assertTrue(result.is_error)
            with patch('crm.mcp_server.services.list_contacts', side_effect=RuntimeError('private diagnostic')):
                result = await client.call_tool('list_contacts', {'profile_id': str(self.profile.id)})
            self.assertTrue(result.is_error)
            self.assertNotIn('private diagnostic', str(result.content))


class StdioSmokeTests(SimpleTestCase):
    def test_real_subprocess_initialize_discover_and_call(self):
        # Independent disposable databases, not the developer's DB or test runner DB.
        with TemporaryDirectory() as directory:
            path = Path(directory)
            settings_file = path / 'raise_mcp_smoke_settings.py'
            settings_file.write_text(
                'from importlib import import_module\n'
                'from pathlib import Path\n'
                'globals().update({k:v for k,v in vars(import_module("raise.settings.test")).items() if k.isupper()})\n'
                'DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(Path(__file__).with_name("crm.sqlite3"))}}\n'
            )
            token_path = path / 'client.token'
            backend = Path(__file__).resolve().parent.parent
            # These are non-secret settings/module paths. Do not forward host secrets.
            env = {'DJANGO_SETTINGS_MODULE': 'raise_mcp_smoke_settings', 'PYTHONPATH': str(path) + os.pathsep + str(backend)}
            import subprocess
            bootstrap = (
                'import django; django.setup(); '
                'from django.core.management import call_command; '
                'from accounts.models import User; '
                'call_command("migrate", interactive=False, verbosity=0); '
                'User.objects.create_user(email="smoke@example.test"); '
                'call_command("bootstrap_raise", user_email="smoke@example.test", charity_name="Smoke Charity", profile_name="Smoke Profile", currency="GBP", timezone="UTC"); '
                'import sys; call_command("issue_mcp_token", user_email="smoke@example.test", token_file=sys.argv[1])'
            )
            seeded = subprocess.run([sys.executable, '-c', bootstrap, str(token_path)],
                                    cwd=backend, env=env, capture_output=True, text=True, timeout=30)
            # Bootstrap output is metadata only; never include credential contents.
            self.assertEqual(seeded.returncode, 0, 'Isolated MCP fixture setup failed.')
            params = StdioServerParameters(command=sys.executable,
                args=[str(backend / 'manage.py'), 'run_mcp', '--token-file', str(token_path)], cwd=backend, env=env)
            async_to_sync(self.stdio_round_trip)(params)

    async def stdio_round_trip(self, params):
        async with Client(params, read_timeout_seconds=15) as client:
            self.assertEqual(len((await client.list_tools()).tools), 8)
            profiles = await client.call_tool('list_profiles')
            self.assertFalse(profiles.is_error)
            profile_id = profiles.structured_content['profiles'][0]['profile_id']
            result = await client.call_tool('create_contact', {
                'profile_id': profile_id, 'contact': {'display_name': 'Stdio donor'}, 'idempotency_key': 'stdio-contact',
            })
            self.assertFalse(result.is_error)
            contact_id = result.structured_content['id']
            result = await client.call_tool('log_interaction', {
                'profile_id': profile_id, 'idempotency_key': 'stdio-call', 'interaction': {
                    'channel': 'phone', 'direction': 'outbound', 'occurred_at': timezone.now().isoformat(),
                    'outcome': 'connected', 'summary': 'Requested a proposal',
                    'participants': [{'contact_id': contact_id}],
                },
            })
            self.assertFalse(result.is_error)
            timeline = await client.call_tool('contact_timeline', {'profile_id': profile_id, 'contact_id': contact_id})
            self.assertEqual(timeline.structured_content['items'][0]['summary'], 'Requested a proposal')