"""Fail-closed ORM access, including explicit aliases and execution shortcuts.

These guards cover the public event ORM, not arbitrary SQL/cursors or tampering
with Django's query compiler. Database credentials/permissions remain the final
isolation boundary. Raw SQL and cross-database subqueries are unsupported;
materialize scalar IDs explicitly rather than embedding event queries in CRM.
Only services should be exposed to callers; ORM writes do not authorize actors.
"""

from importlib import import_module

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import NotSupportedError, models


ALIAS = 'engagement'
validate_engagement_databases = import_module('raise.settings.engagement').validate_engagement_databases


def require_engagement(using=None):
    if using is not None and using != ALIAS:
        raise ImproperlyConfigured('Engagement records may only use the engagement database alias.')
    validate_engagement_databases(
        settings.DATABASES, allow_sqlite=getattr(settings, 'ENGAGEMENT_ALLOW_SQLITE', False),
    )
    return ALIAS


class EngagementQuerySet(models.QuerySet):
    @property
    def db(self):
        return require_engagement(self._db)

    def using(self, alias):
        return super().using(require_engagement(alias))

    def _fetch_all(self):
        require_engagement(self._db)
        return super()._fetch_all()

    def iterator(self, chunk_size=None):
        require_engagement(self._db)
        return super().iterator(chunk_size=chunk_size)

    async def aiterator(self, chunk_size=2000):
        require_engagement(self._db)
        async for item in super().aiterator(chunk_size=chunk_size):
            yield item

    def count(self):
        require_engagement(self._db)
        return super().count()

    def exists(self):
        require_engagement(self._db)
        return super().exists()

    def aggregate(self, *args, **kwargs):
        require_engagement(self._db)
        return super().aggregate(*args, **kwargs)

    def update(self, **kwargs):
        require_engagement(self._db)
        return super().update(**kwargs)

    def _update(self, values):
        require_engagement(self._db)
        return super()._update(values)

    def delete(self):
        require_engagement(self._db)
        return super().delete()

    def _raw_delete(self, using):
        require_engagement(self._db)
        return super()._raw_delete(require_engagement(using))

    def _insert(self, objs, fields, returning_fields=None, raw=False, using=None,
                on_conflict=None, update_fields=None, unique_fields=None):
        require_engagement(self._db)
        return super()._insert(
            objs, fields, returning_fields=returning_fields, raw=raw, using=require_engagement(using),
            on_conflict=on_conflict, update_fields=update_fields, unique_fields=unique_fields,
        )

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False,
                    update_conflicts=False, update_fields=None, unique_fields=None):
        require_engagement(self._db)
        if ignore_conflicts or update_conflicts:
            raise NotSupportedError('Engagement conflicts must be resolved explicitly by services.')
        objs = list(objs)
        for obj in objs:
            require_engagement(obj._state.db)
            obj._state.db = ALIAS
            obj.full_clean(validate_unique=False, validate_constraints=False)
        return super().bulk_create(objs, batch_size=batch_size)

    def bulk_update(self, objs, fields, batch_size=None):
        require_engagement(self._db)
        objs = list(objs)
        for obj in objs:
            require_engagement(obj._state.db)
            obj._state.db = ALIAS
            obj.full_clean(validate_unique=False, validate_constraints=False)
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def raw(self, *args, **kwargs):
        require_engagement(self._db)
        raise NotSupportedError('Raw engagement queries are not supported.')

    def resolve_expression(self, *args, **kwargs):
        raise NotSupportedError('Engagement querysets cannot be embedded in database subqueries.')


class EngagementManager(models.Manager.from_queryset(EngagementQuerySet)):
    def db_manager(self, using=None, hints=None):
        return super().db_manager(using=require_engagement(using), hints=hints)

    @property
    def db(self):
        return require_engagement(self._db)


class EngagementModel(models.Model):
    objects = EngagementManager()

    class Meta:
        abstract = True
        base_manager_name = 'objects'
        default_manager_name = 'objects'

    def save(self, force_insert=False, force_update=False, using=None, update_fields=None):
        using = require_engagement(using)
        require_engagement(self._state.db)
        self._state.db = using
        self.full_clean()
        return super().save(
            force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields,
        )

    def save_base(self, raw=False, force_insert=False, force_update=False, using=None, update_fields=None):
        using = require_engagement(using)
        require_engagement(self._state.db)
        return super().save_base(
            raw=raw, force_insert=force_insert, force_update=force_update, using=using, update_fields=update_fields,
        )

    def delete(self, using=None, keep_parents=False):
        using = require_engagement(using)
        require_engagement(self._state.db)
        return super().delete(using=using, keep_parents=keep_parents)

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        using = require_engagement(using)
        require_engagement(self._state.db)
        if from_queryset is not None:
            require_engagement(from_queryset.db)
        return super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)