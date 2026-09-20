"""A local, authenticated stdio server; no public HTTP listener."""

from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand, CommandError

from crm.mcp_auth import resolve_user
from crm.mcp_server import build_server


class Command(BaseCommand):
    help = 'Serve Raise CRM over MCP stdio using a protected user token file.'
    # Migration notices would corrupt the MCP stdout stream.
    requires_migrations_checks = False

    def add_arguments(self, parser):
        parser.add_argument('--token-file', required=True, help='Owner-only file containing the user token.')

    def handle(self, *args, **options):
        try:
            resolve_user(options['token_file'])
        except PermissionDenied:
            raise CommandError('A valid, owner-only token file for an active user is required.') from None
        build_server(options['token_file']).run(transport='stdio')