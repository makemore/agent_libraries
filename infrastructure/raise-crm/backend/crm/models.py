"""Fundraising records and tenant invariants, independent of request authorization.

Normal saves validate the instance. Bulk writes bypass model validation and must
be controlled by services, as must permissions, append-only ledger operations,
idempotency races, and cumulative refund/pledge accounting.
"""

import re
import uuid
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import MinValueValidator, RegexValidator, validate_email
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


currency_validator = RegexValidator(r'\A[A-Z]{3}\Z', 'Use a three-letter uppercase currency code.')


def validate_iana_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError('Use a valid IANA timezone name.')


def validate_storage_key(value):
    """Accept bounded relative object keys, never URLs or traversal paths."""
    if (
        not isinstance(value, str)
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,511}', value)
        or any(part in ('', '.', '..') for part in value.split('/'))
    ):
        raise ValidationError('Use a relative storage key without traversal, URL syntax, or empty segments.')


def normalize_hostname(value):
    """Canonical ASCII hostname only; reject schemes, ports, paths and userinfo."""
    if not isinstance(value, str):
        raise ValidationError('Use a hostname, not a URL.')
    try:
        hostname = value.strip().removesuffix('.').encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise ValidationError('Use a valid hostname.')
    labels = hostname.split('.')
    if (
        not hostname
        or len(hostname) > 253
        or all(label.isdigit() for label in labels)
        or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
    ):
        raise ValidationError('Use a hostname without a scheme, port, path, or credentials.')
    return hostname


def validate_reference_changes(value):
    """Audit values are UUID references/null, optionally wrapped in before/after."""
    if not isinstance(value, dict) or len(value) > 100:
        raise ValidationError('Changes must be an object with at most 100 reference fields.')
    for field_name, change in value.items():
        if not isinstance(field_name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', field_name):
            raise ValidationError('Change keys must be field identifiers.')
        if isinstance(change, dict):
            if not change or not set(change).issubset({'before', 'after'}):
                raise ValidationError('Reference changes may contain only before and after.')
            references = change.values()
        else:
            references = (change,)
        for reference in references:
            if reference is None:
                continue
            try:
                if not isinstance(reference, str):
                    raise ValueError
                uuid.UUID(reference)
            except (ValueError, AttributeError):
                raise ValidationError('Audit changes may contain only UUID references or null, never content.')


class UUIDTimestampModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Charity(UUIDTimestampModel):
    name = models.CharField(max_length=255)
    default_currency = models.CharField(max_length=3, validators=[currency_validator])
    timezone = models.CharField(max_length=100, default='UTC', validators=[validate_iana_timezone])
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class CharityScopedModel(UUIDTimestampModel):
    charity = models.ForeignKey(Charity, on_delete=models.PROTECT, related_name='%(class)s_records')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='%(app_label)s_%(class)s_created',
    )
    acting_profile = models.ForeignKey(
        'accounts.Profile', on_delete=models.PROTECT, null=True, blank=True,
        related_name='%(app_label)s_%(class)s_acted',
    )

    class Meta:
        abstract = True
        indexes = [models.Index(fields=['charity', '-created_at'])]

    def _related(self, field_name):
        """Resolve an optional relation (or relation path) during full_clean."""
        related = self
        for part in field_name.split('.'):
            if related is None or not getattr(related, f'{part}_id', None):
                return None
            try:
                related = getattr(related, part)
            except (ObjectDoesNotExist, ValidationError, ValueError, TypeError):
                # Field validation reports invalid or nonexistent foreign keys.
                return None
        return related

    def clean(self):
        super().clean()
        errors = {}
        for field in self._meta.fields:
            if not field.is_relation or field.name == 'charity':
                continue
            related = self._related(field.name)
            if related is not None and hasattr(related, 'charity_id'):
                if related.charity_id != self.charity_id:
                    errors[field.name] = 'The related record must belong to the same charity.'
        if errors:
            raise ValidationError(errors)

    def _validate_linked_records(self, *field_names):
        """Financial links must describe one contact and one currency."""
        contact_id = getattr(self, 'contact_id', None)
        currency = getattr(self, 'currency', None)
        errors = {}
        for field_name in field_names:
            related = self._related(field_name)
            if related is None:
                continue
            linked_contact = getattr(related, 'contact_id', None)
            linked_currency = getattr(related, 'currency', None)
            messages = []
            if contact_id and linked_contact and linked_contact != contact_id:
                messages.append('Linked records must refer to the same contact.')
            if currency and linked_currency and linked_currency != currency:
                messages.append('Linked records must use the same currency.')
            contact_id = contact_id or linked_contact
            currency = currency or linked_currency
            if messages:
                errors.setdefault(field_name.split('.')[0], []).extend(messages)
        if errors:
            raise ValidationError(errors)

    def _validate_date_range(self):
        # full_clean still calls clean when an individual date failed parsing.
        if (
            isinstance(self.start_date, date)
            and isinstance(self.end_date, date)
            and self.end_date < self.start_date
        ):
            raise ValidationError({'end_date': 'End date must be on or after start date.'})


class Contact(CharityScopedModel):
    class Kind(models.TextChoices):
        PERSON = 'person', 'Person'
        COMPANY = 'company', 'Company'
        TRUST = 'trust', 'Trust'
        FOUNDATION = 'foundation', 'Foundation'

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.PERSON)
    display_name = models.CharField(max_length=255)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    owner = models.ForeignKey(
        'accounts.Profile', on_delete=models.PROTECT, null=True, blank=True, related_name='owned_contacts',
    )
    background = models.TextField(blank=True)
    do_not_solicit = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta(CharityScopedModel.Meta):
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'display_name']),
            models.Index(fields=['charity', 'owner', 'is_active']),
        ]

    def __str__(self):
        return self.display_name


class ContactMethod(CharityScopedModel):
    class Kind(models.TextChoices):
        EMAIL = 'email', 'Email'
        PHONE = 'phone', 'Phone'
        POSTAL = 'postal', 'Postal'

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='methods')
    kind = models.CharField(max_length=10, choices=Kind.choices)
    label = models.CharField(max_length=100, blank=True)
    value = models.TextField()
    is_primary = models.BooleanField(default=False)

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=['contact', 'kind'], condition=Q(is_primary=True), name='crm_primary_contact_method',
            ),
        ]
        indexes = CharityScopedModel.Meta.indexes + [models.Index(fields=['charity', 'contact', 'kind'])]

    def clean(self):
        super().clean()
        if self.kind == self.Kind.EMAIL:
            try:
                validate_email(self.value)
            except ValidationError as error:
                raise ValidationError({'value': error.messages})


class ContactRelationship(CharityScopedModel):
    from_contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='relationships_from')
    to_contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='relationships_to')
    relationship_type = models.CharField(max_length=100)
    role_title = models.CharField(max_length=255, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.CheckConstraint(condition=~Q(from_contact=F('to_contact')), name='crm_relationship_not_self'),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True) | Q(end_date__isnull=True) | Q(end_date__gte=F('start_date')),
                name='crm_relationship_dates',
            ),
        ]
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'from_contact']),
            models.Index(fields=['charity', 'to_contact']),
        ]

    def clean(self):
        super().clean()
        if self.from_contact_id and self.from_contact_id == self.to_contact_id:
            raise ValidationError({'to_contact': 'A contact cannot be related to itself.'})
        self._validate_date_range()


class ConsentRecord(CharityScopedModel):
    class Status(models.TextChoices):
        GRANTED = 'granted', 'Granted'
        WITHDRAWN = 'withdrawn', 'Withdrawn'
        UNKNOWN = 'unknown', 'Unknown'

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='consent_records')
    channel = models.CharField(max_length=30)
    purpose = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNKNOWN)
    occurred_at = models.DateTimeField(default=timezone.now)
    source = models.CharField(max_length=100, blank=True)
    evidence = models.TextField(blank=True)

    class Meta(CharityScopedModel.Meta):
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'contact', '-occurred_at']),
            models.Index(fields=['charity', 'contact', 'channel', 'purpose', '-occurred_at']),
        ]


class Fund(CharityScopedModel):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    is_restricted = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Appeal(CharityScopedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        ACTIVE = 'active', 'Active'
        CLOSED = 'closed', 'Closed'

    name = models.CharField(max_length=255)
    target_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))],
    )
    currency = models.CharField(max_length=3, validators=[currency_validator])
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    default_fund = models.ForeignKey(Fund, on_delete=models.PROTECT, null=True, blank=True, related_name='appeals')

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.CheckConstraint(condition=Q(target_amount__gte=0), name='crm_appeal_target_nonnegative'),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True) | Q(end_date__isnull=True) | Q(end_date__gte=F('start_date')),
                name='crm_appeal_dates',
            ),
        ]
        indexes = CharityScopedModel.Meta.indexes + [models.Index(fields=['charity', 'status', 'start_date'])]

    def clean(self):
        super().clean()
        self._validate_date_range()

    def __str__(self):
        return self.name


class Opportunity(CharityScopedModel):
    class Kind(models.TextChoices):
        MAJOR_GIFT = 'major_gift', 'Major gift'
        GRANT = 'grant', 'Grant'
        SPONSORSHIP = 'sponsorship', 'Sponsorship'

    class Stage(models.TextChoices):
        IDENTIFIED = 'identified', 'Identified'
        QUALIFYING = 'qualifying', 'Qualifying'
        CULTIVATING = 'cultivating', 'Cultivating'
        ASKED = 'asked', 'Asked'
        COMMITTED = 'committed', 'Committed'
        DECLINED = 'declined', 'Declined'

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='opportunities')
    owner = models.ForeignKey(
        'accounts.Profile', on_delete=models.PROTECT, null=True, blank=True, related_name='owned_opportunities',
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    title = models.CharField(max_length=255)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.IDENTIFIED)
    expected_amount = models.DecimalField(
        max_digits=18, decimal_places=2, default=Decimal('0.00'), validators=[MinValueValidator(Decimal('0.00'))],
    )
    currency = models.CharField(max_length=3, validators=[currency_validator])
    expected_date = models.DateField(null=True, blank=True)
    appeal = models.ForeignKey(Appeal, on_delete=models.PROTECT, null=True, blank=True, related_name='opportunities')
    fund = models.ForeignKey(Fund, on_delete=models.PROTECT, null=True, blank=True, related_name='opportunities')

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.CheckConstraint(condition=Q(expected_amount__gte=0), name='crm_opportunity_nonnegative'),
        ]
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'contact', '-created_at']),
            models.Index(fields=['charity', 'owner', 'stage']),
            models.Index(fields=['charity', 'stage', 'expected_date']),
        ]

    def clean(self):
        super().clean()
        self._validate_linked_records('appeal')


class Pledge(CharityScopedModel):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        CANCELLED = 'cancelled', 'Cancelled'

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='pledges')
    amount = models.DecimalField(max_digits=18, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3, validators=[currency_validator])
    pledged_at = models.DateTimeField(default=timezone.now)
    due_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.PROTECT, null=True, blank=True, related_name='pledges',
    )
    appeal = models.ForeignKey(Appeal, on_delete=models.PROTECT, null=True, blank=True, related_name='pledges')
    fund = models.ForeignKey(Fund, on_delete=models.PROTECT, null=True, blank=True, related_name='pledges')

    class Meta(CharityScopedModel.Meta):
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='crm_pledge_positive')]
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'contact', '-pledged_at']),
            models.Index(fields=['charity', 'status', 'due_date']),
        ]

    def clean(self):
        super().clean()
        self._validate_linked_records('opportunity', 'appeal')


class Donation(CharityScopedModel):
    class Kind(models.TextChoices):
        GIFT = 'gift', 'Gift'
        REFUND = 'refund', 'Refund'

    class AcknowledgementStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACKNOWLEDGED = 'acknowledged', 'Acknowledged'

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='donations')
    amount = models.DecimalField(max_digits=18, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    currency = models.CharField(max_length=3, validators=[currency_validator])
    received_at = models.DateTimeField(default=timezone.now)
    payment_method = models.CharField(max_length=50, blank=True)
    source = models.CharField(max_length=100, blank=True)
    external_id = models.CharField(max_length=255, blank=True)
    appeal = models.ForeignKey(Appeal, on_delete=models.PROTECT, null=True, blank=True, related_name='donations')
    fund = models.ForeignKey(Fund, on_delete=models.PROTECT, null=True, blank=True, related_name='donations')
    pledge = models.ForeignKey(Pledge, on_delete=models.PROTECT, null=True, blank=True, related_name='donations')
    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.PROTECT, null=True, blank=True, related_name='donations',
    )
    acknowledgement_status = models.CharField(
        max_length=20, choices=AcknowledgementStatus.choices, default=AcknowledgementStatus.PENDING,
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.GIFT)
    original_donation = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True, related_name='refunds',
    )

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='crm_donation_positive'),
            models.CheckConstraint(
                condition=Q(kind='gift', original_donation__isnull=True) | Q(kind='refund', original_donation__isnull=False),
                name='crm_donation_refund_original',
            ),
            models.CheckConstraint(condition=~Q(original_donation=F('id')), name='crm_donation_not_self_refund'),
            models.UniqueConstraint(
                fields=['charity', 'source', 'external_id'], condition=~Q(source='') & ~Q(external_id=''),
                name='crm_donation_external_unique',
            ),
            models.CheckConstraint(
                condition=~Q(acknowledgement_status='acknowledged') | Q(acknowledged_at__isnull=False),
                name='crm_donation_acknowledged_at',
            ),
        ]
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'contact', '-received_at']),
            models.Index(fields=['charity', '-received_at']),
            models.Index(fields=['charity', 'acknowledgement_status']),
        ]

    def clean(self):
        super().clean()
        self._validate_linked_records('appeal', 'pledge', 'opportunity', 'original_donation')
        errors = {}
        if self.kind == self.Kind.REFUND:
            original = self._related('original_donation')
            if not self.original_donation_id:
                errors['original_donation'] = 'A refund must reference an original gift.'
            elif self.original_donation_id == self.pk:
                errors['original_donation'] = 'A donation cannot refund itself.'
            elif original is not None and original.kind != self.Kind.GIFT:
                errors['original_donation'] = 'A refund must reference a gift, not another refund.'
        elif self.original_donation_id:
            errors['original_donation'] = 'Only refunds may reference an original donation.'
        if self.acknowledgement_status == self.AcknowledgementStatus.ACKNOWLEDGED and not self.acknowledged_at:
            errors['acknowledged_at'] = 'Acknowledged donations require an acknowledgement time.'
        if errors:
            raise ValidationError(errors)


class Interaction(CharityScopedModel):
    class Channel(models.TextChoices):
        EMAIL = 'email', 'Email'
        PHONE = 'phone', 'Phone'
        MEETING = 'meeting', 'Meeting'
        SMS = 'sms', 'SMS'
        LETTER = 'letter', 'Letter'
        SOCIAL = 'social', 'Social'
        INTERNAL_NOTE = 'internal_note', 'Internal note'
        WEB = 'web', 'Web'

    class Direction(models.TextChoices):
        INBOUND = 'inbound', 'Inbound'
        OUTBOUND = 'outbound', 'Outbound'
        INTERNAL = 'internal', 'Internal'

    class Visibility(models.TextChoices):
        TEAM = 'team', 'Team'
        RESTRICTED = 'restricted', 'Restricted'

    channel = models.CharField(max_length=20, choices=Channel.choices)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=255, blank=True)
    delivery_status = models.CharField(max_length=30, blank=True)
    purpose = models.CharField(max_length=100, blank=True)
    source = models.CharField(max_length=100, blank=True)
    external_id = models.CharField(max_length=255, blank=True)
    thread_id = models.CharField(max_length=255, blank=True)
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.TEAM)
    appeal = models.ForeignKey(Appeal, on_delete=models.PROTECT, null=True, blank=True, related_name='interactions')
    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.PROTECT, null=True, blank=True, related_name='interactions',
    )
    pledge = models.ForeignKey(Pledge, on_delete=models.PROTECT, null=True, blank=True, related_name='interactions')
    donation = models.ForeignKey(Donation, on_delete=models.PROTECT, null=True, blank=True, related_name='interactions')

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=['charity', 'source', 'external_id'], condition=~Q(source='') & ~Q(external_id=''),
                name='crm_interaction_external_unique',
            ),
        ]
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', '-occurred_at']),
            models.Index(fields=['charity', 'channel', '-occurred_at']),
            models.Index(fields=['charity', 'thread_id', '-occurred_at']),
        ]

    def clean(self):
        super().clean()
        self._validate_linked_records('appeal', 'opportunity', 'pledge', 'donation')


class InteractionParticipant(CharityScopedModel):
    interaction = models.ForeignKey(Interaction, on_delete=models.PROTECT, related_name='participants')
    contact = models.ForeignKey(
        Contact, on_delete=models.PROTECT, null=True, blank=True, related_name='interaction_participations',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name='interaction_participations',
    )
    name = models.CharField(max_length=255, blank=True)
    address = models.CharField(max_length=320, blank=True)
    role = models.CharField(max_length=50)

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=Q(contact__isnull=True) | Q(user__isnull=True), name='crm_participant_one_identity',
            ),
            models.CheckConstraint(
                condition=Q(contact__isnull=False) | Q(user__isnull=False) | ~Q(name='') | ~Q(address=''),
                name='crm_participant_has_identity',
            ),
        ]
        indexes = CharityScopedModel.Meta.indexes + [models.Index(fields=['charity', 'contact', 'interaction'])]

    def clean(self):
        super().clean()
        if self.contact_id and self.user_id:
            raise ValidationError({'user': 'Choose a contact or a user, not both.'})
        if not self.contact_id and not self.user_id and not ((self.name or '').strip() or (self.address or '').strip()):
            raise ValidationError({'name': 'External participants require a snapshot name or address.'})


class InteractionAttachment(CharityScopedModel):
    interaction = models.ForeignKey(Interaction, on_delete=models.PROTECT, related_name='attachments')
    storage_key = models.CharField(max_length=512, validators=[validate_storage_key])
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=255)
    size_bytes = models.PositiveBigIntegerField()


class FundraisingTask(CharityScopedModel):
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name='fundraising_tasks')
    assigned_to = models.ForeignKey(
        'accounts.Profile', on_delete=models.PROTECT, null=True, blank=True, related_name='assigned_tasks',
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    task_type = models.CharField(max_length=50)
    due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    completed_at = models.DateTimeField(null=True, blank=True)
    originating_interaction = models.ForeignKey(
        Interaction, on_delete=models.PROTECT, null=True, blank=True, related_name='originated_tasks',
    )
    resulting_interaction = models.ForeignKey(
        Interaction, on_delete=models.PROTECT, null=True, blank=True, related_name='resulted_tasks',
    )
    opportunity = models.ForeignKey(
        Opportunity, on_delete=models.PROTECT, null=True, blank=True, related_name='fundraising_tasks',
    )
    pledge = models.ForeignKey(Pledge, on_delete=models.PROTECT, null=True, blank=True, related_name='fundraising_tasks')
    donation = models.ForeignKey(
        Donation, on_delete=models.PROTECT, null=True, blank=True, related_name='fundraising_tasks',
    )

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.CheckConstraint(
                condition=~Q(status='completed') | Q(completed_at__isnull=False), name='crm_task_completed_at',
            ),
        ]
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'contact', '-created_at']),
            models.Index(fields=['charity', 'assigned_to', 'status', 'due_at']),
            models.Index(fields=['charity', 'status', 'due_at']),
        ]

    def clean(self):
        super().clean()
        self._validate_linked_records(
            'opportunity', 'pledge', 'donation',
            *(f'{interaction}.{link}' for interaction in ('originating_interaction', 'resulting_interaction')
              for link in ('appeal', 'opportunity', 'pledge', 'donation')),
        )
        if self.status == self.Status.COMPLETED and not self.completed_at:
            raise ValidationError({'completed_at': 'Completed tasks require a completion time.'})


class Website(CharityScopedModel):
    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=253)
    is_active = models.BooleanField(default=True)
    consent_required = models.BooleanField(default=True)

    class Meta(CharityScopedModel.Meta):
        constraints = [models.UniqueConstraint(fields=['charity', 'domain'], name='crm_website_domain_unique')]

    def clean(self):
        super().clean()
        try:
            self.domain = normalize_hostname(self.domain)
        except ValidationError as error:
            raise ValidationError({'domain': error.messages})


class ContactEngagementSummary(CharityScopedModel):
    contact = models.OneToOneField(Contact, on_delete=models.PROTECT, related_name='engagement_summary')
    last_web_at = models.DateTimeField(null=True, blank=True)
    event_count = models.PositiveBigIntegerField(default=0)
    session_count = models.PositiveBigIntegerField(default=0)
    computed_at = models.DateTimeField(null=True, blank=True)


class AuditRecord(CharityScopedModel):
    action = models.CharField(max_length=100)
    entity_type = models.CharField(max_length=100)
    entity_id = models.UUIDField()
    changes = models.JSONField(default=dict, blank=True, validators=[validate_reference_changes])

    class Meta(CharityScopedModel.Meta):
        indexes = CharityScopedModel.Meta.indexes + [
            models.Index(fields=['charity', 'entity_type', 'entity_id', '-created_at']),
        ]

    def clean(self):
        super().clean()
        # Empty JSON lists/strings bypass field validators when blank=True.
        try:
            validate_reference_changes(self.changes)
        except ValidationError as error:
            raise ValidationError({'changes': error.messages})


class MutationReceipt(CharityScopedModel):
    key = models.CharField(max_length=128)
    operation = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    result = models.JSONField(default=dict, blank=True)

    class Meta(CharityScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=['charity', 'acting_profile', 'key'], name='crm_mutation_receipt_unique',
            ),
            models.UniqueConstraint(
                fields=['charity', 'key'], condition=Q(acting_profile__isnull=True),
                name='crm_bootstrap_receipt_unique',
            ),
        ]