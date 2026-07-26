"""Settings for running the test suite locally (no Railway env, no network).

Usage:  python manage.py test --settings=core.test_settings
"""
import os

# core.settings reads these via decouple before anything else; provide safe
# defaults so tests run without a .env or Railway environment.
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('DATABASE_URL', 'sqlite:///test.sqlite3')
os.environ.setdefault('CLOUDINARY_CLOUD_NAME', 'test')
os.environ.setdefault('CLOUDINARY_API_KEY', 'test')
os.environ.setdefault('CLOUDINARY_API_SECRET', 'test')

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

# Keep file fields on local disk instead of Cloudinary.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'quest_proofs': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

# Speed up user creation.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Keep uploaded test files (e.g. quest proof photos) out of the repo.
import tempfile  # noqa: E402
MEDIA_ROOT = tempfile.mkdtemp(prefix='aura-test-media-')
