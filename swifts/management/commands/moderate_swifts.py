"""Run the AI moderation jobs that are waiting.

    python manage.py moderate_swifts            # one pass, then exit
    python manage.py moderate_swifts --loop     # keep going, for a worker dyno
    python manage.py moderate_swifts --status   # what is configured, no secrets

Why this worker is required
---------------------------
Web requests only persist QUEUED jobs. This process runs them sequentially,
with no upload-created threads. Run a supervised worker whenever moderation
is enabled; without one new Swifts remain safely PENDING but never finish.
Queued jobs survive restarts; stale running jobs and due retries are recovered.

Run it either way:

  * `--loop` as a second Railway service (or a `worker:` Procfile entry), which
    is the lower-latency option and the one worth having once volume justifies
    a second container;
  * or a scheduled `python manage.py moderate_swifts` every few minutes, which
    needs no new service at all.

Both are safe to run alongside the web process and alongside each other:
`ai_moderation.claim` is a conditional UPDATE, so two sweepers racing for one
job produce one winner and one no-op rather than two inference bills.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from swifts import ai_moderation


class Command(BaseCommand):
    help = 'Run queued AI moderation jobs for Swifts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--loop', action='store_true',
            help='Keep sweeping until interrupted, instead of one pass.')
        parser.add_argument(
            '--interval', type=int, default=20,
            help='Seconds between passes when looping (default 20).')
        parser.add_argument(
            '--limit', type=int, default=50,
            help='Most jobs to claim in one pass (default 50).')
        parser.add_argument(
            '--status', action='store_true',
            help='Print what is configured and exit. Prints no secrets.')

        parser.add_argument(
            '--retry-failed', nargs='+', type=int, metavar='SWIFT_ID',
            help='Retry only failed jobs for these Swift IDs, then exit.')

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 1000 or not 1 <= options['interval'] <= 3600:
            raise CommandError('Use --limit 1..1000 and --interval 1..3600 seconds.')
        swift_ids = options['retry_failed']
        if swift_ids is not None:
            if options['loop'] or options['status']:
                raise CommandError('--retry-failed cannot be combined with --loop or --status.')
            if any(swift_id <= 0 for swift_id in swift_ids):
                raise CommandError('--retry-failed requires positive Swift IDs.')
            if not ai_moderation.enabled():
                raise CommandError('Enable SWIFT_AI_MODERATION_ENABLED before retrying failed jobs.')
            job_ids = ai_moderation.retry_failed(swift_ids)
            self.stdout.write(
                f'Requeued {len(job_ids)} failed job(s); '
                f'skipped {len(set(swift_ids)) - len(job_ids)} Swift ID(s).')
            for job_id in job_ids:
                job = ai_moderation.run(job_id)
                if job is not None:
                    self.stdout.write(f'Swift {job.swift_id}: {job.state}')
            return

        if options['status']:
            self._status()
            return

        # One pass, by hand. Exiting when the feature is off is right here -
        # somebody typed this and wants to be told nothing happened.
        if not options['loop']:
            if not ai_moderation.enabled():
                self.stdout.write(self.style.WARNING(
                    'SWIFT_AI_MODERATION_ENABLED is off - nothing to do. '
                    'Existing queued jobs are left alone.'))
                return
            self._pass(options['limit'])
            return

        # -- --loop: a supervised worker process ------------------------------
        #
        # The enabled() check moved *inside* the loop, and that is the whole
        # reason this branch exists separately.
        #
        # This runs as `worker:` in the Procfile, under a supervisor whose job
        # is to restart anything that exits. So "the feature is off, return" -
        # correct for a human at a terminal - becomes an exit-0 crash loop: the
        # process dies instantly, Railway restarts it, forever, about once a
        # second. `SWIFT_AI_MODERATION_ENABLED` is off by default, so that is
        # the worker's *default* behaviour rather than an edge case.
        #
        # Idling means a dynamically overridden setting can change without
        # restarting the worker, and a disabled feature costs one
        # settings read per interval instead of a restart.
        self.stdout.write(f'Sweeping every {options["interval"]}s. Ctrl-C to stop.')
        idle_logged = False
        try:
            while True:
                if ai_moderation.enabled():
                    idle_logged = False
                    self._pass(options['limit'])
                elif not idle_logged:
                    # Once per transition, not once per tick: a worker logging
                    # every interval about a feature nobody turned on is a log
                    # bill and nothing else.
                    self.stdout.write(self.style.WARNING(
                        'SWIFT_AI_MODERATION_ENABLED is off - idling. '
                        'Enable it and restart after changing environment variables.'))
                    idle_logged = True
                time.sleep(options['interval'])
        except KeyboardInterrupt:
            self.stdout.write('\nStopped.')

    def _pass(self, limit):
        from django.db import connection, connections
        try:
            done = ai_moderation.sweep(limit=limit)
        finally:
            # An empty sweep still opened a connection; return it before idle.
            # Do not close a transaction owned by an explicit synchronous caller.
            if not connection.in_atomic_block:
                connections.close_all()
        if done:
            self.stdout.write(self.style.SUCCESS(f'Ran {done} job(s).'))

    def _status(self):
        from moderation import cloudflare_ai

        from swifts.models import SwiftModerationJob

        # `configuration_summary` reports whether the token is *set* and never
        # any part of it. There is deliberately no "first four characters"
        # convenience here: that is how a credential ends up in a screenshot.
        for key, value in cloudflare_ai.configuration_summary().items():
            self.stdout.write(f'{key:24} {value}')

        counts = {
            state.label: SwiftModerationJob.objects.filter(state=state).count()
            for state in SwiftModerationJob.State
        }
        self.stdout.write('')
        self.stdout.write('Jobs:')
        for label, count in counts.items():
            self.stdout.write(f'  {label:24} {count}')
