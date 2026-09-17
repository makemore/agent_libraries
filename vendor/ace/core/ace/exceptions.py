"""Named exceptions raised by ACE core."""


class AceError(Exception):
    """Base class for ACE failures."""


class DuplicateDefinition(AceError):
    """A registry already contains the requested name and version."""


class DefinitionNotFound(AceError):
    """A requested workflow or activity definition is unavailable."""


class InvalidDefinition(AceError):
    """A workflow or activity definition violates the public contract."""


class InvalidPolicy(AceError):
    """An execution policy contains an invalid value."""


class InvalidAttempt(AceError):
    """An activity attempt number is outside the supported range."""


class InvalidTransition(AceError):
    """A workflow transition is internally inconsistent."""


class InvalidClock(AceError):
    """A clock returned a timestamp ACE cannot persist safely."""


class RunNotFound(AceError):
    """A workflow run does not exist in the execution store."""


class ConcurrentTransition(AceError):
    """A workflow changed after the caller loaded its snapshot."""


class IdempotencyConflict(AceError):
    """An idempotency key was reused for a different logical operation."""


class ActivityCancelled(AceError):
    """An activity observed a cooperative cancellation request."""
