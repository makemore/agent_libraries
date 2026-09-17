"""Standalone settings for ACE Django adapter tests."""

import os

SECRET_KEY = "ace-django-test-key"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = ["django.contrib.contenttypes", "ace_django"]
ACE_RUNTIME_FACTORY = "ace_django.tests.worker_helpers.build_runtime"

if os.getenv("ACE_TEST_DATABASE") == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("ACE_DB_NAME", "ace_django"),
            "HOST": os.getenv("ACE_DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("ACE_DB_PORT", "5432"),
            "USER": os.getenv("ACE_DB_USER", ""),
            "PASSWORD": os.getenv("ACE_DB_PASSWORD", ""),
        }
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
