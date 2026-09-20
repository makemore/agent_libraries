"""Raw events and privileged identity assertions: UUID scalars, no CRM relations.

The event ID is a client-provided global primary key, with charity-scoped replay
checks in services. Never re-label historical anonymous events on identity bind.
"""

import uuid
from datetime import datetime

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

from crm.models import normalize_hostname
from .database import EngagementModel
from .validation import EVENT_TYPES, event_timestamp, sanitize_page_url, validate_properties


class EngagementEvent(EngagementModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    charity_id = models.UUIDField()
    website_id = models.UUIDField()
    website_domain = models.CharField(max_length=253)
    visitor_id = models.UUIDField()
    contact_id = models.UUIDField(null=True, blank=True)
    session_id = models.UUIDField(null=True, blank=True)
    event_type = models.CharField(max_length=32, choices=[(value, value) for value in EVENT_TYPES])
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now, editable=False)
    page_url = models.CharField(max_length=512, blank=True)
    properties = models.JSONField(default=dict, blank=True, validators=[validate_properties])
    actor_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    acting_profile_id = models.UUIDField(null=True, blank=True)
    schema_version = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(1)],
    )
    fingerprint = models.CharField(max_length=64, validators=[RegexValidator(r'\A[0-9a-f]{64}\Z')])

    class Meta(EngagementModel.Meta):
        indexes = [
            models.Index(fields=['charity_id', 'contact_id', '-occurred_at', 'id'], name='eng_event_contact_time'),
            models.Index(fields=['charity_id', 'website_id', 'visitor_id'], name='eng_event_visitor'),
        ]

    def clean(self):
        super().clean()
        self.website_domain = normalize_hostname(self.website_domain)
        self.page_url = sanitize_page_url(self.page_url, self.website_domain)
        self.occurred_at = event_timestamp(self.occurred_at)
        validate_properties(self.properties)


class VisitorIdentity(EngagementModel):
    class Method(models.TextChoices):
        ADMIN_VERIFIED = 'admin_verified', 'Admin verified'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    charity_id = models.UUIDField()
    website_id = models.UUIDField()
    visitor_id = models.UUIDField()
    contact_id = models.UUIDField()
    verified_at = models.DateTimeField(default=timezone.now)
    method = models.CharField(max_length=32, choices=Method.choices)
    actor_user_id = models.PositiveBigIntegerField(null=True, blank=True)
    acting_profile_id = models.UUIDField(null=True, blank=True)

    class Meta(EngagementModel.Meta):
        constraints = [models.UniqueConstraint(
            fields=['charity_id', 'website_id', 'visitor_id'], name='eng_identity_visitor_unique',
        )]

    def clean(self):
        super().clean()
        if not isinstance(self.verified_at, datetime) or timezone.is_naive(self.verified_at):
            raise ValidationError({'verified_at': 'An aware verification timestamp is required.'})