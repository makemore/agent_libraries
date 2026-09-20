"""Compatibility entry point for the shared, migration-enabled test settings.

python manage.py test engagement --settings=engagement.test_settings
"""

from importlib import import_module


_test_settings = import_module('raise.settings.test')
globals().update({name: value for name, value in vars(_test_settings).items() if name.isupper()})