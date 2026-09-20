"""Routing is defense in depth; model/queryset guards also reject explicit aliases."""


class EngagementRouter:
    def db_for_read(self, model, **hints):
        return 'engagement' if model._meta.app_label == 'engagement' else 'default'

    def db_for_write(self, model, **hints):
        return self.db_for_read(model, **hints)

    def allow_relation(self, obj1, obj2, **hints):
        first = self.db_for_read(type(obj1))
        second = self.db_for_read(type(obj2))
        return (
            first == second
            and obj1._state.db in (None, first)
            and obj2._state.db in (None, second)
        )

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == ('engagement' if app_label == 'engagement' else 'default')