# Generated for ACE 1.0 durable runtime - constraints and indexes

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ace_django", "0004_queue_config_and_inbox_models"),
    ]

    operations = [
        # WorkflowRun indexes
        migrations.AddIndex(
            model_name="workflowrun",
            index=models.Index(
                fields=["execution_backend", "status"],
                name="ace_workflo_executi_b9c8a1_idx",
            ),
        ),
        # ActivityRun indexes
        migrations.AddIndex(
            model_name="activityrun",
            index=models.Index(
                fields=["queue", "partition_key", "status"],
                name="ace_activit_queue_p_d4f8c2_idx",
            ),
        ),
        # ActivityAttempt indexes
        migrations.AddIndex(
            model_name="activityattempt",
            index=models.Index(
                fields=["status", "start_to_close_deadline"],
                name="ace_activit_status__e3a7f5_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="activityattempt",
            index=models.Index(
                fields=["status", "heartbeat_deadline"],
                name="ace_activit_status__h8b2c9_idx",
            ),
        ),
        # WorkflowTimer indexes
        migrations.AddIndex(
            model_name="workflowtimer",
            index=models.Index(
                fields=["status", "next_attempt_at"],
                name="ace_workflo_status__n7d4e1_idx",
            ),
        ),
        # AceWorkerHeartbeat indexes
        migrations.AddIndex(
            model_name="aceworkerheartbeat",
            index=models.Index(
                fields=["role", "status", "last_seen_at"],
                name="ace_worker_role_st_a5c2b3_idx",
            ),
        ),
        # WorkflowInboxEvent constraints
        migrations.AddConstraint(
            model_name="workflowinboxevent",
            constraint=models.UniqueConstraint(
                fields=("workflow_run", "inbox_sequence"),
                name="ace_inbox_sequence_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowinboxevent",
            constraint=models.UniqueConstraint(
                fields=("workflow_run", "source_type", "source_key"),
                name="ace_inbox_source_unique",
            ),
        ),
        # WorkflowInboxEvent indexes
        migrations.AddIndex(
            model_name="workflowinboxevent",
            index=models.Index(
                fields=["workflow_run", "status", "inbox_sequence"],
                name="ace_inbox_run_status_seq_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="workflowinboxevent",
            index=models.Index(
                fields=["status", "available_at"],
                name="ace_inbox_status_avail_idx",
            ),
        ),
    ]
