"""Typed errors. Messages are safe to show to agents and users (no secrets, no raw provider bodies)."""


class ChannelError(Exception):
    code = "error"
    status = 400

    def __init__(self, message="Request failed.", *, fields=None):
        super().__init__(message)
        self.message = message
        self.fields = dict(fields or {})


class InvalidRequest(ChannelError):
    code = "invalid"


class PermissionDenied(ChannelError):
    code = "permission_denied"
    status = 403


class NotFound(ChannelError):
    code = "not_found"
    status = 404


class Conflict(ChannelError):
    code = "conflict"
    status = 409


class SpendingLimitExceeded(ChannelError):
    code = "spending_limit"
    status = 402


class RateLimited(ChannelError):
    code = "rate_limited"
    status = 429


class NotConfigured(ChannelError):
    code = "not_configured"
    status = 503


class ProviderError(ChannelError):
    """A provider call failed. ``retriable`` says whether the same call may be retried."""

    code = "provider_error"
    status = 502

    def __init__(self, message="Provider request failed.", *, retriable=False, provider_code=""):
        super().__init__(message)
        self.retriable = retriable
        self.provider_code = provider_code
