"""Create the first charity/profile/grant for an existing active user on the host."""

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from accounts.models import Profile, ProfileGrant
from crm.management import active_user, host_audit, host_errors
from crm.models import Charity


class Command(BaseCommand):
    help = (
        'Host administrators only: create a charity, profile and explicit admin '
        'grant for an existing active user. No users or passwords are created. '
        'For existing charities/profiles use grant_profile. Audits have no '
        'authenticated application actor; the recipient is not the operator.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--user-email', required=True)
        parser.add_argument('--charity-name', required=True)
        parser.add_argument('--profile-name', required=True)
        parser.add_argument('--currency', default='GBP')
        parser.add_argument('--timezone', default='UTC')

    def handle(self, *args, **options):
        with host_errors():
            with transaction.atomic(using='default'):
                # Charity.name has no unique constraint. Serialize PostgreSQL
                # bootstrap checks/inserts, including when the table is empty.
                connection = connections['default']
                if connection.vendor == 'postgresql':
                    table = connection.ops.quote_name(Charity._meta.db_table)
                    with connection.cursor() as cursor:
                        cursor.execute(f'LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE')
                user = active_user(options['user_email'])
                name = options['charity_name'].strip()
                if Charity.objects.using('default').filter(name__iexact=name).exists():
                    raise CommandError('Bootstrap unavailable; use grant_profile for existing profiles.')
                charity = Charity.objects.using('default').create(
                    name=name, default_currency=options['currency'], timezone=options['timezone'],
                )
                profile = Profile.objects.using('default').create(
                    charity=charity, display_name=options['profile_name'].strip(),
                )
                grant = ProfileGrant.objects.using('default').create(
                    user=user, profile=profile, role=ProfileGrant.Role.ADMIN, is_active=True,
                )
                host_audit(grant, 'bootstrap_profile', profile)
            self.stdout.write(f'Charity {charity.pk}: {charity.name!r}')
            self.stdout.write(f'Profile {profile.pk}: {profile.display_name!r}')
            self.stdout.write(f'Grant {grant.pk}')