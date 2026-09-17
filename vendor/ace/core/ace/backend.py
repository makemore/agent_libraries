"""Backend capability declarations and adapter contracts.

This module defines the capabilities that different execution backends support
and provides contracts for adapter implementations. Each backend (Django, DBOS,
Temporal) declares which features it implements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ace.commands import ActivityExecutionMode
    from ace.json_types import JsonObject
    from ace.models import WorkflowSnapshot


class BackendName(StrEnum):
    """Supported execution backends."""

    DJANGO = "django"
    DBOS = "dbos"
    TEMPORAL = "temporal"


@dataclass(frozen=True)
class BackendCapabilities:
    """Declares what features a backend supports.

    Adapters validate emitted commands against these capabilities and reject
    unsupported operations rather than silently degrading.
    """

    name: BackendName

    # Inbox/queue features
    supports_inbox: bool = False
    supports_queue_qos: bool = False
    supports_queue_rate_limits: bool = False
    supports_queue_partitions: bool = False
    supports_activity_priority: bool = False
    supports_activity_delay: bool = False

    # Execution modes
    supports_transactional_activities: bool = False
    supported_execution_modes: frozenset[str] = field(
        default_factory=lambda: frozenset(["STANDARD"])
    )

    # Timeout features
    supports_schedule_to_close_timeout: bool = False
    supports_start_to_close_timeout: bool = False
    supports_heartbeat_timeout: bool = False
    supports_workflow_deadline: bool = False

    # Signal/timer features
    supports_durable_signals: bool = False
    supports_timers: bool = False

    # Operations features
    supports_replay_verification: bool = False
    supports_workflow_blocking: bool = False
    supports_dead_letter_queue: bool = False

    def validate_execution_mode(self, mode: str) -> None:
        """Raise if the execution mode is not supported."""
        from ace.exceptions import InvalidTransition

        if mode not in self.supported_execution_modes:
            raise InvalidTransition(
                f"Backend {self.name!r} does not support execution mode {mode!r}. "
                f"Supported: {sorted(self.supported_execution_modes)}"
            )


# Predefined capability sets for each backend
DJANGO_CAPABILITIES = BackendCapabilities(
    name=BackendName.DJANGO,
    supports_inbox=True,
    supports_queue_qos=True,
    supports_queue_rate_limits=True,
    supports_queue_partitions=True,
    supports_activity_priority=True,
    supports_activity_delay=True,
    supports_transactional_activities=True,
    supported_execution_modes=frozenset(["STANDARD", "TRANSACTIONAL"]),
    supports_schedule_to_close_timeout=True,
    supports_start_to_close_timeout=True,
    supports_heartbeat_timeout=True,
    supports_workflow_deadline=True,
    supports_durable_signals=True,
    supports_timers=True,
    supports_replay_verification=True,
    supports_workflow_blocking=True,
    supports_dead_letter_queue=True,
)

DBOS_CAPABILITIES = BackendCapabilities(
    name=BackendName.DBOS,
    supports_inbox=False,  # DBOS uses its own durability
    supports_queue_qos=False,
    supports_queue_rate_limits=False,
    supports_queue_partitions=False,
    supports_activity_priority=False,
    supports_activity_delay=False,
    supports_transactional_activities=False,
    supported_execution_modes=frozenset(["STANDARD"]),
    supports_schedule_to_close_timeout=True,
    supports_start_to_close_timeout=True,
    supports_heartbeat_timeout=False,
    supports_workflow_deadline=False,
    supports_durable_signals=True,
    supports_timers=True,
    supports_replay_verification=False,
    supports_workflow_blocking=False,
    supports_dead_letter_queue=False,
)

TEMPORAL_CAPABILITIES = BackendCapabilities(
    name=BackendName.TEMPORAL,
    supports_inbox=False,  # Temporal uses its own durability
    supports_queue_qos=False,
    supports_queue_rate_limits=False,
    supports_queue_partitions=False,
    supports_activity_priority=False,
    supports_activity_delay=False,
    supports_transactional_activities=False,
    supported_execution_modes=frozenset(["STANDARD"]),
    supports_schedule_to_close_timeout=True,
    supports_start_to_close_timeout=True,
    supports_heartbeat_timeout=True,
    supports_workflow_deadline=True,
    supports_durable_signals=True,
    supports_timers=True,
    supports_replay_verification=False,
    supports_workflow_blocking=False,
    supports_dead_letter_queue=False,
)


def get_capabilities(backend: BackendName | str) -> BackendCapabilities:
    """Get capabilities for a backend by name."""
    if isinstance(backend, str):
        backend = BackendName(backend)
    return {
        BackendName.DJANGO: DJANGO_CAPABILITIES,
        BackendName.DBOS: DBOS_CAPABILITIES,
        BackendName.TEMPORAL: TEMPORAL_CAPABILITIES,
    }[backend]
