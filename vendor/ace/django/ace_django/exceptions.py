"""Named failures raised by the Django execution adapter."""

from ace.exceptions import AceError


class AceDjangoError(AceError):
    """Base class for Django adapter failures."""


class InvalidIdentifier(AceDjangoError):
    """A core execution identifier is not a valid UUID."""


class LeaseOwnershipLost(AceDjangoError):
    """An activity attempt no longer owns the current execution lease."""


class QueueConfigurationError(AceDjangoError):
    """The activity queue lacks a service required for an operation."""


class InvalidQueueState(AceDjangoError):
    """Persisted queue records violate an execution invariant."""


class WorkerConfigurationError(AceDjangoError):
    """An ACE worker or runtime factory is configured incorrectly."""


class WorkerProcessExited(AceDjangoError):
    """A supervised ACE worker process exited unexpectedly."""


class ImmutableHistoryError(AceDjangoError):
    """An immutable workflow event was modified through the model API."""


class DuplicateInboxEvent(AceDjangoError):
    """An inbox event with the same source already exists with different payload."""


class TransitionFailure(AceDjangoError):
    """A workflow transition failed during dispatch."""


class WorkflowBlocked(AceDjangoError):
    """A workflow has been blocked due to excessive transition failures."""


class DispatcherError(AceDjangoError):
    """An error occurred during workflow dispatch."""
