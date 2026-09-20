"""Authorized engagement operations, with no per-event default-database writes.

The transport must construct Actor from authenticated state. Event dictionaries
require id, visitor_id, event_type and occurred_at; optional fields are session_id,
page_url, properties, schema_version and consent_granted. No CRM references,
identity assertions or attribution supplied by a browser are trusted.

Retries compare normalized, sanitized client payloads, excluding server-derived
contact/provenance/received_at. Binding an identity never relabels old events.
Donation events are telemetry only: they never create gifts or CRM mutations.
No queue, tracker, retention worker or partition maintenance is implemented.
"""

import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from crm.access import Actor, authorize
from crm.models import Contact, ContactEngagementSummary, Website
from crm.mutations import json_value
from .database import ALIAS, require_engagement
from .models import EngagementEvent, VisitorIdentity
from .validation import EVENT_TYPES, WEB_EVENT_TYPES, event_timestamp, sanitize_page_url, uuid_value, validate_properties


EVENT_FIELDS = {
    'id', 'visitor_id', 'session_id', 'event_type', 'occurred_at', 'page_url',
    'properties', 'consent_granted', 'schema_version',
}
EVENT_OUTPUT_FIELDS = (
    'id', 'charity_id', 'website_id', 'website_domain', 'visitor_id', 'contact_id',
    'session_id', 'event_type', 'occurred_at', 'received_at', 'page_url', 'properties',
    'schema_version', 'actor_user_id', 'acting_profile_id',
)
IDENTITY_OUTPUT_FIELDS = (
    'id', 'charity_id', 'website_id', 'visitor_id', 'contact_id', 'verified_at',
    'method', 'actor_user_id', 'acting_profile_id',
)


def _website(grant, website_id):
    website = Website.objects.using('default').filter(
        pk=uuid_value(website_id, 'website_id'), charity_id=grant.profile.charity_id, is_active=True,
    ).first()
    if website is None:
        raise ValidationError('Website is unavailable.')
    return website


def _contact(grant, contact_id):
    contact = Contact.objects.using('default').filter(
        pk=uuid_value(contact_id, 'contact_id'), charity_id=grant.profile.charity_id,
    ).first()
    if contact is None:
        raise ValidationError('Contact is unavailable.')
    return contact


def _event_payload(website, event):
    if not isinstance(event, dict) or not set(event).issubset(EVENT_FIELDS):
        raise ValidationError('Unsupported event fields; direct contact associations are forbidden.')
    if type(event.get('event_type')) is not str or event['event_type'] not in EVENT_TYPES:
        raise ValidationError({'event_type': 'Unsupported event type.'})
    if 'consent_granted' in event and type(event['consent_granted']) is not bool:
        raise ValidationError({'consent_granted': 'Consent must be a boolean.'})
    if website.consent_required and event.get('consent_granted') is not True:
        raise ValidationError({'consent_granted': 'Explicit consent is required.'})
    version = event.get('schema_version', 1)
    if type(version) is not int or version != 1:
        raise ValidationError({'schema_version': 'Only schema version 1 is supported.'})
    properties = event.get('properties', {})
    validate_properties(properties)
    return {
        'id': uuid_value(event.get('id'), 'id'),
        'charity_id': website.charity_id,
        'website_id': website.pk,
        'website_domain': website.domain,
        'visitor_id': uuid_value(event.get('visitor_id'), 'visitor_id'),
        'session_id': uuid_value(event['session_id'], 'session_id') if event.get('session_id') is not None else None,
        'event_type': event['event_type'],
        'occurred_at': event_timestamp(event.get('occurred_at')),
        'page_url': sanitize_page_url(event.get('page_url', ''), website.domain),
        'properties': dict(properties),
        'schema_version': version,
    }


def _fingerprint(payload, consent):
    # Preserve timestamp microseconds; DjangoJSONEncoder truncates them to ms.
    canonical = {**payload, 'occurred_at': payload['occurred_at'].isoformat(), 'consent_granted': consent}
    encoded = json.dumps(json_value(canonical), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _duplicate(payload, fingerprint):
    existing = EngagementEvent.objects.filter(pk=payload['id']).values('charity_id', 'fingerprint').first()
    if existing is None:
        return False
    if existing['charity_id'] != payload['charity_id'] or existing['fingerprint'] != fingerprint:
        raise ValidationError('Event ID conflict.') from None
    return True


def ingest_events(actor: Actor, website_id, events: list[dict]) -> dict:
    """Atomically ingest at most 100 events; return accepted/duplicates counts."""
    grant = authorize(actor, write=True)
    require_engagement()
    website = _website(grant, website_id)
    if not isinstance(events, list) or len(events) > 100:
        raise ValidationError('Supply a list of at most 100 events.')
    prepared = []
    for event in events:
        payload = _event_payload(website, event)
        prepared.append((payload, _fingerprint(payload, event.get('consent_granted'))))
    accepted = duplicates = 0
    with transaction.atomic(using=ALIAS):
        identities = dict(VisitorIdentity.objects.filter(
            charity_id=website.charity_id, website_id=website.pk,
            visitor_id__in={payload['visitor_id'] for payload, _ in prepared},
            method=VisitorIdentity.Method.ADMIN_VERIFIED, verified_at__lte=timezone.now(),
        ).values_list('visitor_id', 'contact_id'))
        # Recheck scalar references against CRM; never trust orphaned/cross-tenant mappings.
        valid_contacts = set(Contact.objects.using('default').filter(
            charity_id=website.charity_id, pk__in=set(identities.values()),
        ).values_list('pk', flat=True))
        for payload, fingerprint in prepared:
            if _duplicate(payload, fingerprint):
                duplicates += 1
                continue
            contact_id = identities.get(payload['visitor_id'])
            record = EngagementEvent(
                **payload, fingerprint=fingerprint,
                contact_id=contact_id if contact_id in valid_contacts else None,
                actor_user_id=grant.user.pk, acting_profile_id=grant.profile.pk,
            )
            try:
                # Savepoint allows concurrent UUID collisions to be checked after rollback.
                with transaction.atomic(using=ALIAS):
                    EngagementEvent.objects.bulk_create([record])
            except IntegrityError:
                if not _duplicate(payload, fingerprint):
                    raise ValidationError('Event could not be accepted.') from None
                duplicates += 1
            else:
                accepted += 1
    return json_value({'accepted': accepted, 'duplicates': duplicates})


def bind_visitor(actor: Actor, website_id, visitor_id, contact_id, method) -> dict:
    """Privileged verified assertion; only method='admin_verified' is supported.

    Repeating the same mapping preserves original verification/provenance. A
    different contact is a conflict, not an implicit merge or historical backfill.
    """
    grant = authorize(actor, admin=True)
    require_engagement()
    website = _website(grant, website_id)
    contact = _contact(grant, contact_id)
    visitor_id = uuid_value(visitor_id, 'visitor_id')
    if method != VisitorIdentity.Method.ADMIN_VERIFIED:
        raise ValidationError('Unsupported identity verification method.')
    key = {'charity_id': website.charity_id, 'website_id': website.pk, 'visitor_id': visitor_id}
    with transaction.atomic(using=ALIAS):
        identity = VisitorIdentity.objects.filter(**key).first()
        if identity is None:
            identity = VisitorIdentity(
                **key, contact_id=contact.pk, method=method,
                actor_user_id=grant.user.pk, acting_profile_id=grant.profile.pk,
            )
            try:
                with transaction.atomic(using=ALIAS):
                    VisitorIdentity.objects.bulk_create([identity])
            except IntegrityError:
                identity = VisitorIdentity.objects.filter(**key).first()
                if identity is None:
                    raise ValidationError('Visitor identity could not be verified.') from None
        if identity.contact_id != contact.pk:
            raise ValidationError('Visitor identity conflict.') from None
    return json_value({field: getattr(identity, field) for field in IDENTITY_OUTPUT_FIELDS})


def list_contact_events(actor: Actor, contact_id, limit=50, offset=0) -> dict:
    """Tenant-scoped newest-first page; limit 1..100, offset 0..100000."""
    grant = authorize(actor)
    require_engagement()
    contact = _contact(grant, contact_id)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValidationError('Use a limit from 1 to 100 and an offset from 0 to 100000.')
    records = EngagementEvent.objects.filter(
        charity_id=grant.profile.charity_id, contact_id=contact.pk,
    ).order_by('-occurred_at', 'id').values(*EVENT_OUTPUT_FIELDS)[offset:offset + limit]
    return json_value({'events': list(records), 'limit': limit, 'offset': offset})


def refresh_contact_summary(actor: Actor, contact_id) -> dict:
    """Admin-only explicit projection; exactly one summary-row write on default.

    Counts only non-bot web events. Sessions are distinct (website, visitor,
    session) tuples; null session IDs are not invented sessions. Reads are an
    eventually consistent snapshot, not a cross-database atomic transaction.
    """
    grant = authorize(actor, write=True, admin=True)
    require_engagement()
    contact = _contact(grant, contact_id)
    with transaction.atomic(using='default'):
        # Serialize refreshes for this contact, including the first summary insert.
        Contact.objects.using('default').select_for_update().get(pk=contact.pk, charity_id=grant.profile.charity_id)
        events = EngagementEvent.objects.filter(
            charity_id=grant.profile.charity_id, contact_id=contact.pk, event_type__in=WEB_EVENT_TYPES,
        ).filter(Q(properties__bot=False) | Q(properties__bot__isnull=True))
        totals = events.aggregate(event_count=Count('id'), last_web_at=Max('occurred_at'))
        totals['session_count'] = events.filter(session_id__isnull=False).values(
            'website_id', 'visitor_id', 'session_id',
        ).distinct().count()
        totals['computed_at'] = timezone.now()
        summary, _ = ContactEngagementSummary.objects.using('default').update_or_create(
            contact=contact,
            defaults={
                **totals, 'charity': grant.profile.charity,
                'created_by': grant.user, 'acting_profile': grant.profile,
            },
        )
    return json_value({'id': summary.pk, 'contact_id': contact.pk, 'charity_id': contact.charity_id, **totals})