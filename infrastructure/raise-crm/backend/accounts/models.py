import uuid

from authtools.models import AbstractEmailUser
from django.conf import settings
from django.db import models


# Create your models here.


class User(AbstractEmailUser):
    """
    Custom User model using email as the identifier.
    Includes optional name fields used by tests and serializers.
    """
    class Kind(models.TextChoices):
        HUMAN = 'human', 'Human'
        AGENT = 'agent', 'Agent'

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.HUMAN)
    first_name = models.CharField('first name', max_length=150, blank=True)
    last_name = models.CharField('last name', max_length=150, blank=True)
    full_name = models.CharField('full name', max_length=255, blank=True)
    preferred_name = models.CharField('preferred name', max_length=255, blank=True)

    def get_full_name(self):
        """
        Return the user's full name.
        """
        return self.full_name.strip() if self.full_name else ''

    def get_short_name(self):
        """
        Return the user's preferred name or first part of full name.
        """
        if self.preferred_name:
            return self.preferred_name.strip()
        elif self.full_name:
            return self.full_name.split()[0] if self.full_name.split() else ''
        return ''

    def __str__(self):
        """
        String representation of the user.
        """
        return self.email

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'


class Profile(models.Model):
    """A charity-scoped acting identity, not a login or a permission grant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    charity = models.ForeignKey('crm.Charity', on_delete=models.PROTECT, related_name='profiles')
    display_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['charity', 'is_active'])]

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name


class ProfileGrant(models.Model):
    """Explicit caller access to a profile; superuser status is not a grant."""

    class Role(models.TextChoices):
        VIEWER = 'viewer', 'Viewer'
        FUNDRAISER = 'fundraiser', 'Fundraiser'
        ADMIN = 'admin', 'Admin'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile_grants')
    profile = models.ForeignKey(Profile, on_delete=models.CASCADE, related_name='grants')
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'profile'], name='accounts_unique_profile_grant'),
        ]
        indexes = [models.Index(fields=['user', 'is_active'])]

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
