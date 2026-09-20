"""Explicit event-store configuration; never derive it from the default database.

Call configure_engagement after all default/cloud database overrides, then install
engagement.apps.EngagementConfig and engagement.router.EngagementRouter (first).
App checks revalidate the final settings. Different DNS aliases/proxies for the
same PostgreSQL instance cannot be detected reliably: operators must provision a
separate instance, not merely a separate database name or login.
SQLite is only a test escape hatch in final settings, ENGAGEMENT_ALLOW_SQLITE=True;
this URL helper always requires PostgreSQL and never consults USE_SQLITE.
"""

from copy import deepcopy
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import dj_database_url
from django.core.exceptions import ImproperlyConfigured


POSTGRES_ENGINES = {'django.db.backends.postgresql', 'django.db.backends.postgresql_psycopg2'}
SQLITE_ENGINE = 'django.db.backends.sqlite3'


def _endpoint(database):
    # libpq connection overrides/multiple hosts defeat an endpoint comparison.
    options = database.get('OPTIONS') or {}
    if not isinstance(options, dict) or set(options).intersection({
        'host', 'hostaddr', 'port', 'service', 'servicefile', 'dbname',
    }):
        raise ImproperlyConfigured('Engagement isolation requires explicit database endpoints.')
    host = str(database.get('HOST') or 'localhost').strip().lower().rstrip('.')
    if ',' in host:
        raise ImproperlyConfigured('Engagement isolation requires a single database endpoint.')
    host = host.removeprefix('[').removesuffix(']')
    if host in {'localhost', 'localhost.localdomain', 'ip6-localhost'}:
        host = 'loopback'
    else:
        try:
            address = ip_address(host)
            address = getattr(address, 'ipv4_mapped', None) or address
            host = 'loopback' if address.is_loopback else address.compressed
        except ValueError:
            pass
    try:
        port = int(database.get('PORT') or 5432)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise ImproperlyConfigured('Engagement requires a valid database port.') from None
    return host, port


def validate_engagement_databases(databases, *, allow_sqlite=False):
    """Validate final configuration without connecting or exposing credentials."""
    if not isinstance(databases, dict):
        raise ImproperlyConfigured('A separate engagement database alias is required.')
    event = databases.get('engagement')
    default = databases.get('default')
    if not isinstance(event, dict) or not isinstance(default, dict):
        raise ImproperlyConfigured('A separate engagement database alias is required.')
    for database in (default, event):
        test = database.get('TEST') or {}
        if not isinstance(test, dict) or test.get('MIRROR'):
            raise ImproperlyConfigured('Engagement isolation does not permit test mirrors.')
    if event.get('ENGINE') == SQLITE_ENGINE and allow_sqlite is True:
        if default.get('ENGINE') != SQLITE_ENGINE:
            raise ImproperlyConfigured('Engagement SQLite tests require two SQLite aliases.')
        names = []
        for database in (default, event):
            name = str(database.get('NAME') or '')
            if not name:
                raise ImproperlyConfigured('Engagement SQLite tests require explicit database names.')
            names.append(name if name.startswith('file:') else str(Path(name).resolve()))
        # Django gives :memory: databases separate per-alias test URIs.
        if names[0] == names[1] and event['NAME'] != ':memory:':
            raise ImproperlyConfigured('Engagement SQLite tests require separate databases.')
        test_names = [(database.get('TEST') or {}).get('NAME') for database in (default, event)]
        if all(test_names) and test_names[0] == test_names[1] and test_names[0] != ':memory:':
            raise ImproperlyConfigured('Engagement SQLite test databases must be separate.')
        return
    if event.get('ENGINE') not in POSTGRES_ENGINES:
        raise ImproperlyConfigured('The engagement database must use PostgreSQL.')
    if not event.get('HOST') or not event.get('NAME'):
        raise ImproperlyConfigured('Engagement requires an explicit database host and name.')
    endpoint = _endpoint(event)
    if default.get('ENGINE') in POSTGRES_ENGINES and endpoint == _endpoint(default):
        raise ImproperlyConfigured('Engagement must use a separate PostgreSQL host or port.')


def configure_engagement(databases, url):
    """Return an independent settings dict; add engagement only for an explicit URL."""
    configured = deepcopy(databases)
    if url is None or url == '':
        return configured
    try:
        if not isinstance(url, str) or any(ord(char) < 33 for char in url):
            raise ValueError
        parts = urlsplit(url)
        if parts.scheme not in {'postgres', 'postgresql'} or not parts.hostname or parts.fragment:
            raise ValueError
        event = dj_database_url.parse(url)
        if not event.get('NAME'):
            raise ValueError
    except Exception:
        # Parser exceptions may contain the original credential-bearing URL.
        raise ImproperlyConfigured('Provide a valid PostgreSQL engagement database URL.') from None
    configured['engagement'] = event
    validate_engagement_databases(configured)
    return configured