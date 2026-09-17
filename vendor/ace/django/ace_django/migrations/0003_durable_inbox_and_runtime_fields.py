# Generated for ACE 1.0 durable runtime

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ace_django", "0002_activitygrouprun_activityrun_group_and_more"),
    ]

    operations = [
        # WorkflowRun 1.0 fields (all nullable for safe additive migration)
        migrations.AddField(
            model_name="workflowrun",
            name="execution_backend",
            field=models.CharField(default="django", max_length=32),
        ),
        migrations.AddField(
            model_name="workflowrun",
            name="last_inbox_sequence",
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="workflowrun",
            name="deadline_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workflowrun",
            name="blocked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workflowrun",
            name="blocked_from_status",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="workflowrun",
            name="block_reason",
            field=models.TextField(blank=True, null=True),
        ),
        # ActivityRun 1.0 fields
        migrations.AddField(
            model_name="activityrun",
            name="execution_mode",
            field=models.CharField(
                choices=[("STANDARD", "Standard"), ("TRANSACTIONAL", "Transactional")],
                default="STANDARD",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="activityrun",
            name="partition_key",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="activityrun",
            name="schedule_to_close_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="activityrun",
            name="start_to_close_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="activityrun",
            name="heartbeat_seconds",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="activityrun",
            name="schedule_to_close_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # ActivityAttempt 1.0 fields
        migrations.AddField(
            model_name="activityattempt",
            name="start_to_close_deadline",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="activityattempt",
            name="heartbeat_deadline",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="activityattempt",
            name="timeout_cause",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        # WorkflowEvent 1.0 field
        migrations.AddField(
            model_name="workflowevent",
            name="commands",
            field=models.JSONField(blank=True, null=True),
        ),
        # WorkflowTimer 1.0 fields
        migrations.AddField(
            model_name="workflowtimer",
            name="delivered_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workflowtimer",
            name="delivery_attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="workflowtimer",
            name="next_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workflowtimer",
            name="last_error",
            field=models.TextField(blank=True, null=True),
        ),
        # AceWorkerHeartbeat 1.0 fields
        migrations.AddField(
            model_name="aceworkerheartbeat",
            name="role",
            field=models.CharField(
                choices=[
                    ("ACTIVITY", "Activity Worker"),
                    ("DISPATCHER", "Workflow Dispatcher"),
                    ("TIMER", "Timer Service"),
                    ("DEADLINE", "Deadline Service"),
                ],
                default="ACTIVITY",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="aceworkerheartbeat",
            name="deployment_id",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="aceworkerheartbeat",
            name="backend",
            field=models.CharField(default="django", max_length=32),
        ),
        migrations.AddField(
            model_name="aceworkerheartbeat",
            name="workflow_capabilities",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="aceworkerheartbeat",
            name="activity_capabilities",
            field=models.JSONField(default=list),
        ),
    ]
