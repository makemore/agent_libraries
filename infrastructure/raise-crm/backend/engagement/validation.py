"""Bounded, content-free event payloads. Never store arbitrary tracking metadata."""

import math
from datetime import datetime, timedelta, timezone as datetime_timezone
from urllib.parse import urlsplit
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from crm.models import normalize_hostname


EVENT_TYPES = (
    'page_view', 'donation_started', 'donation_completed', 'enquiry_submitted',
    'proposal_downloaded', 'event_registered', 'email_delivered', 'email_opened', 'email_clicked',
)
WEB_EVENT_TYPES = EVENT_TYPES[:6]


def uuid_value(value, field):
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError({field: 'Use a valid UUID.'}) from None


def event_timestamp(value):
    try:
        result = parse_datetime(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or timezone.is_naive(result):
            raise ValueError
        if result > timezone.now() + timedelta(minutes=5):
            raise ValueError
        return result.astimezone(datetime_timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ValidationError({'occurred_at': 'Use an aware timestamp no more than five minutes ahead.'}) from None


def validate_properties(value):
    if not isinstance(value, dict) or not set(value).issubset({'duration_seconds', 'bot'}):
        raise ValidationError('Only duration_seconds and bot are permitted.')
    duration = value.get('duration_seconds')
    if 'duration_seconds' in value and (
        type(duration) not in (int, float) or not 0 <= duration <= 86400 or not math.isfinite(duration)
    ):
        raise ValidationError('duration_seconds must be a finite number from 0 to 86400.')
    if 'bot' in value and type(value['bot']) is not bool:
        raise ValidationError('bot must be a boolean.')


def sanitize_page_url(value, domain):
    """Store only the registered origin, never paths (which may also contain PII).

    Query strings, fragments and paths are discarded, not fingerprinted or logged.
    A future path-level report needs an explicit non-PII route-template allowlist.
    """
    if value == '':
        return ''
    try:
        if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 33 for char in value):
            raise ValueError
        parts = urlsplit(value)
        registered = normalize_hostname(domain)
        if (
            parts.scheme not in {'http', 'https'} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or normalize_hostname(parts.hostname) != registered
            or parts.port not in (None, 80 if parts.scheme == 'http' else 443)
            or '\\' in parts.netloc
        ):
            raise ValueError
        return f'{parts.scheme}://{registered}/'
    except (ValueError, TypeError, UnicodeError, ValidationError):
        raise ValidationError({'page_url': 'Use an HTTP(S) URL on the registered website domain.'}) from None