import asyncio
import json
import logging
import sys

from django.http import HttpResponse
from django.test import SimpleTestCase, RequestFactory

from core.credential_logging import CredentialRedactionFilter
from core.request_logging import RequestLoggingMiddleware
from core.structured_logging import JSONFormatter, request_id


class LoggingTests(SimpleTestCase):
    def render(self, name, msg, args=(), exc_info=None):
        record = logging.LogRecord(name, logging.WARNING, __file__, 1, msg, args, exc_info)
        CredentialRedactionFilter().filter(record)
        return JSONFormatter().format(record)

    def test_single_line_json_redacts_tokens_signed_urls_and_exception_payloads(self):
        output = self.render('app', 'failed %s %s', (
            'ExponentPushToken[secret]', 'https://storage.test/video?X-Amz-Signature=secret-signature'))
        self.assertNotIn('secret', output)
        self.assertIn('[redacted]', json.loads(output)['event'])
        try:
            raise RuntimeError('private message latitude=43.123456 password=secret')
        except RuntimeError:
            output = self.render('app', 'operation_failed', exc_info=sys.exc_info())
        self.assertNotIn('private message', output)
        self.assertNotIn('43.123456', output)
        self.assertNotIn('secret', output)
        self.assertEqual(json.loads(output)['error_type'], 'RuntimeError')
        self.assertTrue(json.loads(output)['trace'])

    def test_framework_logs_do_not_expose_arbitrary_paths_or_addresses(self):
        for name in ('django.request', 'django.security.DisallowedHost', 'uvicorn.access'):
            output = self.render(name, 'bad path /private-user/43.123456?token=secret from 192.0.2.1')
            for private in ('private-user', '43.123456', '192.0.2.1', 'secret'):
                self.assertNotIn(private, output)

    def test_sync_request_id_is_generated_and_reset_even_after_errors(self):
        seen = []
        def view(request):
            seen.append(request_id.get())
            return HttpResponse('ok')
        middleware = RequestLoggingMiddleware(view)
        response = middleware(RequestFactory().get('/', HTTP_X_REQUEST_ID='untrusted'))
        self.assertEqual(response['X-Request-ID'], seen[0])
        self.assertRegex(seen[0], r'^[a-f0-9]{32}$')
        self.assertIsNone(request_id.get())
        def broken(request):
            raise RuntimeError('failure')
        with self.assertRaises(RuntimeError):
            RequestLoggingMiddleware(broken)(RequestFactory().get('/'))
        self.assertIsNone(request_id.get())

    async def test_concurrent_async_requests_keep_separate_ids(self):
        async def view(request):
            before = request_id.get()
            await asyncio.sleep(0)
            self.assertEqual(before, request_id.get())
            return HttpResponse('ok')
        middleware = RequestLoggingMiddleware(view)
        responses = await asyncio.gather(*(middleware(RequestFactory().get('/')) for _ in range(20)))
        self.assertEqual(len({response['X-Request-ID'] for response in responses}), 20)
        self.assertIsNone(request_id.get())
