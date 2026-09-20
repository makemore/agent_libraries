"""Transport-independent authentication context and explicit profile grants."""

from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError

from accounts.models import ProfileGrant, User


@dataclass(frozen=True)
class Actor:
    # Construct user_id from authenticated transport state, NEVER a tool argument.
    user_id: int
    profile_id: UUID | str
    source: str = 'api'


def authorize(actor: Actor, *, write=False, admin=False):
    """Recheck the live grant on every operation, including receipt replay."""
    try:
        profile_id = UUID(str(actor.profile_id))
    except (ValueError, TypeError, AttributeError):
        raise PermissionDenied('Profile access denied.') from None
    grant = ProfileGrant.objects.select_related('profile__charity', 'user').filter(
        user_id=actor.user_id, user__is_active=True, profile_id=profile_id,
        is_active=True, profile__is_active=True, profile__charity__is_active=True,
    ).first()
    if grant is None:
        raise PermissionDenied('Profile access denied.')
    if admin and grant.role != ProfileGrant.Role.ADMIN:
        raise PermissionDenied('An admin profile grant is required.')
    if write and grant.role not in (ProfileGrant.Role.FUNDRAISER, ProfileGrant.Role.ADMIN):
        raise PermissionDenied('This profile grant is read-only.')
    return grant


def list_profiles(user_id):
    if not User.objects.filter(pk=user_id, is_active=True).exists():
        raise PermissionDenied('Authentication required.')
    return list(ProfileGrant.objects.filter(
        user_id=user_id, is_active=True, profile__is_active=True,
        profile__charity__is_active=True,
    ).order_by('profile__display_name', 'pk').values(
        'profile_id', 'profile__display_name', 'profile__charity_id',
        'profile__charity__name', 'role',
    ))


def page_bounds(limit, offset):
    if isinstance(limit, bool) or isinstance(offset, bool):
        raise ValidationError('Invalid pagination.')
    if not isinstance(limit, int) or not isinstance(offset, int) or not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise ValidationError('Limit must be 1–100 and offset 0–10000.')
    return limit, offset