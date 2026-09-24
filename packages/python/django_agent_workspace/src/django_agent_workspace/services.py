"""Core workspace services: membership, access, activity, idempotency.

All functions are keyword-only and use the ``default`` database. ``actor`` and
member arguments are canonical runtime principals (``agent:<uuid>`` /
``user:<pk>``; see ``identity``). Which principal a request or run acts as is
the host's job: a session user is ``identity.for_user(user)``; an agent run is
its own ``ctx.identity.agent`` (never the initiating user's access).
"""

import hashlib
import json

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.module_loading import import_string
from django.utils.text import slugify

from . import identity
from .errors import Conflict, InvalidRequest, NotFound, PermissionDenied
from .models import Activity, IdempotencyReceipt, Membership, Workspace

ROLE_RANK = {Membership.Role.VIEWER: 1, Membership.Role.MEMBER: 2, Membership.Role.MANAGER: 3}
MAX_KEY = 128


# --------------------------------------------------------------------------- identity

def _require_active(actor):
    return identity.require_active(actor)


def _can_add_agent(user_key, agent_key):
    path = getattr(settings, "WORKSPACE_CAN_ADD_AGENT", None)
    if not path:
        # No host policy configured: adding agents is blocked, not open.
        return False
    check = import_string(path) if isinstance(path, str) else path
    return check(user=user_key, agent=agent_key) is True


# --------------------------------------------------------------------------- access

def get_membership(*, actor, workspace_id, role=Membership.Role.VIEWER, lock=False):
    """Return the actor's membership if it has at least ``role``; else deny.

    Missing workspaces and non-members both raise ``NotFound`` so existence is
    not disclosed to outsiders.
    """
    actor = _require_active(actor)
    workspaces = Workspace.objects.select_for_update() if lock else Workspace.objects
    workspace = workspaces.filter(pk=workspace_id).first()
    if workspace is None:
        raise NotFound()
    membership = Membership.objects.filter(workspace=workspace, principal=actor).first()
    if membership is None:
        raise NotFound()
    if ROLE_RANK[membership.role] < ROLE_RANK[role]:
        raise PermissionDenied()
    membership.workspace = workspace
    membership.principal = actor
    return membership


def record(*, workspace, actor, verb, target=None, data=None):
    if isinstance(target, str):  # a principal key
        target_type, target_id = "principal", target
    elif target is not None:
        target_type, target_id = type(target).__name__.lower(), str(target.pk)
    else:
        target_type = target_id = ""
    Activity.objects.create(workspace=workspace, actor=actor, verb=verb,
                            target_type=target_type, target_id=target_id, data=data or {})


# --------------------------------------------------------------------------- idempotency

def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def check_receipt(*, actor, key, command, payload, model):
    """Return the earlier result for a replayed key, ``None`` for a new one.

    Must be called inside the command's transaction, after authorization.
    """
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY:
        raise InvalidRequest(fields={"idempotency_key": f"Required, at most {MAX_KEY} characters."})
    receipt = IdempotencyReceipt.objects.filter(actor=actor, key_digest=_digest(key)).first()
    if receipt is None:
        return None
    payload_digest = _digest(json.dumps(payload, sort_keys=True, default=str))
    if receipt.command != command or receipt.payload_digest != payload_digest:
        raise Conflict("Idempotency key was already used for a different request.")
    return model.objects.get(pk=receipt.result_id)


def store_receipt(*, actor, key, command, payload, result):
    try:
        with transaction.atomic():
            IdempotencyReceipt.objects.create(
                actor=actor, key_digest=_digest(key), command=command,
                payload_digest=_digest(json.dumps(payload, sort_keys=True, default=str)),
                result_type=type(result).__name__.lower(), result_id=str(result.pk),
            )
    except IntegrityError as exc:
        raise Conflict("Concurrent request with the same idempotency key.") from exc


# --------------------------------------------------------------------------- workspaces

def _clean_name(name, field="name", limit=200):
    name = (name or "").strip()
    if not name or len(name) > limit:
        raise InvalidRequest(fields={field: f"Required, at most {limit} characters."})
    return name


def _default_handle(principal):
    return (slugify(identity.display_name(principal)) or principal.split(":", 1)[0])[:30]


def create_workspace(*, actor, name, description="", idempotency_key):
    """Create a workspace; the creator becomes its manager."""
    actor = _require_active(actor)
    name = _clean_name(name)
    if len(description) > 5000:
        raise InvalidRequest(fields={"description": "At most 5000 characters."})
    payload = {"name": name, "description": description}
    with transaction.atomic():
        replay = check_receipt(actor=actor, key=idempotency_key, command="create_workspace",
                               payload=payload, model=Workspace)
        if replay is not None:
            return replay
        workspace = Workspace.objects.create(name=name, description=description, created_by=actor)
        Membership.objects.create(workspace=workspace, principal=actor,
                                  role=Membership.Role.MANAGER, handle=_default_handle(actor))
        record(workspace=workspace, actor=actor, verb="workspace.created", target=workspace)
        store_receipt(actor=actor, key=idempotency_key, command="create_workspace",
                      payload=payload, result=workspace)
    return workspace


def list_workspaces(*, actor):
    actor = _require_active(actor)
    return list(Workspace.objects.filter(memberships__principal=actor).distinct().order_by("name"))


def list_members(*, actor, workspace_id):
    membership = get_membership(actor=actor, workspace_id=workspace_id)
    return list(Membership.objects.filter(workspace=membership.workspace).order_by("handle"))


def set_member(*, actor, workspace_id, principal, role, handle=None, expected_revision):
    """Add, change or remove (``role=None``) a member. Managers only.

    ``principal`` is ``agent:<uuid>`` or ``user:<pk>``. Adding an agent also
    requires the host callback ``WORKSPACE_CAN_ADD_AGENT(user=, agent=)`` to
    return exactly ``True`` for the acting manager (e.g. they own or may use
    that agent); unset blocks adding agents. Owning an agent grants it nothing.
    Uses the workspace revision for optimistic concurrency. The last manager
    cannot be removed or demoted.
    """
    if role is not None and role not in ROLE_RANK:
        raise InvalidRequest(fields={"role": "Unknown role."})
    target = identity.parse(principal)
    with transaction.atomic():
        membership = get_membership(actor=actor, workspace_id=workspace_id,
                                    role=Membership.Role.MANAGER, lock=True)
        workspace = membership.workspace
        if workspace.revision != expected_revision:
            raise Conflict("Workspace changed; reload and retry.")
        current = Membership.objects.filter(workspace=workspace, principal=target).first()
        if role is not None and not identity.is_active(target):
            raise NotFound()
        if (role is not None and current is None and identity.is_agent(target)
                and not _can_add_agent(membership.principal, target)):
            raise PermissionDenied()
        losing_manager = (current is not None and current.role == Membership.Role.MANAGER
                          and role != Membership.Role.MANAGER)
        if losing_manager and not Membership.objects.filter(
                workspace=workspace, role=Membership.Role.MANAGER).exclude(pk=current.pk).exists():
            raise Conflict("A workspace needs at least one manager.")
        if role is None:
            if current is not None:
                current.delete()
                from .models import ChannelMember
                ChannelMember.objects.filter(channel__workspace=workspace, principal=target).delete()
            verb = "member.removed"
        else:
            handle = slugify(handle or "")[:40] or _default_handle(target)
            try:
                with transaction.atomic():
                    if current is None:
                        Membership.objects.create(workspace=workspace, principal=target,
                                                  role=role, handle=handle)
                    else:
                        current.role = role
                        current.handle = handle
                        current.save(update_fields=["role", "handle"])
            except IntegrityError as exc:
                raise Conflict("Handle is already taken in this workspace.") from exc
            verb = "member.set"
        workspace.revision += 1
        workspace.save(update_fields=["revision", "updated_at"])
        record(workspace=workspace, actor=membership.principal, verb=verb, target=target,
               data={"role": role or ""})
    return workspace


def set_paused(*, actor, workspace_id, paused):
    """Emergency stop: while paused, agent principals cannot write. Managers only."""
    with transaction.atomic():
        membership = get_membership(actor=actor, workspace_id=workspace_id,
                                    role=Membership.Role.MANAGER, lock=True)
        workspace = membership.workspace
        workspace.is_paused = bool(paused)
        workspace.save(update_fields=["is_paused", "updated_at"])
        record(workspace=workspace, actor=membership.principal,
               verb="workspace.paused" if paused else "workspace.resumed", target=workspace)
    return workspace


def list_activity(*, actor, workspace_id, limit=50):
    membership = get_membership(actor=actor, workspace_id=workspace_id)
    limit = max(1, min(int(limit), 100))
    return list(Activity.objects.filter(workspace=membership.workspace)[:limit])
