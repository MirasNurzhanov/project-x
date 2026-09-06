import base64
import hashlib
import json
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import override_settings
from PIL import Image

from moderation.cloudflare_ai import CloudflareError
from .models import SwiftModerationJob, SwiftReport, SwiftVideo
from .testing import SwiftTestCase, make_swift, make_user


@override_settings(CLOUDFLARE_AI_VISION_MODEL='@cf/meta/llama-3.2-11b-vision-instruct')
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
        self.vision = {
            'description': 'A desk and laptop.', 'evidence': '',
            'unsafe': False, 'categories': [], 'confidence': 0.94,
        }
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
                patch('moderation.cloudflare_ai.classify_image_response', return_value={'response': {
                    'unsafe': True, 'categories': ['other_objectionable'], 'confidence': 0.8,
                    'description': 'The classifier sees a laptop.',
                    'evidence': 'An alleged visible detail.'}}) as classify, \
                patch('moderation.cloudflare_ai._run', return_value={
                    'response': self.inspection,
                }) as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=out)
        self.assertEqual(fetch.call_args.args[1], 14_000)
        classify.assert_called_once_with(self.jpeg)
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
                patch('moderation.cloudflare_ai.classify_image_response', return_value={'response': self.vision}), \
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
                        patch('moderation.cloudflare_ai.classify_image_response',
                              return_value={'response': self.vision}), \
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
                patch('moderation.cloudflare_ai.classify_image_response',
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
                    patch('moderation.cloudflare_ai.classify_image_response',
                          return_value={'response': self.vision}) as classify, \
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
                patch('moderation.cloudflare_ai.classify_image_response') as classify, \
                patch('moderation.cloudflare_ai._run') as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=StringIO(), stderr=errors)
        classify.assert_not_called()
        inspect.assert_not_called()
        self.assertIn('Frame inspection failed', errors.getvalue())

    def test_one_bad_frame_does_not_skip_remaining_frames_and_errors_are_redacted(self):
        out, errors = StringIO(), StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', side_effect=[
            CloudflareError('Bearer fake-secret-123'), self.jpeg, self.jpeg, self.jpeg, self.jpeg,
        ]) as fetch, patch('moderation.cloudflare_ai.classify_image_response',
                          return_value={'response': self.vision}), \
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
                patch('moderation.cloudflare_ai.classify_image_response',
                      return_value={'response': self.vision}), \
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
                patch('moderation.cloudflare_ai.classify_image_response',
                      return_value={'response': self.vision}), \
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

    def test_show_raw_reuses_both_calls_and_preserves_human_reviewed_rows(self):
        before = self._saved_state()
        out, errors = StringIO(), StringIO()
        prose = 'The image shows a laptop and a cup. No visible violation.'
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg) as fetch, \
                patch('moderation.cloudflare_ai._run', side_effect=[
                    {'response': prose, 'usage': {
                        'prompt_tokens': 182, 'completion_tokens': 257, 'total_tokens': 439,
                    }},
                    {'response': self.inspection, 'usage': {
                        'prompt_tokens': 150, 'completion_tokens': 43, 'total_tokens': 193,
                    }},
                ]) as run:
            call_command('diagnose_swifts', str(self.swift.pk), '--show-raw',
                         stdout=out, stderr=errors)
        fetch.assert_called_once()
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[1]['image'], list(self.jpeg))
        current = json.loads(next(line.split(' raw: ', 1)[1]
                                  for line in out.getvalue().splitlines()
                                  if line.startswith('Current classifier raw: ')))
        independent = json.loads(next(line.split(' raw: ', 1)[1]
                                      for line in out.getvalue().splitlines()
                                      if line.startswith('Independent inspection raw: ')))
        self.assertEqual(current['response_type'], 'str')
        self.assertEqual(current['response_chars'], len(prose))
        self.assertEqual(current['preview'], prose)
        self.assertFalse(current['truncated'])
        self.assertEqual(current['usage']['completion_tokens'], 257)
        self.assertEqual(independent['response_type'], 'dict')
        self.assertEqual(json.loads(independent['preview']), self.inspection)
        self.assertEqual(independent['usage']['completion_tokens'], 43)
        self.assertIn('Current classifier failed:', errors.getvalue())
        self.assertIn('Independent inspection: ', out.getvalue())
        self.assertEqual(self._saved_state(), before)

    def test_raw_previews_are_bounded_redacted_and_terminal_controls_are_escaped(self):
        token = 'SENSITIVE-CLOUDFLARE-TOKEN-XYZ'
        # The second occurrence crosses the preview boundary. Redacting after
        # truncation could expose the first characters of the configured token.
        prose = (
            'Bearer fake-secret-123\n\x1b[31m '
            + token + ' ' + 'x' * 1100 + token + '\U0001f600' * 3000)
        out, errors = StringIO(), StringIO()
        with override_settings(CLOUDFLARE_API_TOKEN=token), \
                patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                patch('moderation.cloudflare_ai._run', return_value={
                    'response': prose,
                    'usage': {
                        'prompt_tokens': token, 'completion_tokens': 257,
                        'total_tokens': 'x' * 10000,
                    },
                }) as run:
            call_command('diagnose_swifts', str(self.swift.pk), '--show-raw',
                         stdout=out, stderr=errors)
        self.assertEqual(run.call_count, 2)
        output = out.getvalue() + errors.getvalue()
        self.assertNotIn('fake-secret-123', output)
        self.assertNotIn('SENSITIVE', output)
        self.assertNotIn('\x1b', output)
        self.assertIn(r'\u001b', out.getvalue())
        raw_lines = [line for line in out.getvalue().splitlines() if ' raw: ' in line]
        self.assertEqual(len(raw_lines), 2)
        for line in raw_lines:
            summary = json.loads(line.split(' raw: ', 1)[1])
            self.assertLessEqual(len(json.dumps(summary['preview'], ensure_ascii=True)), 1200)
            self.assertLess(len(line), 1800)
            self.assertEqual(summary['response_chars'], len(prose))
            self.assertTrue(summary['truncated'])
            self.assertIn('[redacted]', summary['preview'])
            self.assertEqual(summary['usage'], {
                'prompt_tokens': None, 'completion_tokens': 257, 'total_tokens': None,
            })
        self.assertIn('Current classifier failed:', errors.getvalue())
        self.assertIn('Independent inspection failed:', errors.getvalue())

    def test_raw_model_prose_is_not_printed_without_show_raw(self):
        out, errors = StringIO(), StringIO()
        prose = 'RAW MODEL PROSE THAT SHOULD STAY HIDDEN BY DEFAULT'
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                patch('moderation.cloudflare_ai._run',
                      return_value={'response': prose}) as run:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=out, stderr=errors)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn(' raw: ', out.getvalue())
        self.assertNotIn(prose, out.getvalue() + errors.getvalue())

    def test_show_raw_reports_missing_or_non_object_responses_without_crashing(self):
        for response, type_name in ((None, 'NoneType'), ([], 'list')):
            with self.subTest(response=response):
                out, errors = StringIO(), StringIO()
                with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg), \
                        patch('moderation.cloudflare_ai._run',
                              return_value={'response': response}) as run:
                    call_command('diagnose_swifts', str(self.swift.pk), '--show-raw',
                                 stdout=out, stderr=errors)
                self.assertEqual(run.call_count, 2)
                raw_lines = [line for line in out.getvalue().splitlines() if ' raw: ' in line]
                self.assertEqual(len(raw_lines), 2)
                for line in raw_lines:
                    summary = json.loads(line.split(' raw: ', 1)[1])
                    self.assertEqual(summary['response_type'], type_name)
                    self.assertEqual(summary['preview'], json.dumps(response))
                    self.assertEqual(summary['usage'], {
                        'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None,
                    })
                self.assertIn('Current classifier failed:', errors.getvalue())
                self.assertIn('Independent inspection failed:', errors.getvalue())

    @override_settings(CLOUDFLARE_AI_VISION_MODEL='@cf/meta/llama-4-scout-17b-16e-instruct')
    def test_scout_inspections_share_exact_jpeg_with_distinct_prompts_and_schemas(self):
        from moderation import cloudflare_ai
        from swifts.management.commands.diagnose_swifts import INSPECTION_PROMPT

        before = self._saved_state()
        out, errors = StringIO(), StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg) as fetch, \
                patch('moderation.cloudflare_ai._run', side_effect=[
                    {'response': self.vision}, {'response': self.inspection},
                ]) as run:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=out, stderr=errors)
        fetch.assert_called_once()
        self.assertEqual(run.call_count, 2)
        current, independent = [call.args[1] for call in run.call_args_list]
        image_urls = []
        for call in run.call_args_list:
            self.assertEqual(call.args[0], '@cf/meta/llama-4-scout-17b-16e-instruct')
            payload = call.args[1]
            self.assertNotIn('image', payload)
            self.assertNotIn('prompt', payload)
            self.assertFalse(payload['stream'])
            image_part = next(part for part in payload['messages'][1]['content']
                              if part['type'] == 'image_url')
            image_url = image_part['image_url']['url']
            self.assertTrue(image_url.startswith('data:image/jpeg;base64,'))
            self.assertEqual(base64.b64decode(image_url.split(',', 1)[1]), self.jpeg)
            image_urls.append(image_url)
        self.assertEqual(image_urls[0], image_urls[1])
        self.assertEqual(current['messages'][0]['content'], cloudflare_ai.VISION_PROMPT)
        self.assertEqual(independent['messages'][0]['content'], INSPECTION_PROMPT)
        self.assertNotEqual(current['messages'][0]['content'],
                            independent['messages'][0]['content'])
        self.assertEqual(set(current['response_format']['json_schema']['required']), {
            'description', 'evidence', 'unsafe', 'categories', 'confidence',
        })
        self.assertEqual(set(independent['response_format']['json_schema']['required']), {
            'description', 'visible_violation', 'evidence',
        })
        self.assertEqual(independent['max_tokens'], 512)
        self.assertIn('Current classifier: ', out.getvalue())
        self.assertIn('Independent inspection: ', out.getvalue())
        self.assertEqual(errors.getvalue(), '')
        self.assertEqual(self._saved_state(), before)
