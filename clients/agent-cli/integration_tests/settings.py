"""Extend only Studio's in-memory test settings, never a host configuration."""

import sys


def _deny_network(event, args):
    # This suite uses an in-process HTTPConnection/Django bridge, not a server.
    # A process-wide guard also covers accidental provider work during imports.
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("Network is unavailable in isolated REST contract tests")


sys.addaudithook(_deny_network)

from django_agent_studio.tests.settings import *  # noqa: E402,F403

if any(db["ENGINE"] != "django.db.backends.sqlite3" or db["NAME"] != ":memory:"
       for db in DATABASES.values()):  # noqa: F405
    raise RuntimeError("REST contract tests require isolated in-memory SQLite")

ROOT_URLCONF = "integration_tests.urls"
ALLOWED_HOSTS = ["127.0.0.1", "testserver"]
# Test-only diagnostic safety: error pages must not render request credentials.
DEBUG = False
