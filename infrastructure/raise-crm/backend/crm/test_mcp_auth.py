"""Local credential and host provisioning regressions; no credentials in output.

Run: python manage.py test crm.test_mcp_auth --settings=engagement.test_settings
SQLite covers rollback/failure paths; PostgreSQL locking needs integration tests.
"""

from contextlib import contextmanager
from io import StringIO
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase
from rest_framework.authtoken.models import Token

from accounts.models import Profile, ProfileGrant, User
from .access import Actor, authorize
from .mcp_auth import resolve_user
from .models import AuditRecord, Charity


POSIX_FILES = os.name == 'posix' and hasattr(os, 'O_NOFOLLOW')
ISSUE_MODULE = 'crm.management.commands.issue_mcp_token'


@skipUnless(POSIX_FILES, 'Owner-only token files require POSIX O_NOFOLLOW.')
class ResolveUserTests(TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.directory / 'credential'
        self.user = User.objects.create_user(email='caller@example.test')
        self.token = Token.objects.create(user=self.user)
        self.path.write_bytes(self.token.key.encode('ascii'))
        self.path.chmod(0o600)

    def assert_denied(self, path=None):
        # Catch first, then assert booleans outside the exception context so a
        # failing regression cannot print an underlying credential-bearing error.
        with self.assertRaises(Exception) as caught:
            resolve_user(self.path if path is None else path)
        self.assertTrue(isinstance(caught.exception, PermissionDenied))
        self.assertTrue(str(caught.exception) == 'MCP authentication failed.')
        self.assertTrue(caught.exception.__cause__ is None)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_owner_only_modes_and_string_or_path(self):
        for mode in (0o600, 0o400):
            self.path.chmod(mode)
            for path in (self.path, str(self.path)):
                with self.subTest(mode=mode):
                    self.assertEqual(resolve_user(path), self.user.pk)

    def test_revocation_and_reissuance_do_not_reuse_cached_identity(self):
        self.assertEqual(resolve_user(self.path), self.user.pk)
        self.token.delete()
        self.assert_denied()
        replacement = Token.objects.create(user=self.user)
        self.assert_denied()
        self.path.write_bytes(replacement.key.encode('ascii'))
        self.assertEqual(resolve_user(self.path), self.user.pk)

    def test_user_deactivation_is_rechecked(self):
        self.assertEqual(resolve_user(self.path), self.user.pk)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assert_denied()

    def test_reads_changed_file_on_every_invocation(self):
        other = User.objects.create_user(email='other@example.test')
        other_token = Token.objects.create(user=other)
        self.assertEqual(resolve_user(self.path), self.user.pk)
        self.path.write_bytes(other_token.key.encode('ascii'))
        self.assertEqual(resolve_user(self.path), other.pk)

    def test_profile_authorization_remains_separate_and_live(self):
        User.objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        charity = Charity.objects.create(name='Test charity', default_currency='GBP')
        profile = Profile.objects.create(charity=charity, display_name='Test profile')

        def operation():
            return authorize(Actor(resolve_user(self.path), profile.pk, source='mcp'), admin=True)

        with self.assertRaises(PermissionDenied):
            operation()
        grant = ProfileGrant.objects.create(user=self.user, profile=profile, role='admin')
        self.assertEqual(operation().pk, grant.pk)
        ProfileGrant.objects.filter(pk=grant.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            operation()
        ProfileGrant.objects.filter(pk=grant.pk).update(is_active=True, role='viewer')
        with self.assertRaises(PermissionDenied):
            operation()

    def test_insecure_modes_are_rejected(self):
        for mode in (0o000, 0o200, 0o604, 0o640, 0o644, 0o660, 0o700, 0o4600):
            with self.subTest(mode=mode):
                self.path.chmod(mode)
                self.assert_denied()

    def test_wrong_effective_owner_is_rejected(self):
        with patch('crm.mcp_auth.os.geteuid', return_value=os.geteuid() + 1):
            self.assert_denied()

    def test_symlink_and_open_time_symlink_swap_are_rejected(self):
        link = self.directory / 'link'
        link.symlink_to(self.path)
        self.assert_denied(link)
        real_open = os.open

        def swap_before_open(path, flags):
            displaced = self.directory / 'displaced'
            self.path.rename(displaced)
            self.path.symlink_to(displaced)
            return real_open(path, flags)

        with patch('crm.mcp_auth.os.open', side_effect=swap_before_open):
            self.assert_denied()

    def test_missing_directory_and_fifo_are_safe(self):
        self.assert_denied(self.directory / 'missing')
        self.assert_denied(self.directory)
        fifo = self.directory / 'fifo'
        os.mkfifo(fifo, 0o600)
        with patch('crm.mcp_auth.os.open', wraps=os.open) as opened:
            self.assert_denied(fifo)
        self.assertTrue(opened.call_args.args[1] & os.O_NONBLOCK)
        self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)

    def test_malformed_and_oversized_files_never_query_tokens(self):
        credential = self.token.key.encode('ascii')
        malformed = (
            b'', b'a' * 39, b'a' * 41, b'A' * 40, b'g' * 40, b'\xff' * 40,
            b' ' * 40, b'\x00' * 40, credential + b'\n', b' ' + credential,
            credential + b' ' * 217, credential + b' ' * 1024,
        )
        for index, content in enumerate(malformed):
            # assertNumQueries would print SQL (and credentials) on failure.
            with self.subTest(case=index), patch('crm.mcp_auth.Token.objects.using') as lookup:
                self.path.write_bytes(content)
                self.assert_denied()
                self.assertEqual(lookup.call_count, 0)

    def test_growth_after_fstat_still_has_bounded_read(self):
        metadata = self.path.stat()
        self.path.write_bytes(self.token.key.encode('ascii') + b' ' * 1024)
        with (
            patch('crm.mcp_auth.os.fstat', return_value=metadata),
            patch('crm.mcp_auth.os.read', wraps=os.read) as read,
        ):
            self.assert_denied()
        self.assertEqual(read.call_count, 1)
        self.assertLessEqual(read.call_args.args[1], 256)

    def test_descriptor_is_closed_on_success_and_filesystem_failures(self):
        with patch('crm.mcp_auth.os.close', wraps=os.close) as closed:
            self.assertEqual(resolve_user(self.path), self.user.pk)
        self.assertEqual(closed.call_count, 1)
        with self.assertRaises(OSError):
            os.fstat(closed.call_args.args[0])
        for operation in ('fstat', 'read'):
            with (
                self.subTest(operation=operation),
                patch(f'crm.mcp_auth.os.{operation}', side_effect=OSError(str(self.path))),
                patch('crm.mcp_auth.os.close', wraps=os.close) as closed,
            ):
                self.assert_denied()
            self.assertEqual(closed.call_count, 1)

    def test_database_errors_do_not_expose_credentials(self):
        with (
            patch('crm.mcp_auth.Token.objects.using', side_effect=DatabaseError(self.token.key)),
            patch('crm.mcp_auth.os.close', wraps=os.close) as closed,
        ):
            self.assert_denied()
        self.assertEqual(closed.call_count, 1)


class HostCommandTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='recipient@example.test')
        self.stdout = StringIO()
        self.stderr = StringIO()

    def command(self, name, **options):
        self.stdout.seek(0)
        self.stdout.truncate()
        self.stderr.seek(0)
        self.stderr.truncate()
        call_command(name, stdout=self.stdout, stderr=self.stderr, **options)

    def assert_command_denied(self, name, **options):
        with self.assertRaises(Exception) as caught:
            self.command(name, **options)
        self.assertTrue(isinstance(caught.exception, CommandError))
        self.assertTrue(str(caught.exception) == 'Host administrative operation failed.')
        self.assertTrue(caught.exception.__cause__ is None)
        self.assertTrue(caught.exception.__suppress_context__)
        # Boolean comparisons intentionally avoid printing captured secrets on failure.
        self.assertTrue(self.stdout.getvalue() == '')
        self.assertTrue(self.stderr.getvalue() == '')

    def assert_host_audit(self, record, grant, action, entity):
        self.assertEqual(record.action, action)
        self.assertEqual(record.charity_id, grant.profile.charity_id)
        self.assertEqual(record.entity_type, entity._meta.label_lower)
        self.assertEqual(record.entity_id, entity.pk)
        self.assertIsNone(record.created_by_id)
        self.assertIsNone(record.acting_profile_id)
        self.assertEqual(record.changes, {'profile': str(grant.profile_id), 'grant': str(grant.pk)})
        record.full_clean()


@skipUnless(POSIX_FILES, 'Owner-only token files require POSIX O_NOFOLLOW.')
class IssueMCPTokenTests(HostCommandTestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.path = self.directory / 'credential'
        self.options = {'user_email': self.user.email, 'token_file': str(self.path)}

    def test_new_token_is_private_and_never_printed(self):
        with patch(f'{ISSUE_MODULE}.os.open', wraps=os.open) as opened:
            self.command('issue_mcp_token', **self.options)
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_CREAT and flags & os.O_EXCL and flags & os.O_WRONLY)
        self.assertEqual(opened.call_args.args[2], 0o600)
        metadata = self.path.stat()
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertTrue(stat.S_ISREG(metadata.st_mode))
        self.assertEqual(metadata.st_size, 40)
        self.assertEqual(resolve_user(self.path), self.user.pk)
        self.assertTrue(self.path.read_bytes() == Token.objects.get(user=self.user).key.encode('ascii'))
        self.assertTrue(self.stdout.getvalue() == 'MCP token provisioned.\n')
        self.assertTrue(self.stderr.getvalue() == '')
        self.assertFalse(ProfileGrant.objects.exists())

    def test_repeated_issuance_never_overwrites_or_rotates(self):
        self.command('issue_mcp_token', **self.options)
        token = Token.objects.get(user=self.user)
        self.assert_command_denied('issue_mcp_token', **self.options)
        self.assertTrue(self.path.read_bytes() == token.key.encode('ascii'))
        second = self.directory / 'second'
        self.command('issue_mcp_token', **{**self.options, 'token_file': str(second)})
        self.assertTrue(second.read_bytes() == token.key.encode('ascii'))
        self.assertEqual(Token.objects.filter(user=self.user).count(), 1)

    def test_creation_failures_do_not_issue_a_token(self):
        existing = self.directory / 'existing'
        existing.write_bytes(b'keep this file')
        link = self.directory / 'link'
        link.symlink_to(existing)
        dangling = self.directory / 'dangling'
        dangling.symlink_to(self.directory / 'absent')
        for index, path in enumerate((existing, link, dangling, self.directory, self.directory / 'missing' / 'file')):
            with self.subTest(case=index):
                self.assert_command_denied('issue_mcp_token', **{**self.options, 'token_file': str(path)})
                self.assertFalse(Token.objects.exists())
        self.assertTrue(existing.read_bytes() == b'keep this file')
        self.assertTrue(link.is_symlink() and dangling.is_symlink())
        with patch(f'{ISSUE_MODULE}.os.open', side_effect=PermissionError(str(self.path))):
            self.assert_command_denied('issue_mcp_token', **self.options)
        self.assertFalse(Token.objects.exists())

    def test_missing_or_inactive_user_cannot_create_file_or_user(self):
        for email in ('missing@example.test', self.user.email):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            self.assert_command_denied('issue_mcp_token', **{**self.options, 'user_email': email})
            self.assertFalse(self.path.exists())
            self.assertFalse(Token.objects.exists())
            self.assertEqual(User.objects.count(), 1)

    def test_exclusive_creation_race_preserves_competing_file(self):
        real_open = os.open

        def competing_create(path, flags, mode):
            self.path.write_bytes(b'competing file')
            return real_open(path, flags, mode)

        with patch(f'{ISSUE_MODULE}.os.open', side_effect=competing_create):
            self.assert_command_denied('issue_mcp_token', **self.options)
        self.assertTrue(self.path.read_bytes() == b'competing file')
        self.assertFalse(Token.objects.exists())

    def test_partial_write_failure_rolls_back_but_preserves_existing_tokens(self):
        real_write = os.write

        def partial_failure(descriptor, content):
            real_write(descriptor, content[:5])
            raise OSError('Write failed')

        for existing in (False, True):
            with self.subTest(existing=existing):
                token = Token.objects.create(user=self.user) if existing else None
                path = self.directory / f'partial-{existing}'
                with (
                    patch(f'{ISSUE_MODULE}.os.write', side_effect=partial_failure),
                    patch(f'{ISSUE_MODULE}.os.close', wraps=os.close) as closed,
                ):
                    self.assert_command_denied('issue_mcp_token', **{**self.options, 'token_file': str(path)})
                self.assertEqual(closed.call_count, 1)
                self.assertEqual(path.stat().st_size, 0)
                self.assertEqual(Token.objects.count(), int(existing))
                if existing:
                    self.assertTrue(Token.objects.get(user=self.user).key == token.key)

    def test_short_writes_are_completed(self):
        real_write = os.write
        with patch(f'{ISSUE_MODULE}.os.write', side_effect=lambda fd, data: real_write(fd, data[:7])):
            self.command('issue_mcp_token', **self.options)
        self.assertEqual(resolve_user(self.path), self.user.pk)

    def test_zero_write_and_sync_failure_roll_back(self):
        failures = (
            ('write', {'return_value': 0}), ('fsync', {'side_effect': OSError('Sync failed')}),
        )
        for operation, kwargs in failures:
            path = self.directory / operation
            with patch(f'{ISSUE_MODULE}.os.{operation}', **kwargs):
                self.assert_command_denied('issue_mcp_token', **{**self.options, 'token_file': str(path)})
            self.assertEqual(path.stat().st_size, 0)
            self.assertFalse(Token.objects.exists())

    def test_token_database_failure_and_cleanup_failure_are_generic_and_close_descriptor(self):
        for cleanup_fails in (False, True):
            path = self.directory / f'database-{cleanup_fails}'
            real_truncate = os.ftruncate
            kwargs = {'side_effect': OSError('Cleanup failed')} if cleanup_fails else {'wraps': real_truncate}
            with (
                self.subTest(cleanup_fails=cleanup_fails),
                patch.object(Token, 'save', side_effect=IntegrityError('Token conflict')),
                patch(f'{ISSUE_MODULE}.os.ftruncate', **kwargs),
                patch(f'{ISSUE_MODULE}.os.close', wraps=os.close) as closed,
            ):
                self.assert_command_denied('issue_mcp_token', **{**self.options, 'token_file': str(path)})
            self.assertEqual(closed.call_count, 1)
            self.assertFalse(Token.objects.exists())

    def test_commit_failure_clears_only_reserved_file(self):
        @contextmanager
        def failing_atomic(**kwargs):
            with transaction.atomic(**kwargs):
                yield
                raise DatabaseError('Commit failed')

        with patch(f'{ISSUE_MODULE}.transaction', SimpleNamespace(atomic=failing_atomic)):
            self.assert_command_denied('issue_mcp_token', **self.options)
        self.assertEqual(self.path.stat().st_size, 0)
        self.assertFalse(Token.objects.exists())

    def test_replacement_race_does_not_unlink_or_truncate_replacement(self):
        displaced = self.directory / 'displaced'

        def replace_file(descriptor):
            self.path.rename(displaced)
            self.path.write_bytes(b'competing file')

        with patch(f'{ISSUE_MODULE}.os.fsync', side_effect=replace_file):
            self.assert_command_denied('issue_mcp_token', **self.options)
        self.assertTrue(self.path.read_bytes() == b'competing file')
        self.assertEqual(displaced.stat().st_size, 0)
        self.assertFalse(Token.objects.exists())


class BootstrapRaiseTests(HostCommandTestCase):
    def setUp(self):
        super().setUp()
        self.options = {
            'user_email': self.user.email, 'charity_name': 'Test charity',
            'profile_name': 'Fundraising team',
        }

    def assert_empty_bootstrap(self):
        for model in (Charity, Profile, ProfileGrant, AuditRecord, Token):
            self.assertFalse(model.objects.exists())
        self.assertEqual(User.objects.count(), 1)

    def test_bootstrap_creates_explicit_admin_grant_not_a_user_or_password(self):
        original_password = self.user.password
        self.command('bootstrap_raise', **self.options)
        charity = Charity.objects.get()
        profile = Profile.objects.get()
        grant = ProfileGrant.objects.get()
        self.assertEqual((charity.name, charity.default_currency, charity.timezone), ('Test charity', 'GBP', 'UTC'))
        self.assertEqual(profile.display_name, 'Fundraising team')
        self.assertEqual(profile.charity_id, charity.pk)
        self.assertEqual((grant.user_id, grant.profile_id, grant.role), (self.user.pk, profile.pk, 'admin'))
        self.assertTrue(charity.is_active and profile.is_active and grant.is_active)
        self.assert_host_audit(AuditRecord.objects.get(), grant, 'bootstrap_profile', profile)
        self.assertEqual(authorize(Actor(self.user.pk, profile.pk), admin=True).pk, grant.pk)
        self.user.refresh_from_db()
        self.assertTrue(self.user.password == original_password)
        self.assertFalse(self.user.is_staff or self.user.is_superuser)
        self.assertEqual(User.objects.count(), 1)
        self.assertFalse(Token.objects.exists())
        self.assertTrue(self.stdout.getvalue() == (
            f'Charity {charity.pk}: {charity.name!r}\n'
            f'Profile {profile.pk}: {profile.display_name!r}\nGrant {grant.pk}\n'
        ))
        ProfileGrant.objects.filter(pk=grant.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            authorize(Actor(self.user.pk, profile.pk), admin=True)

    def test_explicit_currency_timezone_and_trimmed_names(self):
        self.command('bootstrap_raise', **{
            **self.options, 'charity_name': ' Other charity ', 'profile_name': ' Team ',
            'currency': 'EUR', 'timezone': 'Europe/London',
        })
        charity = Charity.objects.get()
        self.assertEqual((charity.name, charity.default_currency, charity.timezone), ('Other charity', 'EUR', 'Europe/London'))
        self.assertEqual(Profile.objects.get().display_name, 'Team')

    def test_missing_and_inactive_users_cannot_bootstrap(self):
        for email in ('missing@example.test', self.user.email):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            self.assert_command_denied('bootstrap_raise', **{**self.options, 'user_email': email})
            self.assert_empty_bootstrap()

    def test_duplicate_charity_name_fails_even_if_inactive(self):
        charity = Charity.objects.create(name='Test charity', default_currency='GBP', is_active=False)
        with self.assertRaisesMessage(CommandError, 'Bootstrap unavailable; use grant_profile for existing profiles.'):
            self.command('bootstrap_raise', **{**self.options, 'charity_name': ' TEST CHARITY '})
        self.assertEqual(Charity.objects.get().pk, charity.pk)
        for model in (Profile, ProfileGrant, AuditRecord):
            self.assertFalse(model.objects.exists())
        self.assertTrue(self.stdout.getvalue() == '')

    def test_invalid_fields_roll_back_the_whole_bootstrap(self):
        invalid = (
            {'currency': 'gbp'}, {'currency': 'GBPP'}, {'timezone': 'No/Such_Zone'},
            {'charity_name': ' '}, {'profile_name': ' '}, {'profile_name': 'x' * 256},
        )
        for index, options in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_command_denied('bootstrap_raise', **{**self.options, **options})
                self.assert_empty_bootstrap()

    def test_grant_conflicts_and_audit_failures_roll_back_everything(self):
        for model, error in (
            (ProfileGrant, IntegrityError('Constraint conflict')),
            (ProfileGrant, ValidationError('Constraint conflict')),
            (AuditRecord, IntegrityError('Audit failed')),
        ):
            with self.subTest(model=model.__name__), patch.object(model, 'save', side_effect=error):
                self.assert_command_denied('bootstrap_raise', **self.options)
            self.assert_empty_bootstrap()


class GrantProfileTests(HostCommandTestCase):
    def setUp(self):
        super().setUp()
        self.charity = Charity.objects.create(name='Test charity', default_currency='GBP')
        self.profile = Profile.objects.create(charity=self.charity, display_name='Test profile')
        self.options = {
            'host_admin': True, 'user_email': self.user.email,
            'profile_id': str(self.profile.pk), 'role': 'viewer',
        }

    def test_explicit_host_admin_acknowledgement_is_required(self):
        with self.assertRaisesMessage(CommandError, 'Host administrative confirmation is required (--host-admin).'):
            self.command('grant_profile', **{**self.options, 'host_admin': False})
        self.assertFalse(ProfileGrant.objects.exists())
        self.assertFalse(AuditRecord.objects.exists())

    def test_roles_create_update_and_reactivate_the_same_grant(self):
        original_grant_id = None
        for role in ProfileGrant.Role.values:
            self.command('grant_profile', **{**self.options, 'role': role})
            grant = ProfileGrant.objects.get()
            original_grant_id = original_grant_id or grant.pk
            self.assertEqual(grant.pk, original_grant_id)
            self.assertEqual(grant.role, role)
            self.assertTrue(grant.is_active)
            self.assertEqual((grant.user_id, grant.profile_id), (self.user.pk, self.profile.pk))
            record = AuditRecord.objects.latest('created_at')
            self.assert_host_audit(record, grant, 'host_grant_role', grant)
            ProfileGrant.objects.filter(pk=grant.pk).update(is_active=False)
        self.assertEqual(AuditRecord.objects.count(), 3)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(self.user.email not in self.stdout.getvalue())

    def test_grants_are_scoped_and_superusers_have_no_implicit_grant(self):
        User.objects.filter(pk=self.user.pk).update(is_staff=True, is_superuser=True)
        with self.assertRaises(PermissionDenied):
            authorize(Actor(self.user.pk, self.profile.pk))
        self.command('grant_profile', **self.options)
        self.assertEqual(authorize(Actor(self.user.pk, self.profile.pk)).role, 'viewer')
        with self.assertRaises(PermissionDenied):
            authorize(Actor(self.user.pk, self.profile.pk), write=True)
        other = Profile.objects.create(charity=self.charity, display_name='Other profile')
        with self.assertRaises(PermissionDenied):
            authorize(Actor(self.user.pk, other.pk))

    def test_missing_or_inactive_user_profile_and_charity_are_rejected(self):
        for options in (
            {'user_email': 'missing@example.test'}, {'profile_id': str(uuid4())},
            {'profile_id': 'not-a-uuid'},
        ):
            self.assert_command_denied('grant_profile', **{**self.options, **options})
        for record in (self.user, self.profile, self.charity):
            type(record).objects.filter(pk=record.pk).update(is_active=False)
            self.assert_command_denied('grant_profile', **self.options)
            type(record).objects.filter(pk=record.pk).update(is_active=True)
        self.assertFalse(ProfileGrant.objects.exists())
        self.assertFalse(AuditRecord.objects.exists())
        self.assertEqual(User.objects.count(), 1)

    def test_role_choices_are_restricted(self):
        with self.assertRaises(CommandError):
            self.command('grant_profile', **{**self.options, 'role': 'owner'})
        self.assertFalse(ProfileGrant.objects.exists())

    def test_unique_grant_conflict_is_generic_and_atomic(self):
        for error in (IntegrityError('Constraint conflict'), ValidationError('Constraint conflict')):
            with patch.object(ProfileGrant, 'save', side_effect=error):
                self.assert_command_denied('grant_profile', **self.options)
            self.assertFalse(ProfileGrant.objects.exists())
            self.assertFalse(AuditRecord.objects.exists())

    def test_audit_failure_rolls_back_grant_creation_and_updates(self):
        with patch.object(AuditRecord, 'save', side_effect=IntegrityError('Audit failed')):
            self.assert_command_denied('grant_profile', **self.options)
        self.assertFalse(ProfileGrant.objects.exists())
        grant = ProfileGrant.objects.create(user=self.user, profile=self.profile, role='viewer', is_active=False)
        with patch.object(AuditRecord, 'save', side_effect=IntegrityError('Audit failed')):
            self.assert_command_denied('grant_profile', **{**self.options, 'role': 'admin'})
        grant.refresh_from_db()
        self.assertEqual(grant.role, 'viewer')
        self.assertFalse(grant.is_active)
        self.assertFalse(AuditRecord.objects.exists())