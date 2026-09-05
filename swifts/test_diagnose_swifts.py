from io import BytesIO, StringIO
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

    def test_inspection_uses_same_middle_frame_and_never_changes_saved_decisions(self):
        before = (
            SwiftVideo.objects.get(pk=self.swift.pk).__dict__.copy(),
            SwiftModerationJob.objects.get(pk=self.job.pk).__dict__.copy(),
            SwiftReport.objects.get(pk=self.report.pk).__dict__.copy(),
        )
        out = StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', return_value=self.jpeg) as fetch, \
                patch('moderation.cloudflare_ai.classify_image', return_value=ModerationResult(
                    safe=False, categories=('other_objectionable',), confidence=0.8)) as classify, \
                patch('moderation.cloudflare_ai._run', return_value={'response': {
                    'description': 'A desk and laptop.', 'visible_violation': 'none',
                    'evidence': 'none',
                }}) as inspect:
            call_command('diagnose_swifts', str(self.swift.pk), stdout=out)
        self.assertEqual(fetch.call_args.args[1], 14_000)
        classify.assert_called_once_with(self.jpeg, at_ms=14_000)
        self.assertEqual(inspect.call_args.args[1]['image'], list(self.jpeg))
        self.assertIn('A desk and laptop.', out.getvalue())
        self.assertIn('JPEG 20x10', out.getvalue())
        for old, model, pk in zip(before, (SwiftVideo, SwiftModerationJob, SwiftReport),
                                  (self.swift.pk, self.job.pk, self.report.pk)):
            current = model.objects.get(pk=pk).__dict__
            self.assertEqual({k: v for k, v in old.items() if k != '_state'},
                             {k: v for k, v in current.items() if k != '_state'})

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
        errors = StringIO()
        with patch('moderation.cloudflare_ai.fetch_frame', side_effect=[
            CloudflareError('Bearer fake-secret-123'), self.jpeg, self.jpeg, self.jpeg, self.jpeg,
        ]) as fetch, patch('moderation.cloudflare_ai.classify_image', return_value=ModerationResult()), \
                patch('moderation.cloudflare_ai._run', return_value={'response': 'A desk.'}):
            call_command('diagnose_swifts', str(self.swift.pk), '--all-frames',
                         stdout=StringIO(), stderr=errors)
        self.assertEqual(fetch.call_count, 5)
        self.assertNotIn('fake-secret-123', errors.getvalue())
        self.assertIn('[redacted]', errors.getvalue())

    def test_unknown_or_invalid_ids_fail_before_any_cloudflare_calls(self):
        for swift_id in ('0', str(self.swift.pk + 100)):
            with self.subTest(swift_id=swift_id), \
                    patch('moderation.cloudflare_ai.fetch_frame') as fetch, \
                    self.assertRaises(CommandError):
                call_command('diagnose_swifts', swift_id, stdout=StringIO())
            fetch.assert_not_called()
