"""Host-administrative provisioning, never an API authorization mechanism.

Access to these commands and the application's database is the authority. There
is no authenticated application caller: the target user is a grantee, NOT the
audit actor. Host/operator identity must be tracked by host administration logs.
"""

from contextlib import contextmanager

from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist, ValidationError
from django.core.management.base import CommandError
from django.db import DatabaseError

from accounts.models import User
from crm.models import AuditRecord


@contextmanager
def host_errors():
    """Keep database, validation and filesystem details out of command output."""
    try:
        yield
    except (
        DatabaseError, ObjectDoesNotExist, MultipleObjectsReturned, ValidationError,
        OSError, ValueError, TypeError, AttributeError,
    ):
        raise CommandError('Host administrative operation failed.') from None


def active_user(email):
    """Call inside the default transaction; never create users or passwords."""
    return User.objects.using('default').select_for_update().get(
        email__iexact=email, is_active=True,
    )


def host_audit(grant, action, entity):
    """References identify the grant without attributing its use to the grantee."""
    AuditRecord.objects.using('default').create(
        charity=grant.profile.charity, created_by=None, acting_profile=None,
        action=action, entity_type=entity._meta.label_lower, entity_id=entity.pk,
        changes={'profile': str(grant.profile_id), 'grant': str(grant.pk)},
    )