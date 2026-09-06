import hashlib
import json
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import CommandError, call_command
from PIL import Image

from moderation.cloudflare_ai import CloudflareError, ModerationResult
from .models import SwiftModerationJob, SwiftReport, SwiftVideo
from .testing import SwiftTestCase, make_swift, make_user


class DiagnoseSwiftsTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.creator = make_user('diagnostic_creator')
        self.swift = make_swift(
            self.creator, moderation=SwiftVideo.Moderation.OK,
            duration_ms=30_000, trim_start_ms=10_000, trim_end_ms=18_000)
        self.report = SwiftReport.objects.create(
            swift=self.swift, reporter=self.creator, source=SwiftReport.Source.AI_VIDEO,
            state=SwiftReport.State.REVIEWED, reason=SwiftReport.Reason.OTHER,
            action_taken='content_restored',
        )
        self.job = SwiftModerationJob.objects.create(
            swift=self.swift, state=SwiftModerationJob.State.DONE,
            outcome='quarantined', confidence=0.8, report=self.report,
        )
        frame = BytesIO()
        Image.new('RGB', (20, 10), 'white').save(frame, format='JPEG')
        self.jpeg = frame.getvalue()
        self.inspection = {
            'description': 'A desk and laptop.', 'visible_violation': 'none',
            'evidence': 'none',
        }

    def _saved_state(self):
        return tuple(
            list(model.objects.values())
            for model in (SwiftVideo, SwiftModerationJob, SwiftReport)
        )

    def test_inspection_uses_same_middle_frame_and_never_changes_saved_decisions(self):
        before = self._saved_state()
        out = StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg) as fetch, \
                patch('moderation.cloudflare_ai.classify_image', return_value=ModerationResult(
                    safe=False, categories=('other_objectionable',), confidence=0.8,
                    description='The classifier sees a laptop.',
                    evidence='An alleged visible detail.')) as classify, \
                patch('moderation.cloudflare_ai._run', return_value={
                    'response': self.inspection,
                }) as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=out)
        self.assertEqual(fetch.call_args.args[1], 14_000)
        classify.assert_called_once_with(self.jpeg, at_ms=14_000)
        payload = inspect.call_args.args[1]
        self.assertEqual(payload['image'], list(self.jpeg))
        self.assertEqual(payload['response_format']['type'], 'json_schema')
        schema = payload['response_format']['json_schema']
        self.assertEqual(set(schema['required']),
                         {'description', 'visible_violation', 'evidence'})
        self.assertFalse(schema['additionalProperties'])
        self.assertIn('A desk and laptop.', out.getvalue())
        self.assertIn('JPEG 20x10', out.getvalue())
        self.assertIn(hashlib.sha256(self.jpeg).hexdigest(), out.getvalue())
        line = next(line for line in out.getvalue().splitlines()
                    if line.startswith('Current classifier: '))
        current = json.loads(line.removeprefix('Current classifier: '))
        self.assertEqual(current['description'], 'The classifier sees a laptop.')
        self.assertEqual(current['evidence'], 'An alleged visible detail.')
        self.assertEqual(self._saved_state(), before)

    def test_independent_inspection_accepts_json_text(self):
        out, errors = StringIO(), StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                patch('moderation.cloudflare_ai.classify_image', return_value=ModerationResult()), \
                patch('moderation.cloudflare_ai._run', return_value={
                    'response': json.dumps(self.inspection),
                }):
            call_command('diagnose_swifts', str(self.swift.pk), stdout=out, stderr=errors)
        line = next(line for line in out.getvalue().splitlines()
                    if line.startswith('Independent inspection: '))
        self.assertEqual(json.loads(line.removeprefix('Independent inspection: ')),
                         self.inspection)
        self.assertEqual(errors.getvalue(), '')

    def test_invalid_inspection_is_reported_instead_of_printed_as_valid_evidence(self):
        invalid_responses = [
            'A desk and laptop. There is no visible violation.',
            'Here is the answer: ' + json.dumps(self.inspection),
            None,
            [self.inspection],
            {'description': 'A desk and laptop.', 'visible_violation': 'none'},
            {**self.inspection, 'evidence': []},
            {**self.inspection, 'description': False},
            {**self.inspection, 'visible_violation': '   '},
            {**self.inspection, 'description': 'a' * 801},
            {**self.inspection, 'visible_violation': 'a' * 301},
            {**self.inspection, 'evidence': 'a' * 801},
            {**self.inspection, 'unsupported': 'another answer'},
        ]
        before = self._saved_state()
        for response in invalid_responses:
            with self.subTest(response=response):
                out, errors = StringIO(), StringIO()
                with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                        patch('moderation.cloudflare_ai.classify_image',
                              return_value=ModerationResult()), \
                        patch('moderation.cloudflare_ai._run',
                              return_value={'response': response}):
                    call_command('diagnose_swifts', str(self.swift.pk),
                                 stdout=out, stderr=errors)
                self.assertIn('Current classifier: ', out.getvalue())
                self.assertNotIn('Independent inspection: ', out.getvalue())
                self.assertIn('Independent inspection failed: ', errors.getvalue())
        self.assertEqual(self._saved_state(), before)

    def test_current_classifier_failure_still_inspects_same_frame_and_redacts_error(self):
        before = self._saved_state()
        out, errors = StringIO(), StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                patch('moderation.cloudflare_ai.classify_image',
                      side_effect=CloudflareError('Invalid JSON Bearer fake-secret-123')), \
                patch('moderation.cloudflare_ai._run',
                      return_value={'response': self.inspection}) as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=out, stderr=errors)
        inspect.assert_called_once()
        self.assertEqual(inspect.call_args.args[1]['image'], list(self.jpeg))
        self.assertIn('Current classifier failed:', errors.getvalue())
        self.assertNotIn('fake-secret-123', errors.getvalue())
        self.assertIn('[redacted]', errors.getvalue())
        self.assertIn('Independent inspection: ', out.getvalue())
        self.assertEqual(self._saved_state(), before)

    def test_optional_saved_jpeg_is_identical_to_both_model_inputs(self):
        with TemporaryDirectory(prefix='swift-diagnosis-') as directory:
            with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                    patch('moderation.cloudflare_ai.classify_image',
                          return_value=ModerationResult()) as classify, \
                    patch('moderation.cloudflare_ai._run',
                          return_value={'response': self.inspection}) as inspect:
                call_command('diagnose_swifts', str(self.swift.pk),
                             '--output-dir', directory, stdout=StringIO())
            saved = Path(directory) / f'swift-{self.swift.pk}-frame-14000ms.jpg'
            self.assertEqual(saved.read_bytes(), self.jpeg)
            self.assertEqual(classify.call_args.args[0], saved.read_bytes())
            self.assertEqual(inspect.call_args.args[1]['image'], list(saved.read_bytes()))

    def test_non_image_response_never_reaches_ai(self):
        errors = StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=b'<html>Error</html>'), \
                patch('moderation.cloudflare_ai.classify_image') as classify, \
                patch('moderation.cloudflare_ai._run') as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=StringIO(), stderr=errors)
        classify.assert_not_called()
        inspect.assert_not_called()
        self.assertIn('Frame inspection failed', errors.getvalue())

    def test_one_bad_frame_does_not_skip_remaining_frames_and_errors_are_redacted(self):
        out, errors = StringIO(), StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', side_effect=[
            CloudflareError('Bearer fake-secret-123'), self.jpeg, self.jpeg, self.jpeg, self.jpeg,
        ]) as fetch, patch('moderation.cloudflare_ai.classify_image',
                          return_value=ModerationResult()), \
                patch('moderation.cloudflare_ai._run',
                      return_value={'response': self.inspection}) as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), '--all-frames',
                         stdout=out, stderr=errors)
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(inspect.call_count, 4)
        self.assertEqual(out.getvalue().count('Independent inspection: '), 4)
        self.assertNotIn('fake-secret-123', errors.getvalue())
        self.assertIn('[redacted]', errors.getvalue())

    def test_one_invalid_inspection_does_not_skip_remaining_frames(self):
        out, errors = StringIO(), StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                patch('moderation.cloudflare_ai.classify_image',
                      return_value=ModerationResult()), \
                patch('moderation.cloudflare_ai._run', side_effect=[
                    {'response': 'A desk.'},
                    *[{'response': self.inspection} for _ in range(4)],
                ]) as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), '--all-frames',
                         stdout=out, stderr=errors)
        self.assertEqual(inspect.call_count, 5)
        self.assertEqual(out.getvalue().count('Independent inspection: '), 4)
        self.assertEqual(errors.getvalue().count('Independent inspection failed:'), 1)

    def test_independent_request_failure_is_redacted(self):
        errors = StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                patch('moderation.cloudflare_ai.classify_image',
                      return_value=ModerationResult()), \
                patch('moderation.cloudflare_ai._run',
                      side_effect=CloudflareError('Worker error Bearer fake-secret-123')):
            call_command('diagnose_swifts', str(self.swift.pk),
                         stdout=StringIO(), stderr=errors)
        self.assertIn('Independent inspection failed:', errors.getvalue())
        self.assertIn('[redacted]', errors.getvalue())
        self.assertNotIn('fake-secret-123', errors.getvalue())

    def test_unknown_or_invalid_ids_fail_before_any_cloudflare_calls(self):
        for swift_id in ('0', str(self.swift.pk + 100)):
            with self.subTest(swift_id=swift_id), \
                    patch('moderation.cloudflare_ai.fetch_frame') as fetch, \
                    self.assertRaises(CommandError):
                call_command('diagnose_swifts', swift_id, stdout=StringIO())
            fetch.assert_not_called()
