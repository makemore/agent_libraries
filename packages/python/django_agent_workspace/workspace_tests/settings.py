"""Credential-free in-memory test host, independent of any host database."""

SECRET_KEY = "isolated-workspace-tests-not-a-host-secret"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
# The runtime app supplies agent identity (AgentDefinition). Its settings are left
# at their defaults; no queue, worker or model provider is used by these tests.
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes",
    "rest_framework", "django_agent_runtime",
    "django_agent_workspace",
]
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# Named isolated-test optimization: no password login is tested with this hasher.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
