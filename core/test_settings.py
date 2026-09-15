"""Settings for running the test suite locally (no Railway env, no network).

Usage:  python manage.py test --settings=core.test_settings
"""
import os

# core.settings reads these via decouple before anything else; provide safe
# defaults so tests run without a .env or Railway environment.
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('DATABASE_URL', 'sqlite:///test.sqlite3')
os.environ.setdefault('R2_ACCESS_KEY_ID', 'test')
os.environ.setdefault('R2_SECRET_ACCESS_KEY', 'test')
os.environ.setdefault('R2_BUCKET_NAME', 'test')
os.environ.setdefault('R2_ENDPOINT_URL', 'https://test.example.com')
os.environ.setdefault('R2_PUBLIC_URL', 'https://test.example.com')

from .settings import *  # noqa: E402,F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# No Redis in tests; group_send calls in views need a working layer.
CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}

# Likewise for the cache: settings.py points this at Redis whenever REDIS_URL
# is set, and a developer with one running locally must not have the test suite
# reach for it.
CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}

# Keep file fields on local disk instead of R2.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

# Speed up user creation.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Keep uploaded test files (e.g. story photos) out of the repo.
import tempfile  # noqa: E402
MEDIA_ROOT = tempfile.mkdtemp(prefix='aura-test-media-')
STORAGES['private_media'] = {
    'BACKEND': 'django.core.files.storage.FileSystemStorage',
    'OPTIONS': {'location': tempfile.mkdtemp(prefix='aura-test-private-media-')},
}

# No push to Expo from a test run. notifications.push honours this by building
# the message list, returning its length, and stopping short of the thread that
# would send it — so what gets decided is still fully asserted, and nothing
# leaves the machine.
PUSH_DISABLED = True
