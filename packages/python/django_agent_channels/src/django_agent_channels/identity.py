"""Agent identity for channels: the shared runtime principal ``agent:<uuid>``.

Requires ``django_agent_runtime`` to resolve agents; without it every agent is
treated as inactive (fail closed).
"""

from agent_runtime_core.identity import AGENT, InvalidPrincipal, Principal
from django.apps import apps
from django.conf import settings
from django.utils.module_loading import import_string

from .errors import InvalidRequest, PermissionDenied


def _runtime():
    if apps.is_installed("django_agent_runtime"):
        from django_agent_runtime import identity

        return identity
    return None


def agent_key(value):
    try:
        principal = value if isinstance(value, Principal) else Principal.parse(value)
    except InvalidPrincipal:
        raise InvalidRequest(fields={"agent": "Use agent:<id>."}) from None
    if principal.kind != AGENT:
        raise InvalidRequest(fields={"agent": "Use agent:<id>."})
    return principal.key


def agent_definition(key):
    runtime = _runtime()
    return runtime.get_agent(key) if runtime is not None else None


def require_agent(value):
    """Canonical key of a currently active agent, else ``PermissionDenied``."""
    try:
        key = agent_key(value)
    except InvalidRequest:
        raise PermissionDenied() from None
    if agent_definition(key) is None:
        raise PermissionDenied()
    return key


def agent_uuid(key):
    return key.split(":", 1)[1]


def can_manage(user, key):
    """May this human configure the agent's channels (limits, capabilities)?

    Host override: ``AGENT_CHANNELS["CAN_MANAGE_AGENT"]`` callable/dotted path
    ``(user=, agent=)`` returning exactly True. Default: the agent's owner or a
    superuser.
    """
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    hook = getattr(settings, "AGENT_CHANNELS", {}).get("CAN_MANAGE_AGENT")
    if hook:
        check = import_string(hook) if isinstance(hook, str) else hook
        return check(user=user, agent=key) is True
    definition = agent_definition(key)
    return definition is not None and (definition.owner_id == user.pk or user.is_superuser)
