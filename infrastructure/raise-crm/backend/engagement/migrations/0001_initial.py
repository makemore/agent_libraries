import uuid

import django.core.validators
import django.utils.timezone
from django.db import migrations, models

import engagement.validation


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name='EngagementEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('charity_id', models.UUIDField()),
                ('website_id', models.UUIDField()),
                ('website_domain', models.CharField(max_length=253)),
                ('visitor_id', models.UUIDField()),
                ('contact_id', models.UUIDField(blank=True, null=True)),
                ('session_id', models.UUIDField(blank=True, null=True)),
                ('event_type', models.CharField(choices=[
                    ('page_view', 'page_view'), ('donation_started', 'donation_started'),
                    ('donation_completed', 'donation_completed'), ('enquiry_submitted', 'enquiry_submitted'),
                    ('proposal_downloaded', 'proposal_downloaded'), ('event_registered', 'event_registered'),
                    ('email_delivered', 'email_delivered'), ('email_opened', 'email_opened'),
                    ('email_clicked', 'email_clicked'),
                ], max_length=32)),
                ('occurred_at', models.DateTimeField()),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('page_url', models.CharField(blank=True, max_length=512)),
                ('properties', models.JSONField(blank=True, default=dict, validators=[engagement.validation.validate_properties])),
                ('actor_user_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('acting_profile_id', models.UUIDField(blank=True, null=True)),
                ('schema_version', models.PositiveSmallIntegerField(default=1, validators=[
                    django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(1),
                ])),
                ('fingerprint', models.CharField(max_length=64, validators=[
                    django.core.validators.RegexValidator('\\A[0-9a-f]{64}\\Z'),
                ])),
            ],
            options={
                'abstract': False, 'base_manager_name': 'objects', 'default_manager_name': 'objects',
                'indexes': [
                    models.Index(fields=['charity_id', 'contact_id', '-occurred_at', 'id'], name='eng_event_contact_time'),
                    models.Index(fields=['charity_id', 'website_id', 'visitor_id'], name='eng_event_visitor'),
                ],
            },
        ),
        migrations.CreateModel(
            name='VisitorIdentity',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('charity_id', models.UUIDField()),
                ('website_id', models.UUIDField()),
                ('visitor_id', models.UUIDField()),
                ('contact_id', models.UUIDField()),
                ('verified_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('method', models.CharField(choices=[('admin_verified', 'Admin verified')], max_length=32)),
                ('actor_user_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('acting_profile_id', models.UUIDField(blank=True, null=True)),
            ],
            options={
                'abstract': False, 'base_manager_name': 'objects', 'default_manager_name': 'objects',
                'constraints': [models.UniqueConstraint(
                    fields=('charity_id', 'website_id', 'visitor_id'), name='eng_identity_visitor_unique',
                )],
            },
        ),
    ]