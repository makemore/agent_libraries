"""Credential-free in-memory test host. Providers are in-process fakes; no network."""

SECRET_KEY = "isolated-channels-tests-not-a-host-secret"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes",
    "rest_framework", "django_agent_runtime",
    "django_agent_channels",
]
ROOT_URLCONF = "channels_tests.urls"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Named test configuration: fake providers (channels_tests.fakes) instead of real
# AgentMail/Cloudflare/Twilio. Real hosts configure the real classes and keys.
AGENT_CHANNELS = {
    "EMAIL_PROVIDER": "channels_tests.fakes.FakeEmail",
    "REGISTRAR_PROVIDER": "channels_tests.fakes.FakeRegistrar",
    "DNS_PROVIDER": "channels_tests.fakes.FakeDNS",
    "SMS_PROVIDER": "channels_tests.fakes.FakeSMS",
    "PUBLIC_BASE_URL": "https://studio.test",
}
