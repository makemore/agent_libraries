"""Atomic low-volume CRM mutations with retry protection and attribution."""

import hashlib
import json

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from accounts.models import Profile
from .access import authorize
from .models import AuditRecord, MutationReceipt


def json_value(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder, allow_nan=False))


def provenance(grant):
    return {
        'charity': grant.profile.charity,
        'created_by': grant.user,
        'acting_profile': grant.profile,
    }


def audit(grant, operation, entity):
    # Do not duplicate correspondence, addresses or credentials into audit logs.
    AuditRecord.objects.create(
        **provenance(grant), action=operation,
        entity_type=entity._meta.label_lower, entity_id=entity.pk,
    )


def mutate(actor, operation, payload, idempotency_key, callback, *, admin=False):
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128 or not idempotency_key.strip():
        raise ValidationError('A nonempty idempotency key of at most 128 characters is required.')
    encoded = json.dumps(payload, cls=DjangoJSONEncoder, sort_keys=True, allow_nan=False)
    if len(encoded.encode()) > 262144:
        raise ValidationError('Mutation payload exceeds 256 KiB.')
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with transaction.atomic(using='default'):
        grant = authorize(actor, write=True, admin=admin)
        # Serializes same-profile retries. Financial workflows additionally lock
        # their shared pledge/gift, protecting concurrent writes across profiles.
        Profile.objects.select_for_update().get(pk=grant.profile_id)
        grant = authorize(actor, write=True, admin=admin)
        receipt = MutationReceipt.objects.filter(
            charity=grant.profile.charity, acting_profile=grant.profile, key=idempotency_key,
        ).first()
        if receipt:
            if receipt.created_by_id != actor.user_id or receipt.operation != operation or receipt.fingerprint != fingerprint:
                raise ValidationError('Idempotency key already used for a different request.')
            return receipt.result
        result = json_value(callback(grant))
        MutationReceipt.objects.create(
            **provenance(grant), key=idempotency_key, operation=operation,
            fingerprint=fingerprint, result=result,
        )
        return result