# Generated for ACE 1.0 durable runtime - new models

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ace_django", "0003_durable_inbox_and_runtime_fields"),
    ]

    operations = [
        # QueueConfig model
        migrations.CreateModel(
            name="QueueConfig",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=100, unique=True)),
                ("enabled", models.BooleanField(default=True)),
                ("global_concurrency", models.PositiveIntegerField(blank=True, null=True)),
                ("rate_limit_count", models.PositiveIntegerField(blank=True, null=True)),
                ("rate_limit_period_seconds", models.PositiveIntegerField(blank=True, null=True)),
                ("rate_limit_window_start", models.DateTimeField(blank=True, null=True)),
                ("rate_limit_window_count", models.PositiveIntegerField(default=0)),
                ("partition_concurrency", models.PositiveIntegerField(blank=True, null=True)),
            ],
            options={
                "db_table": "ace_queue_configs",
                "ordering": ["-created_at"],
                "abstract": False,
            },
        ),
        # WorkflowInboxEvent model
        migrations.CreateModel(
            name="WorkflowInboxEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("inbox_sequence", models.PositiveBigIntegerField()),
                ("source_type", models.CharField(max_length=32)),
                ("source_key", models.CharField(max_length=255)),
                ("event_type", models.CharField(max_length=255)),
                ("payload", models.JSONField(default=dict)),
                ("actor", models.CharField(blank=True, max_length=255, null=True)),
                ("occurred_at", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RETRYING", "Retrying"),
                            ("PROCESSED", "Processed"),
                            ("DISCARDED", "Discarded"),
                            ("DEAD_LETTER", "Dead Letter"),
                        ],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("available_at", models.DateTimeField(blank=True, null=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("dead_letter_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_type", models.CharField(blank=True, max_length=255, null=True)),
                ("last_error_message", models.TextField(blank=True, null=True)),
                ("discard_reason", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "workflow_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inbox_events",
                        to="ace_django.workflowrun",
                    ),
                ),
            ],
            options={
                "db_table": "ace_workflow_inbox_events",
                "ordering": ["inbox_sequence"],
                "abstract": False,
            },
        ),
        # WorkflowTransitionFailure model
        migrations.CreateModel(
            name="WorkflowTransitionFailure",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("attempt", models.PositiveIntegerField()),
                ("error_type", models.CharField(max_length=255)),
                ("error_message", models.TextField()),
                ("error_details", models.JSONField(default=dict)),
                ("resolution", models.CharField(blank=True, max_length=32, null=True)),
                (
                    "workflow_run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transition_failures",
                        to="ace_django.workflowrun",
                    ),
                ),
                (
                    "inbox_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="failures",
                        to="ace_django.workflowinboxevent",
                    ),
                ),
            ],
            options={
                "db_table": "ace_workflow_transition_failures",
                "ordering": ["-created_at"],
                "abstract": False,
            },
        ),
    ]
