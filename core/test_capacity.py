"""M20: the web worker count is configurable, Redis-backed and budgeted against the database."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import skipUnless

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from core import capacity
from core.test_security import TRUSTED_PROXIES, _environment, import_asgi

BASE_DIR = Path(__file__).resolve().parent.parent

_PROBE = (
    'import json, decouple\n'
    'decouple.config = decouple.Config(decouple.RepositoryEmpty())\n'
    'import core.settings as s\n'
    'print(json.dumps({"channels": s.CHANNEL_LAYERS, "caches": s.CACHES, "reserve": s.DB_CONNECTION_RESERVE,'
    ' "max_connections": s.DB_MAX_CONNECTIONS, "pool_max": s.DB_POOL_MAX_SIZE}))\n'
)


def production_settings(**overrides):
    result = subprocess.run(
        [sys.executable, '-c', _PROBE], cwd=BASE_DIR, env=_environment(overrides),
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class WorkerCountTests(SimpleTestCase):
    def test_worker_count_comes_from_web_concurrency(self):
        self.assertEqual(capacity.web_workers({}), capacity.DEFAULT_WEB_WORKERS)
        self.assertEqual(capacity.web_workers({'WEB_CONCURRENCY': ' '}), capacity.DEFAULT_WEB_WORKERS)
        self.assertEqual(capacity.web_workers({'WEB_CONCURRENCY': '4'}), 4)
        for raw in ('0', '-1', 'two', '1.5'):
            with self.subTest(raw=raw), self.assertRaises(ImproperlyConfigured):
                capacity.web_workers({'WEB_CONCURRENCY': raw})

    def test_procfile_starts_that_many_workers(self):
        web = next(line for line in (BASE_DIR / 'Procfile').read_text().splitlines() if line.startswith('web:'))
        self.assertIn(f'--workers "${{WEB_CONCURRENCY:-{capacity.DEFAULT_WEB_WORKERS}}}"', web)
        # With several workers uvicorn restarts a child that fails to import the app instead of exiting,
        # so the startup guards run once, in the foreground, before it starts.
        self.assertTrue(web.startswith('web: python -c "import core.asgi" && uvicorn '))

    @skipUnless(shutil.which('sh'), 'needs a POSIX shell to expand the Procfile command')
    def test_procfile_expands_the_worker_count(self):
        web = next(line for line in (BASE_DIR / 'Procfile').read_text().splitlines() if line.startswith('web:'))
        uvicorn = web.split('&&')[-1].strip()
        for env, expected in (({}, str(capacity.DEFAULT_WEB_WORKERS)), ({'WEB_CONCURRENCY': '3'}, '3')):
            with self.subTest(env=env):
                shell_env = {k: v for k, v in _environment({}).items() if k.upper() != 'WEB_CONCURRENCY'}
                shell_env.update(env, PORT='8000', FORWARDED_ALLOW_IPS=TRUSTED_PROXIES)
                result = subprocess.run(
                    [shutil.which('sh'), '-c', "printf '%s\\n' " + uvicorn[len('uvicorn '):]],
                    env=shell_env, capture_output=True, text=True, timeout=30,
                )
                args = result.stdout.splitlines()
                self.assertEqual(args[args.index('--workers') + 1], expected)


class ConnectionBudgetTests(SimpleTestCase):
    def check(self, **overrides):
        options = dict(debug=False, workers=2, redis_url='redis://redis:6379', pool_enabled=True,
                       pool_max_size=12, reserve=20, max_connections=100)
        options.update(overrides)
        capacity.require_worker_capacity(**options)

    def test_budget_arithmetic(self):
        self.assertEqual(capacity.connection_budget(2, 12, 20), 44)
        self.assertEqual(capacity.connection_budget(4, 12, 20), 68)

    def test_multiple_workers_require_redis(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'REDIS_URL'):
            self.check(redis_url='')
        self.check(redis_url='', workers=1)
        self.check(redis_url='', debug=True)

    def test_pools_must_fit_the_declared_database_limit(self):
        self.check(max_connections=44)
        with self.assertRaisesMessage(ImproperlyConfigured, '= 45 connections, more than DB_MAX_CONNECTIONS 44'):
            self.check(max_connections=44, reserve=21)
        self.check(max_connections=0, workers=50)
        self.check(pool_enabled=False, max_connections=10)


class ProductionCapacityTests(SimpleTestCase):
    def test_channel_layer_and_cache_use_redis_in_production(self):
        values = production_settings(REDIS_URL='redis://redis.internal:6379')
        channels = values['channels']['default']
        self.assertEqual(channels['BACKEND'], 'channels_redis.core.RedisChannelLayer')
        self.assertEqual(channels['CONFIG']['hosts'], ['redis://redis.internal:6379'])
        self.assertEqual(values['caches']['default']['BACKEND'], 'django.core.cache.backends.redis.RedisCache')
        self.assertEqual((values['pool_max'], values['reserve'], values['max_connections']), (12, 20, 0))

    def test_asgi_application_refuses_multiple_workers_without_redis_or_over_budget(self):
        base = {'FORWARDED_ALLOW_IPS': TRUSTED_PROXIES}
        no_redis = import_asgi(**base, WEB_CONCURRENCY='2')
        self.assertNotEqual(no_redis.returncode, 0)
        self.assertIn('REDIS_URL', no_redis.stderr)

        over_budget = import_asgi(**base, WEB_CONCURRENCY='2', REDIS_URL='redis://redis:6379', DB_MAX_CONNECTIONS='30')
        self.assertNotEqual(over_budget.returncode, 0)
        self.assertTrue(re.search(r'= 44 connections, more than DB_MAX_CONNECTIONS 30', over_budget.stderr))

        fits = import_asgi(**base, WEB_CONCURRENCY='2', REDIS_URL='redis://redis:6379', DB_MAX_CONNECTIONS='100')
        self.assertEqual(fits.returncode, 0, fits.stderr)
