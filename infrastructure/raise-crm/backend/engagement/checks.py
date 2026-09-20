"""Check final settings, including cloud overrides applied after the helper."""

from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.exceptions import ImproperlyConfigured
from django.db import router

from .database import ALIAS, validate_engagement_databases
from .router import EngagementRouter


@register(Tags.database)
def engagement_database_check(app_configs=None, **kwargs):
    errors = []
    # Contact-only deployments may omit the alias. Event services still require it.
    if ALIAS in settings.DATABASES:
        try:
            validate_engagement_databases(
                settings.DATABASES, allow_sqlite=getattr(settings, 'ENGAGEMENT_ALLOW_SQLITE', False),
            )
        except ImproperlyConfigured:
            errors.append(Error(
                'Engagement requires a separate, explicitly configured PostgreSQL database.',
                hint='Check final database engines/endpoints; SQLite requires explicit test-only opt-in.',
                id='engagement.E001',
            ))
    if not router.routers or not isinstance(router.routers[0], EngagementRouter):
        errors.append(Error(
            'EngagementRouter must be the first database router.', id='engagement.E002',
        ))
    return errors