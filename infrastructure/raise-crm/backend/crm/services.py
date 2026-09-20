"""Authorized, tenant-scoped CRM operations shared by authenticated transports."""

import re
from datetime import datetime, timedelta
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import Profile, ProfileGrant, User
from .access import Actor, authorize, page_bounds
from .models import (
    Appeal, Contact, ContactEngagementSummary, ContactMethod, Donation,
    Interaction, InteractionParticipant, Opportunity, Pledge,
)
from .mutations import audit, json_value, mutate, provenance


def _authorize(actor, *, write=False):
    if (
        not isinstance(actor, Actor) or type(actor.user_id) is not int
        or not 1 <= actor.user_id <= 9223372036854775807 or not isinstance(actor.source, str)
        or not actor.source.strip() or len(actor.source) > 100
    ):
        raise PermissionDenied('Authentication and an acting profile are required.')
    return authorize(actor, write=write)


def _uuid(value):
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError('Invalid record ID.') from None


def _scoped(queryset, grant, record_id):
    record = queryset.filter(charity_id=grant.profile.charity_id, pk=_uuid(record_id)).first()
    if record is None:
        raise ValidationError('Record not found or unavailable.')
    return record


def _text(value, field, maximum, *, required=False, byte_limit=None):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValidationError({field: f'Use text of at most {maximum} characters.'})
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        raise ValidationError({field: 'Use valid Unicode text.'}) from None
    if '\x00' in value or (byte_limit is not None and size > byte_limit):
        raise ValidationError({field: 'Text contains invalid characters or exceeds its byte limit.'})
    return value


def _choice(value, field, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValidationError({field: 'Invalid choice.'})
    return value


def _boolean(value, field):
    if type(value) is not bool:
        raise ValidationError({field: 'Use a boolean.'})
    return value


def _occurred_at(value):
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or timezone.is_naive(parsed):
            raise ValueError
        if parsed > timezone.now() + timedelta(minutes=5):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ValidationError({'occurred_at': 'Use an aware timestamp no more than five minutes in the future.'}) from None
    return parsed


def _mutate(actor, operation, payload, key, callback):
    # Caller source is distinct from an interaction's external-system source.
    try:
        return mutate(actor, operation, {'actor_source': actor.source, **payload}, key, callback)
    except IntegrityError:
        # mutate's transaction has rolled back, including participants and audits.
        # Never expose constraint values or replay another request's external ID.
        raise ValidationError('The request conflicts with an existing record.') from None


def _created(grant, operation, record):
    audit(grant, operation, record)
    return {'id': str(record.pk)}


def _page(queryset, limit, offset, serialize):
    records = list(queryset[offset:offset + limit + 1])
    return {
        'items': [serialize(record) for record in records[:limit]],
        'next_offset': offset + limit if len(records) > limit else None,
    }


def _fields(record, names):
    return json_value({name: getattr(record, name) for name in names})


CONTACT_FIELDS = (
    'id', 'kind', 'display_name', 'first_name', 'last_name', 'owner_id',
    'do_not_solicit', 'is_active', 'created_at', 'updated_at',
)
PARTICIPANT_FIELDS = ('id', 'contact_id', 'user_id', 'name', 'address', 'role')
INTERACTION_FIELDS = (
    'id', 'channel', 'direction', 'occurred_at', 'subject', 'summary', 'outcome',
    'duration_seconds', 'delivery_status', 'purpose', 'source', 'external_id',
    'thread_id', 'visibility', 'opportunity_id', 'appeal_id', 'pledge_id',
    'donation_id', 'created_by_id', 'acting_profile_id', 'created_at',
)


def list_contacts(actor, *, search='', limit=50, offset=0) -> dict:
    grant = _authorize(actor)
    limit, offset = page_bounds(limit, offset)
    search = _text(search, 'search', 255)
    contacts = Contact.objects.filter(charity_id=grant.profile.charity_id)
    if search:
        contacts = contacts.filter(
            Q(display_name__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search),
        )
    return _page(
        contacts.defer('background').order_by('display_name', 'id'),
        limit, offset, lambda item: _fields(item, CONTACT_FIELDS),
    )


def create_contact(
    actor, *, display_name, kind='person', first_name='', last_name='', background='',
    do_not_solicit=False, owner_id=None, idempotency_key,
) -> dict:
    _authorize(actor, write=True)
    values = {
        'display_name': _text(display_name, 'display_name', 255, required=True),
        'kind': _choice(kind, 'kind', Contact.Kind.values),
        'first_name': _text(first_name, 'first_name', 150),
        'last_name': _text(last_name, 'last_name', 150),
        'background': _text(background, 'background', 32000, byte_limit=32000),
        'do_not_solicit': _boolean(do_not_solicit, 'do_not_solicit'),
        'owner_id': _uuid(owner_id) if owner_id is not None else None,
    }

    def write(grant):
        if values['owner_id'] is not None:
            _scoped(Profile.objects.filter(is_active=True), grant, values['owner_id'])
        contact = Contact.objects.create(**provenance(grant), **values)
        return _created(grant, 'create_contact', contact)

    return _mutate(actor, 'create_contact', values, idempotency_key, write)


def get_contact(actor, contact_id) -> dict:
    grant = _authorize(actor)
    contact = _scoped(Contact.objects.all(), grant, contact_id)
    result = _fields(contact, (*CONTACT_FIELDS, 'background', 'created_by_id', 'acting_profile_id'))
    result['methods'] = [
        _fields(method, ('id', 'kind', 'value', 'label', 'is_primary'))
        for method in ContactMethod.objects.filter(
            charity_id=grant.profile.charity_id, contact=contact,
        ).order_by('kind', '-is_primary', 'id')
    ]
    summary = ContactEngagementSummary.objects.filter(
        charity_id=grant.profile.charity_id, contact=contact,
    ).first()
    result['engagement_summary'] = (
        _fields(summary, ('last_web_at', 'event_count', 'session_count', 'computed_at'))
        if summary is not None else None
    )
    return result


def add_contact_method(
    actor, *, contact_id, kind, value, label='', is_primary=False, idempotency_key,
) -> dict:
    _authorize(actor, write=True)
    values = {
        'contact_id': _uuid(contact_id),
        'kind': _choice(kind, 'kind', ContactMethod.Kind.values),
        'value': _text(value, 'value', 4000, required=True, byte_limit=4000),
        'label': _text(label, 'label', 100),
        'is_primary': _boolean(is_primary, 'is_primary'),
    }
    if kind == ContactMethod.Kind.PHONE and (
        not re.fullmatch(r'\+?[0-9(). -]+', value)
        or not 3 <= sum(character.isdigit() for character in value) <= 20
    ):
        raise ValidationError({'value': 'Use a phone number with 3–20 digits and optional phone punctuation.'})

    def write(grant):
        _scoped(Contact.objects.select_for_update(), grant, values['contact_id'])
        method = ContactMethod.objects.create(**provenance(grant), **values)
        return _created(grant, 'add_contact_method', method)

    return _mutate(actor, 'add_contact_method', values, idempotency_key, write)


def _participant_payload(participants):
    if not isinstance(participants, list) or not 1 <= len(participants) <= 100:
        raise ValidationError({'participants': 'Use between 1 and 100 participants.'})
    result = []
    for participant in participants:
        if not isinstance(participant, dict) or not set(participant).issubset(PARTICIPANT_FIELDS[1:]):
            raise ValidationError({'participants': 'Unknown participant fields.'})
        values = dict(participant)
        contact_id, user_id = values.get('contact_id'), values.get('user_id')
        if contact_id is not None and user_id is not None:
            raise ValidationError({'participants': 'Choose a contact or a user, not both.'})
        if contact_id is not None:
            values['contact_id'] = _uuid(contact_id)
        if user_id is not None:
            if isinstance(user_id, str) and re.fullmatch(r'[0-9]{1,19}', user_id):
                user_id = int(user_id)
            if type(user_id) is not int or not 1 <= user_id <= 9223372036854775807:
                raise ValidationError({'participants': 'Invalid user ID.'})
            values['user_id'] = user_id
        for field, maximum in (('name', 255), ('address', 320), ('role', 50)):
            if field in values:
                _text(values[field], field, maximum, required=field == 'role')
        if contact_id is None and user_id is None and not (
            values.get('name', '').strip() or values.get('address', '').strip()
        ):
            raise ValidationError({'participants': 'External participants require a name or address.'})
        result.append(values)
    if not any(item.get('contact_id') is not None for item in result):
        raise ValidationError({'participants': 'At least one Contact participant is required.'})
    return result


def _participant_values(grant, participant, channel):
    values = dict(participant)
    values.setdefault('role', 'participant')
    name, address = '', ''
    if values.get('contact_id') is not None:
        contact = _scoped(Contact.objects.all(), grant, values['contact_id'])
        name = contact.display_name
        method_kind = {'email': 'email', 'phone': 'phone', 'sms': 'phone'}.get(channel)
        if method_kind and 'address' not in values:
            address = ContactMethod.objects.filter(
                charity_id=grant.profile.charity_id, contact=contact, kind=method_kind, is_primary=True,
            ).values_list('value', flat=True).first() or ''
    elif values.get('user_id') is not None:
        allowed = ProfileGrant.objects.filter(
            user_id=OuterRef('pk'), is_active=True, profile__is_active=True,
            profile__charity_id=grant.profile.charity_id, profile__charity__is_active=True,
        )
        user = User.objects.filter(pk=values['user_id'], is_active=True).filter(Exists(allowed)).first()
        if user is None:
            raise ValidationError('Record not found or unavailable.')
        name = user.get_full_name()
        address = user.email if channel == Interaction.Channel.EMAIL else ''
    # Presence, not truthiness: explicitly supplied empty historical snapshots
    # must not be replaced with today's identity or contact methods.
    values.setdefault('name', name)
    values.setdefault('address', address)
    _text(values['name'], 'name', 255)
    _text(values['address'], 'address', 320)
    return values


def log_interaction(
    actor, *, participants: list[dict], channel, direction, occurred_at, subject='', body='',
    summary='', outcome='', duration_seconds=None, delivery_status='', purpose='', source='',
    external_id='', thread_id='', visibility='team', opportunity_id=None, appeal_id=None,
    pledge_id=None, donation_id=None, idempotency_key,
) -> dict:
    _authorize(actor, write=True)
    participants = _participant_payload(participants)
    values = {
        'channel': _choice(channel, 'channel', Interaction.Channel.values),
        'direction': _choice(direction, 'direction', Interaction.Direction.values),
        'occurred_at': _occurred_at(occurred_at),
        'subject': _text(subject, 'subject', 255),
        'body': _text(body, 'body', 32000, byte_limit=32000),
        'summary': _text(summary, 'summary', 4000, byte_limit=4000),
        'outcome': _text(outcome, 'outcome', 255),
        'duration_seconds': duration_seconds,
        'delivery_status': _text(delivery_status, 'delivery_status', 30),
        'purpose': _text(purpose, 'purpose', 100),
        'source': _text(source, 'source', 100, required=bool(source)),
        'external_id': _text(external_id, 'external_id', 255, required=bool(external_id)),
        'thread_id': _text(thread_id, 'thread_id', 255),
        'visibility': _choice(visibility, 'visibility', Interaction.Visibility.values),
        'opportunity_id': _uuid(opportunity_id) if opportunity_id is not None else None,
        'appeal_id': _uuid(appeal_id) if appeal_id is not None else None,
        'pledge_id': _uuid(pledge_id) if pledge_id is not None else None,
        'donation_id': _uuid(donation_id) if donation_id is not None else None,
    }
    if duration_seconds is not None and (type(duration_seconds) is not int or not 0 <= duration_seconds <= 2147483647):
        raise ValidationError({'duration_seconds': 'Use a nonnegative integer duration.'})
    if (channel == Interaction.Channel.INTERNAL_NOTE) != (direction == Interaction.Direction.INTERNAL):
        raise ValidationError({'direction': 'Only internal notes use the internal direction, and it is required for them.'})
    if channel == Interaction.Channel.WEB and purpose not in {'enquiry', 'application', 'event_registration'}:
        raise ValidationError({'purpose': 'Web interactions must be enquiries, applications or event registrations, not telemetry.'})
    if external_id and not source:
        raise ValidationError({'source': 'An external ID requires a source.'})

    def write(grant):
        for field, model in (
            ('opportunity_id', Opportunity), ('appeal_id', Appeal),
            ('pledge_id', Pledge), ('donation_id', Donation),
        ):
            if values[field] is not None:
                _scoped(model.objects.all(), grant, values[field])
        snapshots = [_participant_values(grant, item, channel) for item in participants]
        # The model and database enforce source/external_id uniqueness. A new
        # request must conflict, never silently adopt an existing interaction.
        interaction = Interaction.objects.create(**provenance(grant), **values)
        for snapshot in snapshots:
            participant = InteractionParticipant.objects.create(
                **provenance(grant), interaction=interaction, **snapshot,
            )
            audit(grant, 'log_interaction', participant)
        return _created(grant, 'log_interaction', interaction)

    payload = {**values, 'participants': participants, 'occurred_at': values['occurred_at'].isoformat()}
    return _mutate(actor, 'log_interaction', payload, idempotency_key, write)


def _visible_interactions(grant):
    interactions = Interaction.objects.filter(charity_id=grant.profile.charity_id)
    if grant.role != ProfileGrant.Role.ADMIN:
        interactions = interactions.filter(
            Q(visibility=Interaction.Visibility.TEAM)
            | Q(visibility=Interaction.Visibility.RESTRICTED, acting_profile_id=grant.profile_id),
        )
    return interactions.prefetch_related(Prefetch(
        'participants',
        queryset=InteractionParticipant.objects.filter(charity_id=grant.profile.charity_id).order_by('created_at', 'id'),
    ))


def _interaction_result(interaction, *, include_body=False):
    fields = (*INTERACTION_FIELDS, 'body') if include_body else INTERACTION_FIELDS
    result = _fields(interaction, fields)
    result['participants'] = [_fields(item, PARTICIPANT_FIELDS) for item in interaction.participants.all()]
    return result


def get_interaction(actor, interaction_id) -> dict:
    grant = _authorize(actor)
    interaction = _scoped(_visible_interactions(grant), grant, interaction_id)
    return _interaction_result(interaction, include_body=True)


def contact_timeline(actor, contact_id, *, limit=50, offset=0) -> dict:
    grant = _authorize(actor)
    limit, offset = page_bounds(limit, offset)
    contact = _scoped(Contact.objects.all(), grant, contact_id)
    participation = InteractionParticipant.objects.filter(
        charity_id=grant.profile.charity_id, contact=contact, interaction_id=OuterRef('pk'),
    )
    # Exists gives one interaction per timeline, even for repeated contact roles.
    interactions = _visible_interactions(grant).filter(Exists(participation)).defer('body')
    return _page(interactions.order_by('-occurred_at', '-id'), limit, offset, _interaction_result)