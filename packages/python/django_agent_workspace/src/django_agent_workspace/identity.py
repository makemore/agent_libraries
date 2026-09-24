"""Workspace view of the shared runtime identity. The workspace owns no identity table.

Members, authors and recipients are canonical principal strings from
``agent_runtime_core.identity``: ``agent:<AgentDefinition UUID>`` or
``user:<pk>``. When ``django_agent_runtime`` is installed, it decides whether a
principal is active and what it is called, so an agent has the same identity
here as in MCP grants, SDLC and channels. Without the runtime app only users
resolve; agent principals are treated as inactive (fail closed).
"""

from agent_runtime_core.identity import AGENT, USER, InvalidPrincipal, Principal
from django.apps import apps
from django.contrib.auth import get_user_model

from .errors import InvalidRequest, PermissionDenied

MEMBER_KINDS = (AGENT, USER)


def _runtime():
    if apps.is_installed("django_agent_runtime"):
        from django_agent_runtime import identity

        return identity
    return None


def parse(value, *, field="principal"):
    """Canonical key for a member principal (agent or user). Raises ``InvalidRequest``."""
    try:
        principal = value if isinstance(value, Principal) else Principal.parse(value)
    except InvalidPrincipal:
        raise InvalidRequest(fields={field: "Use agent:<id> or user:<id>."}) from None
    if principal.kind not in MEMBER_KINDS:
        raise InvalidRequest(fields={field: "Use agent:<id> or user:<id>."})
    return principal.key


def is_agent(key):
    return isinstance(key, str) and key.startswith(f"{AGENT}:")


def is_active(key):
    runtime = _runtime()
    if runtime is not None:
        return runtime.is_active_principal(key)
    if not isinstance(key, str) or not key.startswith(f"{USER}:"):
        return False
    return get_user_model()._default_manager.filter(pk=key[len(USER) + 1:], is_active=True).exists()


def display_name(key):
    runtime = _runtime()
    if runtime is not None:
        return runtime.display_name(key)
    if not is_active(key):
        return ""
    user = get_user_model()._default_manager.get(pk=key[len(USER) + 1:])
    return user.get_username()


def require_active(actor):
    """Return the actor's canonical key if it is a currently active agent or user."""
    try:
        key = parse(actor)
    except InvalidRequest:
        raise PermissionDenied() from None
    if not is_active(key):
        raise PermissionDenied()
    return key


def for_user(user):
    """Key for an authenticated, active user (e.g. a request's user)."""
    if user is None or not getattr(user, "is_authenticated", False):
        raise PermissionDenied()
    return require_active(f"{USER}:{user.pk}")


def agent_id(key):
    return key[len(AGENT) + 1:] if is_agent(key) else None
