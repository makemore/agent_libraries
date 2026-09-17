"""Django runtime configuration for ACE workers and dispatchers.

DjangoRuntime replaces WorkerRuntime for 1.0, providing:
- Activity workers no longer need a workflow engine (inbox handles transitions)
- Workflow/activity version capabilities for deployment routing
- Backend identification for run authority
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ace import ActivityRegistry, WorkflowRegistry


@dataclass(frozen=True)
class DjangoRuntime:
    """Runtime configuration for Django ACE workers and services.

    Unlike the deprecated WorkerRuntime, activity workers no longer need
    a workflow engine reference. Activity completion enqueues to the
    durable inbox, and the dispatcher processes transitions.

    Attributes:
        activities: Registry of activity definitions this worker can execute.
        workflows: Registry of workflow definitions (for dispatcher only).
        deployment_id: Unique identifier for this deployment (for version routing).
        backend: Execution backend name (always "django" for this runtime).
        workflow_capabilities: Set of (name, version) pairs this deployment supports.
        activity_capabilities: Set of (name, version) pairs this worker supports.
    """

    activities: ActivityRegistry
    workflows: WorkflowRegistry | None = None
    deployment_id: str | None = None
    backend: str = "django"

    @property
    def workflow_capabilities(self) -> frozenset[tuple[str, str]]:
        """Get workflow (name, version) pairs this deployment can dispatch."""
        if self.workflows is None:
            return frozenset()
        return self.workflows.identities()

    @property
    def activity_capabilities(self) -> frozenset[tuple[str, str]]:
        """Get activity (name, version) pairs this worker can execute."""
        return self.activities.identities()


# Keep WorkerRuntime as a deprecated alias for backward compatibility
@dataclass(frozen=True)
class WorkerRuntime:
    """Deprecated: Use DjangoRuntime instead.

    This class is kept for backward compatibility during the 1.0 transition.
    Activity workers using this class will continue to work, but the
    workflow_engine field is no longer used for inbox-based completion.
    """

    activities: ActivityRegistry
    workflow_engine: object | None = None  # Ignored in 1.0

    def __post_init__(self) -> None:
        import warnings

        warnings.warn(
            "WorkerRuntime is deprecated. Use DjangoRuntime instead.",
            DeprecationWarning,
            stacklevel=2,
        )


def load_runtime() -> DjangoRuntime:
    """Load runtime configuration from Django settings.

    Expects ACE_RUNTIME_FACTORY to be a dotted path to a callable
    that returns a DjangoRuntime instance.
    """
    from typing import cast
    from collections.abc import Callable

    from django.conf import settings
    from django.utils.module_loading import import_string

    from ace_django.exceptions import WorkerConfigurationError

    path = getattr(settings, "ACE_RUNTIME_FACTORY", None)
    if not isinstance(path, str) or not path.strip():
        raise WorkerConfigurationError(
            "ACE_RUNTIME_FACTORY must be a dotted path to a callable returning DjangoRuntime."
        )
    factory = cast(Callable[[], object], import_string(path))
    runtime = factory()

    from ace_django.worker import WorkerRuntime as LegacyWorkerRuntime

    # Accept both DjangoRuntime and legacy WorkerRuntime variants
    if isinstance(runtime, DjangoRuntime):
        return runtime
    if isinstance(runtime, (WorkerRuntime, LegacyWorkerRuntime)):
        # Upgrade legacy runtime
        return DjangoRuntime(
            activities=runtime.activities,
            workflows=None,
            deployment_id=None,
            backend="django",
        )
    raise WorkerConfigurationError(
        f"ACE runtime factory {path!r} returned {type(runtime).__name__}, not DjangoRuntime."
    )
