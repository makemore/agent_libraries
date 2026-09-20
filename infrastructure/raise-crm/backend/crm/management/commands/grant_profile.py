"""Explicit host-admin grant management, not a caller-authenticated API."""

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Profile, ProfileGrant
from crm.management import active_user, host_audit, host_errors


class Command(BaseCommand):
    help = (
        'Host administrators only: create, reactivate or change an explicit '
        'profile grant. Requires --host-admin acknowledgement; this is not '
        'application-user authentication. The user, profile and charity must '
        'already be active. Audits have no authenticated application actor.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--host-admin', action='store_true', help='Acknowledge host-admin authority.')
        parser.add_argument('--user-email', required=True)
        parser.add_argument('--profile-id', required=True)
        parser.add_argument('--role', required=True, choices=ProfileGrant.Role.values)

    def handle(self, *args, **options):
        if not options['host_admin']:
            raise CommandError('Host administrative confirmation is required (--host-admin).')
        with host_errors():
            # call_command keyword options bypass argparse choice validation.
            if options['role'] not in ProfileGrant.Role.values:
                raise ValueError
            with transaction.atomic(using='default'):
                profile = Profile.objects.using('default').select_for_update().select_related('charity').get(
                    pk=UUID(str(options['profile_id'])), is_active=True, charity__is_active=True,
                )
                user = active_user(options['user_email'])
                grant, created = ProfileGrant.objects.using('default').select_for_update().get_or_create(
                    user=user, profile=profile,
                    defaults={'role': options['role'], 'is_active': True},
                )
                if not created:
                    grant.role = options['role']
                    grant.is_active = True
                    grant.save(using='default', update_fields=['role', 'is_active'])
                host_audit(grant, 'host_grant_role', grant)
            self.stdout.write(f'Profile {profile.pk}: {profile.display_name!r}')
            self.stdout.write(f'Grant {grant.pk}: {grant.role}')