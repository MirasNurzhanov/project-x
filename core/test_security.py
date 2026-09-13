"""CORS, CSRF and deployment security settings (codebase review findings C1 and M11)."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock, skipUnless

import uvicorn
from channels.routing import ProtocolTypeRouter
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
from django.core.asgi import get_asgi_application
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import request_finished, request_started
from django.db import close_old_connections
from django.test import Client, SimpleTestCase, TestCase, override_settings
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from core.asgi import require_trusted_proxies

BASE_DIR = Path(__file__).resolve().parent.parent

PROD_HOST = 'api.approx.test'
FRONTEND_ORIGIN = 'https://frontend.approx.test'
HOSTILE_ORIGINS = (
    'https://attacker.vercel.app',
    'https://attacker.up.railway.app',
    'https://evil.example',
    'null',
)
STAFF_PASSWORD = 'correct horse battery staple'

# RFC 5737 documentation ranges, standing in for the deployment's verified proxy range.
TRUSTED_PROXIES = '203.0.113.0/24'
TRUSTED_PROXY_PEER = '203.0.113.10'
UNTRUSTED_PEER = '198.51.100.20'

SETTING_NAMES = (
    'ALLOWED_HOSTS', 'CSRF_TRUSTED_ORIGINS',
    'CORS_URLS_REGEX', 'CORS_ALLOW_ALL_ORIGINS', 'CORS_ALLOW_CREDENTIALS', 'CORS_ALLOWED_ORIGINS',
    'SECURE_PROXY_SSL_HEADER', 'USE_X_FORWARDED_HOST',
    'SECURE_SSL_REDIRECT', 'SECURE_REDIRECT_EXEMPT',
    'SECURE_HSTS_SECONDS', 'SECURE_HSTS_INCLUDE_SUBDOMAINS', 'SECURE_HSTS_PRELOAD',
    'SECURE_REFERRER_POLICY',
    'SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE', 'SESSION_COOKIE_HTTPONLY',
    'SESSION_COOKIE_SAMESITE', 'CSRF_COOKIE_SAMESITE',
)
CORS_KEYS = (
    'CORS_URLS_REGEX', 'CORS_ALLOW_ALL_ORIGINS', 'CORS_ALLOW_CREDENTIALS',
    'CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS',
)
TRANSPORT_KEYS = (
    'SECURE_PROXY_SSL_HEADER', 'USE_X_FORWARDED_HOST', 'SECURE_SSL_REDIRECT',
    'SECURE_REDIRECT_EXEMPT', 'SECURE_HSTS_SECONDS', 'SECURE_HSTS_INCLUDE_SUBDOMAINS',
    'SECURE_HSTS_PRELOAD', 'SECURE_REFERRER_POLICY', 'SESSION_COOKIE_SECURE',
    'CSRF_COOKIE_SECURE', 'SESSION_COOKIE_HTTPONLY', 'SESSION_COOKIE_SAMESITE',
    'CSRF_COOKIE_SAMESITE',
)

# Settings are decided once at import, so each environment gets a fresh interpreter; the empty
# repository keeps a developer's local .env out of the result.
_PROBE = (
    'import json, sys, decouple\n'
    'decouple.config = decouple.Config(decouple.RepositoryEmpty())\n'
    'import core.settings as s\n'
    'print(json.dumps({n: getattr(s, n) for n in sys.argv[1:]}))\n'
)
_ASGI_PROBE = (
    'import decouple\n'
    'decouple.config = decouple.Config(decouple.RepositoryEmpty())\n'
    'import core.asgi\n'
)
_CONTROLLED_ENV = (
    'DJANGO_SETTINGS_MODULE', 'SECRET_KEY', 'DEBUG', 'DATABASE_URL', 'REDIS_URL',
    'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET_NAME', 'R2_ENDPOINT_URL', 'R2_PUBLIC_URL',
    'ALLOWED_HOSTS', 'CORS_ALLOWED_ORIGINS', 'CSRF_TRUSTED_ORIGINS', 'TRUST_PROXY_SSL_HEADER',
    'SECURE_PROXY_SSL_HEADER', 'USE_X_FORWARDED_HOST', 'FORWARDED_ALLOW_IPS',
    'SECURE_SSL_REDIRECT', 'SECURE_HSTS_SECONDS', 'SECURE_HSTS_INCLUDE_SUBDOMAINS',
    'SECURE_HSTS_PRELOAD',
)
_imports = {}


def _environment(overrides):
    # Inherit everything else: Windows writes caches under a literal %SystemDrive% without it.
    env = {key: value for key, value in os.environ.items() if key.upper() not in _CONTROLLED_ENV}
    env.update({
        'DJANGO_SETTINGS_MODULE': 'core.settings',
        'SECRET_KEY': 's' * 64,
        'DEBUG': 'False',
        'DATABASE_URL': 'sqlite:///unused.sqlite3',
        'R2_ACCESS_KEY_ID': 'test',
        'R2_SECRET_ACCESS_KEY': 'test',
        'R2_BUCKET_NAME': 'test',
        'R2_ENDPOINT_URL': 'https://r2.approx.test',
        'R2_PUBLIC_URL': 'https://r2.approx.test',
        'ALLOWED_HOSTS': PROD_HOST,
        'CORS_ALLOWED_ORIGINS': FRONTEND_ORIGIN,
    })
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def import_settings(**overrides):
    """Import core.settings under a production environment plus `overrides` (None unsets)."""
    key = tuple(sorted(overrides.items()))
    if key not in _imports:
        _imports[key] = subprocess.run(
            [sys.executable, '-c', _PROBE, *SETTING_NAMES],
            cwd=BASE_DIR, env=_environment(overrides),
            capture_output=True, text=True, timeout=120,
        )
    return _imports[key]


def settings_for(**overrides):
    result = import_settings(**overrides)
    if result.returncode != 0:
        raise AssertionError(f'core.settings failed to import:\n{result.stderr}')
    return json.loads(result.stdout.strip().splitlines()[-1])


def import_asgi(**overrides):
    """Import core.asgi, as uvicorn does at startup, under a production environment."""
    return subprocess.run(
        [sys.executable, '-c', _ASGI_PROBE],
        cwd=BASE_DIR, env=_environment(overrides),
        capture_output=True, text=True, timeout=180,
    )


def uvicorn_config(**env):
    """Load the HTTP app the way the Procfile's uvicorn does, reading FORWARDED_ALLOW_IPS from `env`."""
    app = ProtocolTypeRouter({'http': ASGIStaticFilesHandler(get_asgi_application())})
    with mock.patch.dict(os.environ, env):
        if 'FORWARDED_ALLOW_IPS' not in env:
            os.environ.pop('FORWARDED_ALLOW_IPS', None)
        config = uvicorn.Config(app, proxy_headers=True, log_config=None, lifespan='off')
    config.load()
    return config


async def asgi_get(app, path, peer, headers=(), host='testserver'):
    """One GET delivered the way uvicorn delivers it: a socket peer plus raw request headers."""
    scope = {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
        'query_string': b'', 'root_path': '',
        'headers': [(name.encode(), value.encode()) for name, value in (('host', host), *headers)],
        'client': (peer, 43210), 'server': ('10.0.0.2', 8080),
    }
    messages = []
    responded = asyncio.Event()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await responded.wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        messages.append(message)
        if message['type'] == 'http.response.body' and not message.get('more_body'):
            responded.set()

    # As Django's own test client does: the test's transaction has to outlive the request.
    request_started.disconnect(close_old_connections)
    request_finished.disconnect(close_old_connections)
    try:
        await app(scope, receive, send)
    finally:
        request_started.connect(close_old_connections)
        request_finished.connect(close_old_connections)
    start = next(message for message in messages if message['type'] == 'http.response.start')
    return start['status'], {k.decode().lower(): v.decode() for k, v in start['headers']}


def make_staff():
    return get_user_model().objects.create_user(
        username='staff', email='staff@approx.test', password=STAFF_PASSWORD,
        is_staff=True, is_superuser=True,
    )


class ProductionSettingsTests(SimpleTestCase):
    """What core.settings itself decides, read from a real import."""

    def test_cors_never_allows_every_origin_or_credentials(self):
        for debug in ('False', 'True'):
            with self.subTest(DEBUG=debug):
                values = settings_for(DEBUG=debug)
                self.assertIs(values['CORS_ALLOW_ALL_ORIGINS'], False)
                self.assertIs(values['CORS_ALLOW_CREDENTIALS'], False)

    def test_cors_covers_the_api_and_nothing_else(self):
        regex = settings_for()['CORS_URLS_REGEX']
        for path in ('/api/users/terms/', '/api/swifts/', '/api/map/'):
            self.assertTrue(re.match(regex, path), path)
        for path in (
            '/admin/', '/admin/login/', '/admin/users/user/', '/legal/terms/',
            '/api/internal/moderation/lease/', '/static/admin/css/base.css', '/', '/apix/',
        ):
            self.assertFalse(re.match(regex, path), path)

    def test_hosts_and_origins_come_only_from_configuration(self):
        values = settings_for()
        self.assertEqual(values['ALLOWED_HOSTS'], [PROD_HOST])
        self.assertEqual(values['CORS_ALLOWED_ORIGINS'], [FRONTEND_ORIGIN])
        self.assertEqual(values['CSRF_TRUSTED_ORIGINS'], [])
        self.assertEqual(settings_for(CORS_ALLOWED_ORIGINS=None)['CORS_ALLOWED_ORIGINS'], [])

    def test_refuses_to_start_without_exact_allowed_hosts(self):
        for value in (None, '*', f'{PROD_HOST},*'):
            with self.subTest(ALLOWED_HOSTS=value):
                result = import_settings(ALLOWED_HOSTS=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('ImproperlyConfigured', result.stderr)
                self.assertIn('ALLOWED_HOSTS', result.stderr)

    def test_refuses_wildcard_csrf_trusted_origins(self):
        for debug, value in (
            ('False', f'https://{PROD_HOST},https://*.railway.app'),
            ('True', 'https://*.vercel.app'),
        ):
            with self.subTest(DEBUG=debug, CSRF_TRUSTED_ORIGINS=value):
                result = import_settings(DEBUG=debug, CSRF_TRUSTED_ORIGINS=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('ImproperlyConfigured', result.stderr)
                self.assertIn('CSRF_TRUSTED_ORIGINS', result.stderr)

    def test_production_transport_and_cookie_hardening(self):
        values = settings_for()
        self.assertIs(values['SECURE_SSL_REDIRECT'], True)
        self.assertEqual(values['SECURE_REDIRECT_EXEMPT'], [r'^api/internal/'])
        self.assertEqual(values['SECURE_HSTS_SECONDS'], 3600)
        self.assertIs(values['SECURE_HSTS_INCLUDE_SUBDOMAINS'], False)
        self.assertIs(values['SECURE_HSTS_PRELOAD'], False)
        self.assertEqual(values['SECURE_REFERRER_POLICY'], 'same-origin')
        self.assertIs(values['SESSION_COOKIE_SECURE'], True)
        self.assertIs(values['CSRF_COOKIE_SECURE'], True)
        self.assertIs(values['SESSION_COOKIE_HTTPONLY'], True)
        self.assertEqual(values['SESSION_COOKIE_SAMESITE'], 'Lax')
        self.assertEqual(values['CSRF_COOKIE_SAMESITE'], 'Lax')
        self.assertIs(values['USE_X_FORWARDED_HOST'], False)
        self.assertIsNone(values['SECURE_PROXY_SSL_HEADER'])

    def test_django_cannot_be_configured_to_trust_raw_forwarded_headers(self):
        values = settings_for(
            TRUST_PROXY_SSL_HEADER='True',
            SECURE_PROXY_SSL_HEADER='HTTP_X_FORWARDED_PROTO,https',
            USE_X_FORWARDED_HOST='True',
        )
        self.assertIsNone(values['SECURE_PROXY_SSL_HEADER'])
        self.assertIs(values['USE_X_FORWARDED_HOST'], False)

    def test_hsts_is_raised_only_by_explicit_configuration(self):
        values = settings_for(SECURE_HSTS_SECONDS='31536000', SECURE_HSTS_INCLUDE_SUBDOMAINS='True')
        self.assertEqual(values['SECURE_HSTS_SECONDS'], 31536000)
        self.assertIs(values['SECURE_HSTS_INCLUDE_SUBDOMAINS'], True)

    def test_development_works_without_any_configuration(self):
        values = settings_for(DEBUG='True', ALLOWED_HOSTS=None, CORS_ALLOWED_ORIGINS=None)
        self.assertEqual(values['ALLOWED_HOSTS'], ['localhost', '127.0.0.1', '[::1]'])
        self.assertIn('http://localhost:5173', values['CORS_ALLOWED_ORIGINS'])
        self.assertIs(values['SECURE_SSL_REDIRECT'], False)
        self.assertEqual(values['SECURE_HSTS_SECONDS'], 0)
        self.assertIs(values['SESSION_COOKIE_SECURE'], False)
        self.assertIs(values['CSRF_COOKIE_SECURE'], False)
        self.assertIsNone(values['SECURE_PROXY_SSL_HEADER'])


class DeploymentCommandTests(SimpleTestCase):
    """M11: the trusted-proxy boundary is uvicorn's, and its value comes from the deployment."""

    @staticmethod
    def web_command():
        return next(
            line.split(':', 1)[1].strip()
            for line in (BASE_DIR / 'Procfile').read_text().splitlines()
            if line.startswith('web:')
        )

    def test_no_deployment_file_trusts_forwarded_headers_from_every_peer(self):
        for name in ('Procfile', 'Dockerfile', 'docker-compose.yml'):
            with self.subTest(file=name):
                text = (BASE_DIR / name).read_text()
                self.assertNotRegex(text, r'''--forwarded-allow-ips[= ]*["']?[^"'\s]*\*''')
                self.assertNotRegex(text, r'''FORWARDED_ALLOW_IPS["']?\s*[:=]\s*["']?\*''')

    def test_uvicorn_takes_trusted_proxies_from_the_environment(self):
        web = self.web_command()
        self.assertIn('--proxy-headers', web)
        self.assertIn('--forwarded-allow-ips="$FORWARDED_ALLOW_IPS"', web)
        self.assertNotRegex(web, r'[0-9A-Fa-f:.]+/\d{1,3}\b')

    @skipUnless(shutil.which('sh'), 'needs a POSIX shell to expand the Procfile command')
    def test_procfile_command_expands_to_the_deployment_value(self):
        uvicorn_command = self.web_command().split('&&')[-1].strip()
        self.assertTrue(uvicorn_command.startswith('uvicorn '), uvicorn_command)
        env = {k: v for k, v in os.environ.items() if k.upper() not in ('FORWARDED_ALLOW_IPS', 'PORT')}
        env.update(PORT='8000', FORWARDED_ALLOW_IPS=TRUSTED_PROXIES)
        result = subprocess.run(
            [shutil.which('sh'), '-c', "printf '%s\\n' " + uvicorn_command[len('uvicorn '):]],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertIn('--proxy-headers', args)
        self.assertIn(f'--forwarded-allow-ips={TRUSTED_PROXIES}', args)


class TrustedProxyGuardTests(SimpleTestCase):
    """M11: production does not serve unless uvicorn has a bounded trusted-proxy set."""

    def test_accepts_explicit_addresses_and_ranges(self):
        for value in (TRUSTED_PROXIES, '203.0.113.7', f'{TRUSTED_PROXIES}, 2001:db8::/32'):
            with self.subTest(value=value):
                require_trusted_proxies(False, value)

    def test_refuses_missing_catch_all_or_malformed_values(self):
        for value in (
            None, '', ' , ', '*', '0.0.0.0/0', '::/0', f'{TRUSTED_PROXIES},*',
            '<verified Railway trusted proxy CIDR>',
        ):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, 'FORWARDED_ALLOW_IPS'):
                    require_trusted_proxies(False, value)

    def test_development_needs_no_proxy_configuration(self):
        require_trusted_proxies(True, None)

    def test_production_asgi_application_refuses_to_load_without_it(self):
        refused = import_asgi(FORWARDED_ALLOW_IPS=None)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('ImproperlyConfigured', refused.stderr)
        self.assertIn('FORWARDED_ALLOW_IPS', refused.stderr)

        # Redis as well: the capacity check (M20) refuses more than one worker without it.
        loaded = import_asgi(FORWARDED_ALLOW_IPS=TRUSTED_PROXIES, REDIS_URL='redis://redis:6379')
        self.assertEqual(loaded.returncode, 0, loaded.stderr)


class ProductionSettingsMixin:
    """Runs the case under the values core.settings produces with DEBUG off."""
    production_keys = ()

    @classmethod
    def setUpClass(cls):
        values = settings_for()
        context = override_settings(**{key: values[key] for key in cls.production_keys})
        context.enable()
        cls.addClassCleanup(context.disable)
        super().setUpClass()


class AdminCorsTests(ProductionSettingsMixin, TestCase):
    """C1: a page on another origin cannot read or drive the admin with a staff session."""
    production_keys = CORS_KEYS

    def setUp(self):
        self.staff = make_staff()
        self.client.force_login(self.staff)

    def assert_no_cors_grant(self, response):
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

    def test_other_origins_cannot_read_admin_pages(self):
        for path in ('/admin/', '/admin/users/user/'):
            for origin in HOSTILE_ORIGINS + (FRONTEND_ORIGIN,):
                with self.subTest(path=path, origin=origin):
                    response = self.client.get(path, HTTP_ORIGIN=origin)
                    self.assertEqual(response.status_code, 200)
                    self.assert_no_cors_grant(response)

    def test_admin_preflight_is_not_granted(self):
        for origin in HOSTILE_ORIGINS:
            with self.subTest(origin=origin):
                response = self.client.options(
                    '/admin/users/user/', HTTP_ORIGIN=origin,
                    HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
                    HTTP_ACCESS_CONTROL_REQUEST_HEADERS='content-type',
                )
                self.assert_no_cors_grant(response)

    def test_legal_and_worker_routes_are_not_cors_surfaces(self):
        for path in ('/legal/terms/', '/api/internal/moderation/lease/'):
            with self.subTest(path=path):
                self.assert_no_cors_grant(self.client.get(path, HTTP_ORIGIN=FRONTEND_ORIGIN))

    def test_admin_post_from_a_claimable_subdomain_fails_the_csrf_origin_check(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.get('/admin/users/user/').status_code, 200)
        data = {
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
            'action': 'delete_selected',
            '_selected_action': [self.staff.pk],
            'index': 0,
        }
        for origin in ('https://attacker.vercel.app', 'https://attacker.up.railway.app'):
            with self.subTest(origin=origin):
                response = client.post('/admin/users/user/', data, secure=True, HTTP_ORIGIN=origin)
                self.assertEqual(response.status_code, 403)

        same_origin = client.post(
            '/admin/users/user/', data, secure=True, HTTP_ORIGIN='https://testserver')
        self.assertNotEqual(same_origin.status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=self.staff.pk).exists())


class ApiCorsTests(ProductionSettingsMixin, TestCase):
    """C1: the configured web origin still reaches /api/, as a bearer-token client."""
    production_keys = CORS_KEYS

    def test_configured_origin_can_read_the_api_without_credentials(self):
        response = self.client.get('/api/users/terms/', HTTP_ORIGIN=FRONTEND_ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], FRONTEND_ORIGIN)
        self.assertNotIn('Access-Control-Allow-Credentials', response.headers)
        self.assertIn('origin', response.headers['Vary'].lower())

    def test_configured_origin_preflight_allows_the_authorization_header(self):
        response = self.client.options(
            '/api/swifts/', HTTP_ORIGIN=FRONTEND_ORIGIN,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Access-Control-Allow-Origin'], FRONTEND_ORIGIN)
        self.assertIn('authorization', response.headers['Access-Control-Allow-Headers'])
        self.assertNotIn('Access-Control-Allow-Credentials', response.headers)

    def test_unlisted_origins_get_no_grant_on_the_api(self):
        for origin in HOSTILE_ORIGINS:
            with self.subTest(origin=origin):
                response = self.client.get('/api/users/terms/', HTTP_ORIGIN=origin)
                self.assertNotIn('Access-Control-Allow-Origin', response.headers)


class TransportSecurityTests(ProductionSettingsMixin, TestCase):
    """M11: HTTPS, HSTS, cookies and hosts as Django applies them to the scheme it is handed."""
    production_keys = TRANSPORT_KEYS

    def test_plain_http_is_redirected_to_https(self):
        response = self.client.get('/api/users/terms/')
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response['Location'].startswith('https://testserver/'))

    def test_raw_forwarded_proto_header_is_not_trusted_by_django(self):
        response = self.client.get('/api/users/terms/', HTTP_X_FORWARDED_PROTO='https')
        self.assertEqual(response.status_code, 301)

    def test_https_request_gets_hsts_and_referrer_policy(self):
        response = self.client.get('/api/users/terms/', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Strict-Transport-Security'], 'max-age=3600')
        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')

    def test_internal_worker_routes_are_not_redirected(self):
        response = self.client.get('/api/internal/moderation/lease/')
        self.assertNotIn(response.status_code, (301, 302, 307, 308))

    def test_admin_cookies_are_secure_httponly_and_lax(self):
        staff = make_staff()
        page = self.client.get('/admin/login/', secure=True)
        csrf = page.cookies['csrftoken']
        self.assertTrue(csrf['secure'])
        self.assertEqual(csrf['samesite'], 'Lax')

        response = self.client.post(
            '/admin/login/?next=/admin/',
            {'username': staff.email, 'password': STAFF_PASSWORD},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        session = response.cookies['sessionid']
        self.assertTrue(session['secure'])
        self.assertTrue(session['httponly'])
        self.assertEqual(session['samesite'], 'Lax')

    def test_forwarded_host_is_ignored(self):
        response = self.client.get(
            '/api/users/terms/', secure=True, HTTP_X_FORWARDED_HOST='attacker.example')
        self.assertEqual(response.status_code, 200)

    def test_unknown_host_header_is_refused(self):
        response = self.client.get('/api/users/terms/', secure=True, HTTP_HOST='attacker.example')
        self.assertEqual(response.status_code, 400)


class UvicornProxyChainTests(ProductionSettingsMixin, TestCase):
    """M11: Railway proxy -> uvicorn (FORWARDED_ALLOW_IPS) -> ASGI scheme -> Django."""
    production_keys = TRANSPORT_KEYS

    def setUp(self):
        self.trusted_app = uvicorn_config(FORWARDED_ALLOW_IPS=TRUSTED_PROXIES).loaded_app
        self.default_app = uvicorn_config().loaded_app

    def test_uvicorn_trusts_only_the_configured_proxies(self):
        config = uvicorn_config(FORWARDED_ALLOW_IPS=TRUSTED_PROXIES)
        self.assertEqual(config.forwarded_allow_ips, TRUSTED_PROXIES)
        self.assertIsInstance(config.loaded_app, ProxyHeadersMiddleware)
        self.assertEqual(uvicorn_config().forwarded_allow_ips, '127.0.0.1')

    async def test_https_forwarded_by_a_trusted_proxy_is_served_as_https(self):
        status, headers = await asgi_get(
            self.trusted_app, '/api/users/terms/', TRUSTED_PROXY_PEER,
            headers=[('x-forwarded-proto', 'https')],
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers['strict-transport-security'], 'max-age=3600')
        self.assertEqual(headers['referrer-policy'], 'same-origin')

    async def test_forwarded_proto_from_an_untrusted_peer_is_redirected(self):
        status, headers = await asgi_get(
            self.trusted_app, '/api/users/terms/', UNTRUSTED_PEER,
            headers=[('x-forwarded-proto', 'https')],
        )
        self.assertEqual(status, 301)
        self.assertEqual(headers['location'], 'https://testserver/api/users/terms/')

    async def test_without_forwarded_allow_ips_no_remote_proxy_is_trusted(self):
        status, _ = await asgi_get(
            self.default_app, '/api/users/terms/', TRUSTED_PROXY_PEER,
            headers=[('x-forwarded-proto', 'https')],
        )
        self.assertEqual(status, 301)

    async def test_private_network_worker_is_not_redirected(self):
        status, _ = await asgi_get(self.trusted_app, '/api/internal/moderation/lease/', 'fd12::5')
        self.assertNotIn(status, (301, 302, 307, 308))

    async def test_forwarded_host_from_a_trusted_proxy_is_ignored(self):
        status, _ = await asgi_get(
            self.trusted_app, '/api/users/terms/', TRUSTED_PROXY_PEER,
            headers=[('x-forwarded-proto', 'https'), ('x-forwarded-host', 'attacker.example')],
        )
        self.assertEqual(status, 200)

    async def test_hostile_host_is_refused_behind_a_trusted_proxy(self):
        status, _ = await asgi_get(
            self.trusted_app, '/api/users/terms/', TRUSTED_PROXY_PEER,
            headers=[('x-forwarded-proto', 'https')], host='attacker.example',
        )
        self.assertEqual(status, 400)
