"""The Cloudflare client, with no Cloudflare.

Every test in this file mocks `requests` at the boundary. Nothing here makes a
network call, and the token used throughout is a recognisable fake — the one
assertion this whole suite exists to protect is `TokenSecrecyTests` at the
bottom, which checks that a real one could not escape into a response body, an
exception message or a log line even when everything else goes wrong.
"""
import json
import logging
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from . import cloudflare_ai as cf
from .reasons import Reason

#: Not a credential. Distinctive enough that a grep over test output would find
#: it instantly if any of these tests started leaking one.
FAKE_TOKEN = 'cf-test-token-NEVER-REAL-0000'
FAKE_ACCOUNT = 'acct-test-0000'

CREDS = dict(
    CLOUDFLARE_API_TOKEN=FAKE_TOKEN,
    CLOUDFLARE_ACCOUNT_ID=FAKE_ACCOUNT,
    CLOUDFLARE_MEDIA_TRANSFORM_BASE='https://media.example.com',
)


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, content=b'', text=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


def ai_ok(response_text):
    return FakeResponse(payload={'success': True, 'result': {'response': response_text}})


# ─── Credentials ──────────────────────────────────────────────────────────────
class CredentialTests(SimpleTestCase):
    @override_settings(CLOUDFLARE_API_TOKEN='', CLOUDFLARE_ACCOUNT_ID='')
    def test_missing_credentials_raise_rather_than_pass(self):
        """The failure that matters. A version of this that returned "safe,
        nothing configured" would publish everything the moment a variable was
        mistyped."""
        with self.assertRaises(cf.MissingCredentials):
            cf.credentials()
        self.assertFalse(cf.is_configured())

    @override_settings(CLOUDFLARE_API_TOKEN=FAKE_TOKEN, CLOUDFLARE_ACCOUNT_ID='')
    def test_account_id_alone_is_not_enough(self):
        with self.assertRaises(cf.MissingCredentials):
            cf.credentials()

    @override_settings(**CREDS)
    def test_missing_credential_errors_are_not_retryable(self):
        with override_settings(CLOUDFLARE_API_TOKEN=''):
            try:
                cf.credentials()
            except cf.MissingCredentials as exc:
                self.assertFalse(exc.retryable)


# ─── Media Transformations ────────────────────────────────────────────────────
@override_settings(**CREDS)
class FrameFetchTests(SimpleTestCase):
    def test_the_url_is_a_media_transformation(self):
        seen = {}

        def fake_get(url, timeout=None):
            seen['url'] = url
            return FakeResponse(content=b'\xff\xd8jpeg')

        with patch.object(requests, 'get', fake_get):
            data = cf.fetch_frame('https://cdn.example.com/v/a.mp4', 4500, width=640)

        self.assertEqual(data, b'\xff\xd8jpeg')
        self.assertIn('https://media.example.com/cdn-cgi/media/', seen['url'])
        self.assertIn('mode=frame', seen['url'])
        self.assertIn('time=4.5s', seen['url'])
        self.assertIn('format=jpg', seen['url'])
        self.assertIn('width=640', seen['url'])
        self.assertIn('https://cdn.example.com/v/a.mp4', seen['url'])

    def test_a_timeout_is_retryable(self):
        with patch.object(requests, 'get', side_effect=requests.Timeout()):
            with self.assertRaises(cf.CloudflareError) as ctx:
                cf.fetch_frame('https://cdn.example.com/v/a.mp4', 0)
        self.assertTrue(ctx.exception.retryable)

    def test_a_404_is_not_retryable(self):
        """Transformations not enabled on the zone answers the same way
        forever; retrying it burns quota to learn nothing."""
        with patch.object(requests, 'get', return_value=FakeResponse(
                status_code=404, text='not found')):
            with self.assertRaises(cf.CloudflareError) as ctx:
                cf.fetch_frame('https://cdn.example.com/v/a.mp4', 0)
        self.assertFalse(ctx.exception.retryable)

    def test_a_500_is_retryable(self):
        with patch.object(requests, 'get', return_value=FakeResponse(
                status_code=503, text='upstream')):
            with self.assertRaises(cf.CloudflareError) as ctx:
                cf.fetch_frame('https://cdn.example.com/v/a.mp4', 0)
        self.assertTrue(ctx.exception.retryable)

    def test_an_empty_body_is_not_a_frame(self):
        with patch.object(requests, 'get', return_value=FakeResponse(content=b'')):
            with self.assertRaises(cf.CloudflareError):
                cf.fetch_frame('https://cdn.example.com/v/a.mp4', 0)

    @override_settings(CLOUDFLARE_MEDIA_TRANSFORM_BASE='', R2_PUBLIC_URL='')
    def test_no_origin_configured_is_an_error_not_a_pass(self):
        with self.assertRaises(cf.CloudflareError) as ctx:
            cf.fetch_frame('https://cdn.example.com/v/a.mp4', 0)
        self.assertFalse(ctx.exception.retryable)

    @override_settings(CLOUDFLARE_MEDIA_TRANSFORM_BASE='',
                       R2_PUBLIC_URL='https://cdn.aura.example/')
    def test_the_origin_falls_back_to_the_bucket(self):
        self.assertEqual(cf.transform_base(), 'https://cdn.aura.example')

    def test_audio_is_capped_at_sixty_seconds(self):
        seen = {}

        def fake_get(url, timeout=None):
            seen['url'] = url
            return FakeResponse(content=b'audio')

        with patch.object(requests, 'get', fake_get):
            cf.fetch_audio('https://cdn.example.com/v/a.mp4', duration_s=500)
        self.assertIn('mode=audio', seen['url'])
        self.assertIn('duration=60s', seen['url'])


# ─── Parsing model output ─────────────────────────────────────────────────────
class ParseVisionTests(SimpleTestCase):
    def test_a_clean_safe_answer(self):
        result = cf.parse_vision_response(
            '{"unsafe": false, "categories": [], "confidence": 0.98}')
        self.assertTrue(result.safe)
        self.assertAlmostEqual(result.confidence, 0.98)
        self.assertEqual(result.categories, ())

    def test_a_clean_unsafe_answer_maps_to_a_reason(self):
        result = cf.parse_vision_response(
            '{"unsafe": true, "categories": ["sexual_content"], "confidence": 0.94}',
            at_ms=1200)
        self.assertFalse(result.safe)
        self.assertEqual(result.categories, ('sexual_content',))
        self.assertEqual(result.reasons, (Reason.NUDITY,))
        self.assertEqual(result.at_ms, 1200)

    def test_json_wrapped_in_prose_is_still_read(self):
        """Models ignore "no other text" routinely, and refusing those answers
        would fail closed on a model that actually classified correctly."""
        result = cf.parse_vision_response(
            'Sure! Here is my answer:\n```json\n'
            '{"unsafe": true, "categories": ["graphic_violence"], "confidence": 0.8}\n'
            '```\nHope that helps.')
        self.assertFalse(result.safe)
        self.assertEqual(result.reasons, (Reason.VIOLENCE,))

    def test_a_word_confidence_is_understood(self):
        result = cf.parse_vision_response(
            '{"unsafe": true, "categories": ["nudity"], "confidence": "high"}')
        self.assertAlmostEqual(result.confidence, 0.9)

    def test_a_percentage_confidence_is_rescaled(self):
        result = cf.parse_vision_response(
            '{"unsafe": true, "categories": ["nudity"], "confidence": 85}')
        self.assertAlmostEqual(result.confidence, 0.85)

    def test_an_unreadable_confidence_becomes_uncertainty_not_certainty(self):
        result = cf.parse_vision_response(
            '{"unsafe": true, "categories": ["nudity"], "confidence": "banana"}')
        self.assertEqual(result.confidence, 0.0)
        self.assertFalse(result.safe)

    def test_invented_categories_are_dropped(self):
        result = cf.parse_vision_response(
            '{"unsafe": true, "categories": ["vibes_are_off"], "confidence": 0.9}')
        self.assertFalse(result.safe)
        self.assertEqual(result.categories, ())
        self.assertEqual(result.reasons, ())

    def test_no_json_at_all_raises(self):
        with self.assertRaises(cf.CloudflareError):
            cf.parse_vision_response('I am not able to help with that.')

    def test_malformed_json_raises(self):
        with self.assertRaises(cf.CloudflareError):
            cf.parse_vision_response('{"unsafe": tru, "categories": []}')

    def test_a_missing_verdict_raises_rather_than_defaulting_to_safe(self):
        """The single most important line in this file. A default
        `ModerationResult()` is `safe=True`, so a lenient parser here would
        approve everything a broken model touched, invisibly."""
        with self.assertRaises(cf.CloudflareError):
            cf.parse_vision_response('{"categories": [], "confidence": 0.9}')

    def test_empty_response_raises(self):
        with self.assertRaises(cf.CloudflareError):
            cf.parse_vision_response('')

    def test_stringly_typed_booleans_are_accepted(self):
        result = cf.parse_vision_response(
            '{"unsafe": "true", "categories": ["hate_symbols"], "confidence": 0.7}')
        self.assertFalse(result.safe)
        self.assertEqual(result.reasons, (Reason.HATE,))


# ─── Workers AI transport ─────────────────────────────────────────────────────
@override_settings(**CREDS)
class WorkersAiTests(SimpleTestCase):
    def test_the_endpoint_and_auth_header_are_right(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, headers=headers, body=json, timeout=timeout)
            return ai_ok('{"unsafe": false, "categories": [], "confidence": 0.9}')

        with patch.object(requests, 'post', fake_post):
            cf.classify_image(b'\xff\xd8')

        self.assertEqual(
            seen['url'],
            f'{cf.API_ROOT}/accounts/{FAKE_ACCOUNT}/ai/run/'
            '@cf/meta/llama-3.2-11b-vision-instruct')
        self.assertEqual(seen['headers']['Authorization'], f'Bearer {FAKE_TOKEN}')
        self.assertEqual(seen['body']['image'], [255, 216])
        self.assertIsNotNone(seen['timeout'])

    def test_every_call_carries_a_timeout(self):
        """A hung inference request holding a worker thread open is how a
        moderation feature takes the whole API down with it."""
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen['timeout'] = timeout
            return ai_ok('{"unsafe": false, "categories": [], "confidence": 1}')

        with override_settings(CLOUDFLARE_AI_TIMEOUT_S=7):
            with patch.object(requests, 'post', fake_post):
                cf.classify_image(b'x')
        self.assertEqual(seen['timeout'], 7.0)

    def test_a_success_false_body_is_an_error(self):
        with patch.object(requests, 'post', return_value=FakeResponse(
                payload={'success': False, 'errors': [{'message': 'nope'}]})):
            with self.assertRaises(cf.CloudflareError):
                cf.classify_image(b'x')

    def test_a_non_json_body_is_an_error(self):
        with patch.object(requests, 'post', return_value=FakeResponse(
                payload=None, text='<html>502</html>')):
            with self.assertRaises(cf.CloudflareError):
                cf.classify_image(b'x')

    def test_a_403_is_not_retried(self):
        with patch.object(requests, 'post', return_value=FakeResponse(
                status_code=403, text='token lacks Workers AI')):
            with self.assertRaises(cf.CloudflareError) as ctx:
                cf.classify_image(b'x')
        self.assertFalse(ctx.exception.retryable)

    def test_a_429_is_retried(self):
        with patch.object(requests, 'post', return_value=FakeResponse(
                status_code=429, text='slow down')):
            with self.assertRaises(cf.CloudflareError) as ctx:
                cf.classify_image(b'x')
        self.assertTrue(ctx.exception.retryable)

    def test_transcription_returns_text_and_forms_no_verdict(self):
        with patch.object(requests, 'post', return_value=FakeResponse(
                payload={'success': True, 'result': {'text': ' hello there '}})):
            self.assertEqual(cf.transcribe(b'audio').text, 'hello there')

    def test_transcription_without_text_is_an_error(self):
        with patch.object(requests, 'post', return_value=FakeResponse(
                payload={'success': True, 'result': {}})):
            with self.assertRaises(cf.CloudflareError):
                cf.transcribe(b'audio')


# ─── The one that protects the credential ─────────────────────────────────────
@override_settings(**CREDS)
class TokenSecrecyTests(SimpleTestCase):
    """The token must not reach a log, an exception or a diagnostic.

    Every one of these is a real path a credential escapes by: an error handler
    that prints the request, a summary endpoint that shows "the first four
    characters so you can check it", a log line at DEBUG that nobody looked at
    in production.
    """

    def test_an_http_error_message_carries_no_token(self):
        with patch.object(requests, 'post', return_value=FakeResponse(
                status_code=401,
                text=f'Authorization: Bearer {FAKE_TOKEN} was rejected')):
            with self.assertRaises(cf.CloudflareError) as ctx:
                cf.classify_image(b'x')
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))
        self.assertIn('[redacted]', str(ctx.exception))

    def test_logging_a_failure_carries_no_token(self):
        with self.assertLogs('moderation.cloudflare_ai', level=logging.DEBUG) as logs:
            logging.getLogger('moderation.cloudflare_ai').debug('probe')
            with patch.object(requests, 'post', return_value=FakeResponse(
                    status_code=500, text=f'bearer {FAKE_TOKEN}')):
                with self.assertRaises(cf.CloudflareError):
                    cf.classify_image(b'x')
        self.assertNotIn(FAKE_TOKEN, '\n'.join(logs.output))

    def test_the_configuration_summary_reports_presence_not_value(self):
        summary = cf.configuration_summary()
        self.assertTrue(summary['api_token_set'])
        self.assertNotIn(FAKE_TOKEN, json.dumps(summary))
        # Not even a prefix. "The first four characters so you can check it" is
        # how a credential ends up in a screenshot in a ticket.
        self.assertNotIn(FAKE_TOKEN[:4], json.dumps(summary))

    def test_the_prompt_is_never_part_of_a_result(self):
        """A published filter prompt is a specification for getting past the
        filter."""
        result = cf.parse_vision_response(
            '{"unsafe": false, "categories": [], "confidence": 1}')
        self.assertNotIn('classifier', str(result))


# ─── Startup checks ───────────────────────────────────────────────────────────
class ConfigurationCheckTests(SimpleTestCase):
    """The misconfiguration has to be noticed at deploy time, not as a pile of
    invisible Swifts an hour later.

    Warnings rather than errors, deliberately: a check at ERROR level aborts
    `manage.py migrate`, which is the first thing the Procfile runs, so a
    mistyped variable would take the whole service down instead of degrading one
    feature. The runtime already fails in the safe direction on its own — see
    `swifts.test_ai_moderation`.
    """

    def ids(self, **overrides):
        from .checks import check_ai_moderation

        with override_settings(**overrides):
            return {m.id for m in check_ai_moderation(None)}

    def test_nothing_is_reported_while_the_feature_is_off(self):
        self.assertEqual(
            self.ids(SWIFT_AI_MODERATION_ENABLED=False,
                     CLOUDFLARE_API_TOKEN='', CLOUDFLARE_ACCOUNT_ID=''),
            set(),
        )

    def test_missing_credentials_are_reported(self):
        from .checks import W001

        self.assertIn(W001, self.ids(
            SWIFT_AI_MODERATION_ENABLED=True,
            CLOUDFLARE_API_TOKEN='', CLOUDFLARE_ACCOUNT_ID='',
            CLOUDFLARE_MEDIA_TRANSFORM_BASE='https://media.example.com'))

    def test_a_missing_transform_origin_is_reported(self):
        from .checks import W002

        self.assertIn(W002, self.ids(
            SWIFT_AI_MODERATION_ENABLED=True, **CREDS | {
                'CLOUDFLARE_MEDIA_TRANSFORM_BASE': '', 'R2_PUBLIC_URL': ''}))

    def test_an_r2_dev_origin_is_reported_on_its_own(self):
        """The failure that looks like a working configuration: the URL
        resolves, the video plays, and every /cdn-cgi/media/ request 404s
        because r2.dev is not a zone anybody can enable transformations on."""
        from .checks import W003

        self.assertIn(W003, self.ids(
            SWIFT_AI_MODERATION_ENABLED=True, **CREDS | {
                'CLOUDFLARE_MEDIA_TRANSFORM_BASE': 'https://pub-abc.r2.dev'}))

    def test_a_complete_configuration_reports_nothing(self):
        self.assertEqual(
            self.ids(SWIFT_AI_MODERATION_ENABLED=True, **CREDS), set())
