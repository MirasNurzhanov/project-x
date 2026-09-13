"""Disposable PostgreSQL/Redis integration services; never a developer database.

The ordinary full suite uses core.test_settings. CI additionally runs database
concurrency and socket regressions against these explicitly supplied services.
"""
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from .test_settings import *  # noqa: F403,F401

DATABASES = {'default': dj_database_url.parse(os.environ['CI_DATABASE_URL'], conn_max_age=0)}
if DATABASES['default']['ENGINE'] != 'django.db.backends.postgresql':
    raise ImproperlyConfigured('CI integration tests require PostgreSQL, not a SQLite fallback.')
DATABASES['default']['TEST'] = {'NAME': 'test_aura_ci'}

CACHES = {'default': {
    'BACKEND': 'django.core.cache.backends.redis.RedisCache',
    'LOCATION': os.environ['CI_REDIS_URL'],
    'KEY_PREFIX': 'aura-ci',
}}
CHANNEL_LAYERS = {'default': {
    'BACKEND': 'channels_redis.core.RedisChannelLayer',
    'CONFIG': {'hosts': [os.environ['CI_REDIS_URL']], 'prefix': 'aura-ci'},
}}
