"""R2 configuration, and the boot that must refuse a bad one.

The fault these cover shipped: `R2_PRIVATE_BUCKET_NAME` defaulted to an empty
string, so a server with it unset booted perfectly and then failed every single
publish with a 500 and an empty body -- botocore rejects a blank bucket name
before it sends anything, and nothing in the response said storage was involved.
"""
import logging
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from core.checks import private_media_problems
from core.media_storage import ProtectedMediaStorage
from core.structured_logging import JSONFormatter, request_id
from swifts.testing import SwiftTestCase, fake_video, make_user

S3 = 'storages.backends.s3boto3.S3Boto3Storage'


def storages_config(private_bucket='private-bucket', public_bucket='public-bucket', **private_overrides):
    """A STORAGES mapping shaped like the deployed one, with one thing posed wrong."""
    private = {
        'access_key': 'id', 'secret_key': 'secret', 'endpoint_url': 'https://r2.example.test',
        'bucket_name': private_bucket, 'custom_domain': None, 'querystring_auth': True,
    }
    private.update(private_overrides)
    if private_bucket is None:
        private.pop('bucket_name')
    return {
        'private_media': {'BACKEND': S3, 'OPTIONS': private},
        'default': {'BACKEND': S3, 'OPTIONS': {
            'access_key': 'id', 'secret_key': 'secret', 'endpoint_url': 'https://r2.example.test',
            'bucket_name': public_bucket,
        }},
    }


def ids(problems):
    return [identifier for identifier, _ in problems]


class PrivateBucketConfigurationTests(SimpleTestCase):
    def test_a_correctly_separated_private_bucket_has_nothing_to_report(self):
        self.assertEqual(private_media_problems(storages_config()), [])

    def test_a_missing_private_bucket_is_refused(self):
        self.assertEqual(ids(private_media_problems(storages_config(private_bucket=None))), ['core.E001'])

    def test_a_blank_or_whitespace_private_bucket_is_refused(self):
        for blank in ('', '   '):
            with self.subTest(bucket=repr(blank)):
                self.assertEqual(ids(private_media_problems(storages_config(private_bucket=blank))), ['core.E001'])

    def test_a_private_bucket_equal_to_the_public_one_is_refused(self):
        problems = private_media_problems(storages_config(private_bucket='same', public_bucket='same'))
        self.assertEqual(ids(problems), ['core.E002'])
        self.assertIn('readable over the public domain', problems[0][1])

    def test_private_media_served_publicly_is_refused(self):
        self.assertIn('core.E003', ids(private_media_problems(storages_config(custom_domain='cdn.example.test'))))
        self.assertIn('core.E003', ids(private_media_problems(storages_config(querystring_auth=False))))

    def test_missing_credentials_are_reported_by_name_only(self):
        problems = private_media_problems(storages_config(access_key='', secret_key=''))
        self.assertEqual(ids(problems), ['core.E004'])
        message = problems[0][1]
        self.assertIn('access_key', message)
        self.assertIn('secret_key', message)

    def test_every_fault_is_reported_in_one_pass(self):
        problems = private_media_problems(storages_config(private_bucket='', querystring_auth=False, endpoint_url=''))
        self.assertEqual(sorted(ids(problems)), ['core.E001', 'core.E003', 'core.E004'])

    def test_local_filesystem_storage_and_debug_have_no_r2_to_check(self):
        local = {'private_media': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {}},
                 'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {}}}
        self.assertEqual(private_media_problems(local), [])
        self.assertEqual(private_media_problems(storages_config(private_bucket=''), debug=True), [])


class BootGuardTests(SimpleTestCase):
    """The checks framework is not enough on its own: uvicorn never runs it."""

    def test_a_blank_private_bucket_stops_the_app_registry_from_starting(self):
        with override_settings(STORAGES=storages_config(private_bucket=''), DEBUG=False):
            with self.assertRaises(ImproperlyConfigured) as caught:
                apps.get_app_config('core').ready()
        self.assertIn('core.E001', str(caught.exception))

    def test_a_correct_configuration_starts_normally(self):
        with override_settings(STORAGES=storages_config(), DEBUG=False):
            apps.get_app_config('core').ready()


class StorageFailureLoggingTests(SimpleTestCase):
    def test_a_failed_private_write_is_logged_with_operation_bucket_and_error_type(self):
        private = MagicMock()
        private.save.side_effect = ValueError('bucket name is invalid')
        with patch('core.media_storage.storages', {'default': MagicMock(), 'private_media': private}):
            storage = ProtectedMediaStorage()
            with self.assertLogs('core.media_storage', level='ERROR') as captured:
                with self.assertRaises(ValueError):
                    storage._save('private/swifts/x.mp4', MagicMock())

        record = captured.records[0]
        self.assertEqual(record.operation, 'save')
        self.assertEqual(record.bucket, 'private')
        self.assertEqual(record.error_type, 'ValueError')

    def test_the_json_line_carries_the_fields_and_the_request_id(self):
        token = request_id.set('abc123')
        try:
            record = logging.LogRecord('core.media_storage', logging.ERROR, __file__, 1,
                                       'media_storage_operation_failed', None, None)
            record.operation, record.bucket, record.error_type = 'save', 'private', 'ParamValidationError'
            line = JSONFormatter().format(record)
        finally:
            request_id.reset(token)

        self.assertIn('"operation": "save"', line)
        self.assertIn('"bucket": "private"', line)
        self.assertIn('"error_type": "ParamValidationError"', line)
        self.assertIn('"request_id": "abc123"', line)

    def test_a_bucket_name_or_url_in_those_fields_is_dropped_rather_than_logged(self):
        record = logging.LogRecord('core.media_storage', logging.ERROR, __file__, 1, 'x', None, None)
        record.operation = 'https://account.r2.cloudflarestorage.com/secret-bucket'
        record.bucket = 'my-private-bucket'
        line = JSONFormatter().format(record)
        self.assertNotIn('cloudflarestorage', line)
        self.assertNotIn('my-private-bucket', line)


class SwiftPublishStorageTests(SwiftTestCase):
    def test_publishing_writes_the_video_to_the_private_backend_and_never_the_public_one(self):
        author = make_user('private-bucket-publisher')
        client = APIClient()
        client.force_authenticate(author)

        public, private = MagicMock(), MagicMock()
        private.exists.return_value = False
        private.save.side_effect = lambda name, content: name
        private.url.side_effect = lambda name: 'https://r2.example.test/' + name + '?X-Amz-Signature=abc'

        with patch('core.media_storage.storages', {'default': public, 'private_media': private}):
            response = client.post('/api/swifts/', {
                'video': fake_video(), 'duration_ms': 5000,
            }, format='multipart')

        self.assertEqual(response.status_code, 201)
        public.save.assert_not_called()
        self.assertTrue(private.save.called)
        self.assertTrue(private.save.call_args[0][0].startswith('private/'))
        self.assertIn('X-Amz-Signature', response.data['video_url'])
