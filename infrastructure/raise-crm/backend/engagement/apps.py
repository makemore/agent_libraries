from django.apps import AppConfig


class EngagementConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'engagement'

    def ready(self):
        from . import checks  # noqa: F401