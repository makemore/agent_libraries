"""Export the user's existing/new DRF token to a new private local file.

Run only on a trusted administrative host, with a trusted destination directory.
There is one DRF token per user: export does not rotate it, and revocation affects
every client using it. Revoke through an authorized Django admin shell by deleting
that user's Token row; this command never deletes existing tokens.

On failure after exclusive creation, cleanup truncates only the reserved file's
descriptor. It never unlinks a potentially competing replacement at the same
path. An empty reserved file normally remains; if filesystem cleanup also fails,
treat it as potentially containing a live credential. Inspect the file before
removing it manually or choose a new destination.
"""

import os
import stat

from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from crm.management import active_user, host_errors
from crm.mcp_auth import TOKEN_PATTERN


class Command(BaseCommand):
    help = (
        'Host administrators only: write an existing active user\'s DRF token to '
        'a NEW owner-only file in a trusted directory. Never prints the token or '
        'overwrites a file. Revoke the user\'s Token row in an authorized admin '
        'shell, not with this command. Failed writes can leave a reserved file; '
        'treat it as sensitive, especially if filesystem cleanup also failed.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--user-email', required=True)
        parser.add_argument('--token-file', required=True)

    def handle(self, *args, **options):
        with host_errors():
            if os.name != 'posix':
                raise ValueError
            descriptor = None
            committed = False
            try:
                with transaction.atomic(using='default'):
                    user = active_user(options['user_email'])
                    # Reserve the file BEFORE get_or_create: an existing path,
                    # symlink or creation failure must not issue a credential.
                    descriptor = os.open(
                        options['token_file'], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600,
                    )
                    os.fchmod(descriptor, 0o600)
                    metadata = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_uid != os.geteuid()
                    ):
                        raise ValueError
                    token, _ = Token.objects.using('default').get_or_create(user=user)
                    credential = token.key.encode('ascii')
                    if TOKEN_PATTERN.fullmatch(credential) is None:
                        raise ValueError
                    remaining = memoryview(credential)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError
                        remaining = remaining[written:]
                    os.fsync(descriptor)
                    # A replacement is not ours to delete, and must not cause a
                    # newly issued database token to survive a failed export.
                    current = os.stat(options['token_file'], follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise OSError
                committed = True
            finally:
                if descriptor is not None:
                    try:
                        if not committed:
                            os.ftruncate(descriptor, 0)
                    finally:
                        os.close(descriptor)
            self.stdout.write('MCP token provisioned.')