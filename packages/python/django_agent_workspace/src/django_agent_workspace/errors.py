"""Typed service errors. Messages are generic and safe to show to callers."""


class WorkspaceError(Exception):
    code = "error"
    status = 400

    def __init__(self, message="Request failed.", *, fields=None):
        super().__init__(message)
        self.message = message
        self.fields = dict(fields or {})


class InvalidRequest(WorkspaceError):
    code = "invalid"
    status = 400


class PermissionDenied(WorkspaceError):
    code = "permission_denied"
    status = 403


class NotFound(WorkspaceError):
    code = "not_found"
    status = 404


class Conflict(WorkspaceError):
    code = "conflict"
    status = 409


class Blocked(WorkspaceError):
    """A safety guard refused the action (paused workspace, rate or loop limit)."""

    code = "blocked"
    status = 429
