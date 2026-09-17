"""Read-only Django admin registrations for durable ACE execution history."""

from django.contrib import admin

from ace_django.models import (
    AceWorkerHeartbeat,
    ActivityAttempt,
    ActivityGroupRun,
    ActivityRun,
    WorkflowEvent,
    WorkflowRun,
    WorkflowTimer,
)


class ReadOnlyAceAdmin(admin.ModelAdmin):
    """Expose ACE records without permitting history mutation."""

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(WorkflowRun)
class WorkflowRunAdmin(ReadOnlyAceAdmin):
    list_display = ("id", "workflow_name", "workflow_version", "status", "created_at")
    list_filter = ("status", "namespace", "workflow_name")
    search_fields = ("id", "idempotency_key", "correlation_id")


@admin.register(ActivityGroupRun)
class ActivityGroupRunAdmin(ReadOnlyAceAdmin):
    list_display = (
        "id",
        "workflow_run",
        "group_key",
        "status",
        "completion_policy",
        "completed_at",
    )
    list_filter = ("status", "completion_policy")
    search_fields = ("id", "group_key", "workflow_run__id")


@admin.register(ActivityRun)
class ActivityRunAdmin(ReadOnlyAceAdmin):
    list_display = ("id", "activity_name", "queue", "status", "group", "available_at")
    list_filter = ("status", "queue", "activity_name")
    search_fields = ("id", "activity_key", "idempotency_key", "workflow_run__id", "group__id")


@admin.register(ActivityAttempt)
class ActivityAttemptAdmin(ReadOnlyAceAdmin):
    list_display = ("id", "activity_run", "attempt_number", "status", "worker_id")
    list_filter = ("status",)
    search_fields = ("id", "activity_run__id", "worker_id", "ownership_token")


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(ReadOnlyAceAdmin):
    list_display = ("id", "workflow_run", "sequence", "event_type", "occurred_at")
    list_filter = ("event_type",)
    search_fields = ("id", "workflow_run__id", "actor")


@admin.register(WorkflowTimer)
class WorkflowTimerAdmin(ReadOnlyAceAdmin):
    list_display = ("id", "workflow_run", "timer_key", "status", "fire_at")
    list_filter = ("status",)
    search_fields = ("id", "workflow_run__id", "timer_key")


@admin.register(AceWorkerHeartbeat)
class AceWorkerHeartbeatAdmin(ReadOnlyAceAdmin):
    list_display = ("worker_id", "hostname", "process_id", "status", "last_seen_at")
    list_filter = ("status", "hostname")
    search_fields = ("worker_id", "hostname")
