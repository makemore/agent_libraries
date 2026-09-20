"""Resolve a local credential afresh for each MCP invocation.

The token file must contain exactly the 40 lowercase ASCII hex bytes of a DRF
token, without whitespace. This authenticates a user, not an acting profile:
callers must also authorize the live profile grant on every operation.
"""

import os
from pathlib import Path
import re
import stat

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from rest_framework.authtoken.models import Token


TOKEN_PATTERN = re.compile(rb'[0-9a-f]{40}')


def resolve_user(token_file: str | Path) -> int:
    """Fail closed without exposing credentials, paths, or underlying errors."""
    try:
        if os.name != 'posix' or not isinstance(token_file, (str, Path)):
            raise ValueError
        # NONBLOCK prevents a FIFO supplied in place of a file from hanging us.
        descriptor = os.open(token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) not in (0o600, 0o400)
                or metadata.st_uid != os.geteuid()
                or metadata.st_size != 40
            ):
                raise ValueError
            credential = os.read(descriptor, 256)
            if TOKEN_PATTERN.fullmatch(credential) is None:
                raise ValueError
        finally:
            os.close(descriptor)

        user_id = Token.objects.using('default').filter(
            key=credential.decode('ascii'), user__is_active=True,
        ).values_list('user_id', flat=True).first()
        if user_id is None:
            raise ValueError
        return user_id
    except (OSError, ValueError, TypeError, AttributeError, DatabaseError):
        raise PermissionDenied('MCP authentication failed.') from None