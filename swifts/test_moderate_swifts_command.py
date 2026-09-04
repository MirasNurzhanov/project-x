"""The `moderate_swifts` worker, as a supervised process rather than a person.

`Procfile`'s `worker:` line runs `moderate_swifts --loop` under a supervisor
whose entire job is to restart anything that exits. That makes "exit when there
is nothing to do" - the obviously right behaviour for somebody running this by
hand - a crash loop: the process dies instantly, Railway restarts it, about
once a second, forever.

And it is the *default* path, not an edge case: `SWIFT_AI_MODERATION_ENABLED`
is off unless somebody turns it on.
"""
from io import StringIO
from unittest.mock import call, patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings


class SinglePassTests(SimpleTestCase):
    """No --loop: a human typed this, and exiting is the right answer."""

    @override_settings(SWIFT_AI_MODERATION_ENABLED=False)
    def test_it_reports_and_returns_when_disabled(self):
        out = StringIO()
        with patch('swifts.ai_moderation.sweep') as sweep:
            call_command('moderate_swifts', stdout=out)
        sweep.assert_not_called()
        self.assertIn('nothing to do', out.getvalue())

    @override_settings(SWIFT_AI_MODERATION_ENABLED=True)
    def test_it_sweeps_once_when_enabled(self):
        with patch('swifts.ai_moderation.sweep', return_value=0) as sweep:
            call_command('moderate_swifts', stdout=StringIO())
        sweep.assert_called_once()


class LoopTests(SimpleTestCase):
    """--loop: a supervisor is watching, and exiting is a restart."""

    def run_loop(self, ticks):
        """Run the loop for `ticks` iterations, then break out of it.

        `time.sleep` is the seam: the loop is `while True`, so the only way to
        stop it deterministically is to interrupt the thing it waits on.
        Raising KeyboardInterrupt is what the command already handles, so this
        exercises the real exit path rather than a test-only one.
        """
        out = StringIO()
        calls = {'n': 0}

        def fake_sleep(_seconds):
            calls['n'] += 1
            if calls['n'] >= ticks:
                raise KeyboardInterrupt
        with patch('swifts.management.commands.moderate_swifts.time.sleep', fake_sleep):
            call_command('moderate_swifts', '--loop', '--interval', '0', stdout=out)
        return out.getvalue(), calls['n']

    @override_settings(SWIFT_AI_MODERATION_ENABLED=False)
    def test_it_idles_instead_of_exiting_when_disabled(self):
        """The regression this file exists for.

        Before the fix this returned immediately, which under `worker:` is an
        exit-0 crash loop restarting roughly once a second.
        """
        with patch('swifts.ai_moderation.sweep') as sweep:
            output, ticks = self.run_loop(3)

        self.assertEqual(ticks, 3, 'the loop exited early instead of idling')
        sweep.assert_not_called()
        self.assertIn('idling', output)
        self.assertIn('Stopped.', output)

    @override_settings(SWIFT_AI_MODERATION_ENABLED=False)
    def test_the_idle_notice_is_logged_once_not_every_tick(self):
        """A worker logging every interval about a feature nobody turned on is
        a log bill and nothing else."""
        with patch('swifts.ai_moderation.sweep'):
            output, _ = self.run_loop(5)
        self.assertEqual(output.count('idling'), 1, output)

    @override_settings(SWIFT_AI_MODERATION_ENABLED=True)
    def test_it_sweeps_every_tick_when_enabled(self):
        with patch('swifts.ai_moderation.sweep', return_value=0) as sweep:
            _, ticks = self.run_loop(3)
        self.assertEqual(sweep.call_count, ticks)

    def test_the_switch_is_read_every_tick_not_once_at_startup(self):
        """Turning moderation on must not require redeploying the worker.

        The setting is read inside the loop, so a variable change is picked up
        on the next tick rather than needing the process replaced.
        """
        states = iter([False, False, True, True])
        with patch('swifts.ai_moderation.enabled', lambda: next(states, True)), \
                patch('swifts.ai_moderation.sweep', return_value=0) as sweep:
            _, ticks = self.run_loop(4)
        self.assertEqual(ticks, 4)
        self.assertEqual(sweep.call_count, 2, 'the switch was only read at startup')


@override_settings(SWIFT_AI_MODERATION_ENABLED=True)
class ExplicitRetryTests(SimpleTestCase):
    def test_only_returned_job_ids_are_run(self):
        with patch('swifts.ai_moderation.retry_failed', return_value=[701, 702]) as retry, \
                patch('swifts.ai_moderation.run', return_value=None) as run, \
                patch('swifts.ai_moderation.sweep') as sweep:
            call_command('moderate_swifts', '--retry-failed', '22', '23',
                         stdout=StringIO())
        retry.assert_called_once_with([22, 23])
        self.assertEqual(run.call_args_list, [call(701), call(702)])
        sweep.assert_not_called()

    def test_ineligible_ids_do_not_run_anything(self):
        out = StringIO()
        with patch('swifts.ai_moderation.retry_failed', return_value=[]), \
                patch('swifts.ai_moderation.run') as run, \
                patch('swifts.ai_moderation.sweep') as sweep:
            call_command('moderate_swifts', '--retry-failed', '22', stdout=out)
        run.assert_not_called()
        sweep.assert_not_called()
        self.assertIn('Requeued 0 failed job(s); skipped 1', out.getvalue())

    def test_retry_rejects_loop_status_and_nonpositive_ids_before_mutation(self):
        for args in (
            ('--retry-failed', '22', '--loop'),
            ('--retry-failed', '22', '--status'),
            ('--retry-failed', '0'),
            ('--retry-failed', '-1'),
        ):
            with self.subTest(args=args), \
                    patch('swifts.ai_moderation.retry_failed') as retry, \
                    patch('swifts.ai_moderation.run') as run:
                with self.assertRaises(CommandError):
                    call_command('moderate_swifts', *args, stdout=StringIO())
                retry.assert_not_called()
                run.assert_not_called()

    @override_settings(SWIFT_AI_MODERATION_ENABLED=False)
    def test_disabled_retry_leaves_failed_jobs_alone(self):
        with patch('swifts.ai_moderation.retry_failed') as retry:
            with self.assertRaises(CommandError):
                call_command('moderate_swifts', '--retry-failed', '22',
                             stdout=StringIO())
        retry.assert_not_called()
