"""Durable ACE execution records owned by the Django adapter."""

from __future__ import annotations

import uuid

from ace import WorkflowStatus
from django.db import models
from django.db.models import Q

WORKFLOW_STATUS_CHOICES = tuple((status.value, status.value.title()) for status in WorkflowStatus)


class ActivityStatus(models.TextChoices):
    READY = "READY", "Ready"
    RUNNING = "RUNNING", "Running"
    RETRYING = "RETRYING", "Retrying"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


class AttemptStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    EXPIRED = "EXPIRED", "Expired"
    CANCELLED = "CANCELLED", "Cancelled"
    TIMED_OUT = "TIMED_OUT", "Timed Out"


class TimerStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", "Scheduled"
    FIRED = "FIRED", "Fired"  # Legacy alias - use DELIVERED for new code
    DELIVERED = "DELIVERED", "Delivered"
    RETRYING = "RETRYING", "Retrying"
    DEAD_LETTER = "DEAD_LETTER", "Dead Letter"
    CANCELLED = "CANCELLED", "Cancelled"


class ActivityGroupStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


class WorkerStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    DRAINING = "DRAINING", "Draining"
    STOPPED = "STOPPED", "Stopped"


class WorkerRole(models.TextChoices):
    """Role of a worker process."""

    ACTIVITY = "ACTIVITY", "Activity Worker"
    DISPATCHER = "DISPATCHER", "Workflow Dispatcher"
    TIMER = "TIMER", "Timer Service"
    DEADLINE = "DEADLINE", "Deadline Service"


class InboxStatus(models.TextChoices):
    """Status of a workflow inbox event."""

    PENDING = "PENDING", "Pending"
    RETRYING = "RETRYING", "Retrying"
    PROCESSED = "PROCESSED", "Processed"
    DISCARDED = "DISCARDED", "Discarded"
    DEAD_LETTER = "DEAD_LETTER", "Dead Letter"


class ActivityExecutionMode(models.TextChoices):
    """Execution mode for activities."""

    STANDARD = "STANDARD", "Standard"
    TRANSACTIONAL = "TRANSACTIONAL", "Transactional"


class AceTimestampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at"]


class WorkflowRun(AceTimestampedModel):
    namespace = models.CharField(max_length=100, default="default")
    workflow_name = models.CharField(max_length=255)
    workflow_version = models.CharField(max_length=100)
    status = models.CharField(
        max_length=20,
        choices=WORKFLOW_STATUS_CHOICES,
        default=WorkflowStatus.PENDING.value,
    )
    input = models.JSONField(default=dict)
    state = models.JSONField(default=dict)
    result = models.JSONField(null=True, blank=True)
    failure = models.JSONField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    correlation_id = models.CharField(max_length=255, null=True, blank=True)
    last_event_sequence = models.PositiveBigIntegerField(default=0)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # 1.0 additions
    execution_backend = models.CharField(max_length=32, default="django")
    last_inbox_sequence = models.PositiveBigIntegerField(default=0)
    deadline_at = models.DateTimeField(null=True, blank=True)
    blocked_at = models.DateTimeField(null=True, blank=True)
    blocked_from_status = models.CharField(max_length=20, null=True, blank=True)
    block_reason = models.TextField(null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_workflow_runs"
        constraints = [
            models.UniqueConstraint(
                fields=["namespace", "workflow_name", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="ace_workflow_idempotency_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["workflow_name", "workflow_version"]),
            models.Index(fields=["execution_backend", "status"]),
        ]


class ActivityGroupRun(AceTimestampedModel):
    """A group of activities that must all succeed (ALL_SUCCESS) or fail fast."""

    workflow_run = models.ForeignKey(
        WorkflowRun,
        on_delete=models.CASCADE,
        related_name="activity_groups",
    )
    group_key = models.CharField(max_length=255)
    completion_policy = models.CharField(max_length=50, default="ALL_SUCCESS")
    status = models.CharField(
        max_length=20,
        choices=ActivityGroupStatus.choices,
        default=ActivityGroupStatus.RUNNING,
    )
    result = models.JSONField(null=True, blank=True)
    failure = models.JSONField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_activity_group_runs"
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_run", "group_key"],
                name="ace_workflow_group_key_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["workflow_run", "status"]),
        ]


class ActivityRun(AceTimestampedModel):
    workflow_run = models.ForeignKey(
        WorkflowRun,
        on_delete=models.CASCADE,
        related_name="activities",
        null=True,
        blank=True,
    )
    group = models.ForeignKey(
        ActivityGroupRun,
        on_delete=models.CASCADE,
        related_name="members",
        null=True,
        blank=True,
    )
    namespace = models.CharField(max_length=100, default="default")
    activity_key = models.CharField(max_length=255)
    activity_name = models.CharField(max_length=255)
    activity_version = models.CharField(max_length=100, default="1")
    queue = models.CharField(max_length=100, default="medium")
    priority = models.IntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=ActivityStatus.choices,
        default=ActivityStatus.READY,
    )
    input = models.JSONField(default=dict)
    result = models.JSONField(null=True, blank=True)
    failure = models.JSONField(null=True, blank=True)
    retry_policy = models.JSONField(default=dict)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    available_at = models.DateTimeField(null=True, blank=True, db_index=True)
    current_attempt = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # 1.0 additions
    execution_mode = models.CharField(
        max_length=20,
        choices=ActivityExecutionMode.choices,
        default=ActivityExecutionMode.STANDARD,
    )
    partition_key = models.CharField(max_length=255, null=True, blank=True)
    schedule_to_close_seconds = models.FloatField(null=True, blank=True)
    start_to_close_seconds = models.FloatField(null=True, blank=True)
    heartbeat_seconds = models.FloatField(null=True, blank=True)
    schedule_to_close_at = models.DateTimeField(null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_activity_runs"
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_run", "activity_key"],
                condition=Q(workflow_run__isnull=False),
                name="ace_workflow_activity_key_unique",
            ),
            models.UniqueConstraint(
                fields=["namespace", "activity_name", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="ace_activity_idempotency_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "queue", "available_at", "priority"]),
            models.Index(fields=["queue", "partition_key", "status"]),
        ]


class ActivityAttempt(AceTimestampedModel):
    activity_run = models.ForeignKey(
        ActivityRun,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    attempt_number = models.PositiveIntegerField()
    status = models.CharField(
        max_length=20,
        choices=AttemptStatus.choices,
        default=AttemptStatus.RUNNING,
    )
    worker_id = models.CharField(max_length=255)
    ownership_token = models.UUIDField(default=uuid.uuid4, editable=False)
    lease_expires_at = models.DateTimeField(db_index=True)
    heartbeat_at = models.DateTimeField()
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    error_type = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    details = models.JSONField(default=dict)
    # 1.0 additions
    start_to_close_deadline = models.DateTimeField(null=True, blank=True)
    heartbeat_deadline = models.DateTimeField(null=True, blank=True)
    timeout_cause = models.CharField(max_length=32, null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_activity_attempts"
        constraints = [
            models.UniqueConstraint(
                fields=["activity_run", "attempt_number"],
                name="ace_activity_attempt_number_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "lease_expires_at"]),
            models.Index(fields=["status", "start_to_close_deadline"]),
            models.Index(fields=["status", "heartbeat_deadline"]),
        ]


class WorkflowEvent(AceTimestampedModel):
    workflow_run = models.ForeignKey(
        WorkflowRun,
        on_delete=models.CASCADE,
        related_name="events",
    )
    sequence = models.PositiveBigIntegerField()
    event_type = models.CharField(max_length=255)
    payload = models.JSONField(default=dict)
    actor = models.CharField(max_length=255, null=True, blank=True)
    occurred_at = models.DateTimeField()
    # 1.0 addition: canonical emitted commands for replay verification
    # NULL means commands not available (legacy), empty list means no commands
    commands = models.JSONField(null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_workflow_events"
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_run", "sequence"],
                name="ace_workflow_event_sequence_unique",
            ),
        ]


class WorkflowTimer(AceTimestampedModel):
    workflow_run = models.ForeignKey(
        WorkflowRun,
        on_delete=models.CASCADE,
        related_name="timers",
    )
    timer_key = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=TimerStatus.choices,
        default=TimerStatus.SCHEDULED,
    )
    fire_at = models.DateTimeField(db_index=True)
    payload = models.JSONField(default=dict)
    fired_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # 1.0 additions for durable delivery
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_workflow_timers"
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_run", "timer_key"],
                name="ace_workflow_timer_key_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "fire_at"]),
            models.Index(fields=["status", "next_attempt_at"]),
        ]


class AceWorkerHeartbeat(AceTimestampedModel):
    worker_id = models.CharField(max_length=255, unique=True)
    process_id = models.PositiveIntegerField()
    hostname = models.CharField(max_length=255)
    queues = models.JSONField(default=list)
    status = models.CharField(
        max_length=20,
        choices=WorkerStatus.choices,
        default=WorkerStatus.ACTIVE,
    )
    last_seen_at = models.DateTimeField(db_index=True)
    # 1.0 additions
    role = models.CharField(
        max_length=20,
        choices=WorkerRole.choices,
        default=WorkerRole.ACTIVITY,
    )
    deployment_id = models.CharField(max_length=255, null=True, blank=True)
    backend = models.CharField(max_length=32, default="django")
    workflow_capabilities = models.JSONField(default=list)  # List of [name, version] pairs
    activity_capabilities = models.JSONField(default=list)  # List of [name, version] pairs

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_worker_heartbeats"
        indexes = [
            models.Index(fields=["status", "last_seen_at"]),
            models.Index(fields=["role", "status", "last_seen_at"]),
        ]


class QueueConfig(AceTimestampedModel):
    """Configuration for an activity queue."""

    name = models.CharField(max_length=100, unique=True)
    enabled = models.BooleanField(default=True)
    # Global concurrency limit (null = unlimited)
    global_concurrency = models.PositiveIntegerField(null=True, blank=True)
    # Fixed-window rate limit
    rate_limit_count = models.PositiveIntegerField(null=True, blank=True)
    rate_limit_period_seconds = models.PositiveIntegerField(null=True, blank=True)
    rate_limit_window_start = models.DateTimeField(null=True, blank=True)
    rate_limit_window_count = models.PositiveIntegerField(default=0)
    # Per-partition concurrency limit (null = unlimited)
    partition_concurrency = models.PositiveIntegerField(null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_queue_configs"


class WorkflowInboxEvent(AceTimestampedModel):
    """Durable inbox for workflow events awaiting dispatch."""

    workflow_run = models.ForeignKey(
        WorkflowRun,
        on_delete=models.CASCADE,
        related_name="inbox_events",
    )
    inbox_sequence = models.PositiveBigIntegerField()
    source_type = models.CharField(max_length=32)  # activity, group, timer, signal, deadline
    source_key = models.CharField(max_length=255)  # activity_key, timer_key, signal idempotency key
    event_type = models.CharField(max_length=255)
    payload = models.JSONField(default=dict)
    actor = models.CharField(max_length=255, null=True, blank=True)
    occurred_at = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=InboxStatus.choices,
        default=InboxStatus.PENDING,
    )
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    dead_letter_at = models.DateTimeField(null=True, blank=True)
    last_error_type = models.CharField(max_length=255, null=True, blank=True)
    last_error_message = models.TextField(null=True, blank=True)
    discard_reason = models.CharField(max_length=255, null=True, blank=True)

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_workflow_inbox_events"
        ordering = ["inbox_sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_run", "inbox_sequence"],
                name="ace_inbox_sequence_unique",
            ),
            models.UniqueConstraint(
                fields=["workflow_run", "source_type", "source_key"],
                name="ace_inbox_source_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["workflow_run", "status", "inbox_sequence"]),
            models.Index(fields=["status", "available_at"]),
        ]


class WorkflowTransitionFailure(AceTimestampedModel):
    """Append-only record of transition failures for diagnostics."""

    workflow_run = models.ForeignKey(
        WorkflowRun,
        on_delete=models.CASCADE,
        related_name="transition_failures",
    )
    inbox_event = models.ForeignKey(
        WorkflowInboxEvent,
        on_delete=models.CASCADE,
        related_name="failures",
        null=True,
        blank=True,
    )
    attempt = models.PositiveIntegerField()
    error_type = models.CharField(max_length=255)
    error_message = models.TextField()
    error_details = models.JSONField(default=dict)
    resolution = models.CharField(
        max_length=32, null=True, blank=True
    )  # retried, blocked, dead_letter

    class Meta(AceTimestampedModel.Meta):
        db_table = "ace_workflow_transition_failures"
        ordering = ["-created_at"]
