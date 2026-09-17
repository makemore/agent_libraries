"""Django and PostgreSQL persistence adapter for ACE.

Key classes (import from submodules):
- ace_django.store.DjangoExecutionStore: Primary workflow execution store
- ace_django.queue.DjangoActivityQueue: Activity claim/complete/fail queue
- ace_django.dispatcher.DjangoWorkflowDispatcher: Durable inbox event processor (1.0)
- ace_django.runtime.DjangoRuntime: Runtime configuration for workers (1.0)
"""

VERSION = (1, 0, 0)
__version__ = ".".join(str(part) for part in VERSION)

__all__ = [
    "__version__",
    "VERSION",
]


def __getattr__(name: str) -> object:
    """Lazy import of key classes to avoid Django setup issues.

    This allows `from ace_django import DjangoExecutionStore` to work
    after Django has been set up.
    """
    if name == "DjangoExecutionStore":
        from ace_django.store import DjangoExecutionStore

        return DjangoExecutionStore
    if name == "DjangoActivityQueue":
        from ace_django.queue import DjangoActivityQueue

        return DjangoActivityQueue
    if name == "DjangoWorkflowDispatcher":
        from ace_django.dispatcher import DjangoWorkflowDispatcher

        return DjangoWorkflowDispatcher
    if name == "DjangoRuntime":
        from ace_django.runtime import DjangoRuntime

        return DjangoRuntime
    raise AttributeError(f"module 'ace_django' has no attribute {name!r}")
