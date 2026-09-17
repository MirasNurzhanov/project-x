from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from importlib import import_module
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command, CommandError
from django.db import connection, connections, close_old_connections
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from swifts import ai_moderation
from swifts.models import SwiftModerationJob
from swifts.testing import make_swift, make_user
from users.models import User


class DeploymentTests(SimpleTestCase):
    def test_web_boot_only_starts_application(self):
        entries = dict(line.split(': ', 1) for line in (Path(settings.BASE_DIR) / 'Procfile').read_text().splitlines())
        self.assertTrue(entries['web'].startswith('python -c "import core.asgi" && uvicorn core.asgi:application'))
        self.assertNotIn('manage.py', entries['web'])
        self.assertIn('manage.py migrate', entries['release'])
        self.assertNotIn('fix_age_tiers', '\n'.join(entries.values()))

    @override_settings(SWIFT_AI_MODERATION_SYNC=False)
    def test_upload_burst_starts_no_threads_or_inline_work(self):
        with patch('threading.Thread.start') as start, patch('swifts.ai_moderation.run') as run, \
                patch('swifts.ai_moderation.logger'):
            for job_id in range(1000):
                ai_moderation.start(job_id)
        start.assert_not_called()
        run.assert_not_called()

    def test_worker_connection_cleanup_on_success_and_failure(self):
        for error in (None, RuntimeError('worker failed')):
            with patch('django.db.connection') as conn, patch('django.db.close_old_connections') as old, \
                    patch('django.db.connections.close_all') as close, \
                    patch('swifts.ai_moderation._run', side_effect=error, return_value='done'):
                conn.in_atomic_block = False
                if error:
                    with self.assertRaises(RuntimeError):
                        ai_moderation.run(1)
                else:
                    self.assertEqual(ai_moderation.run(1), 'done')
                old.assert_called_once()
                close.assert_called_once()

    def test_invalid_polling_and_batch_limits_refused_before_work(self):
        for args in (('--interval', '0'), ('--limit', '0'), ('--limit', '1001')):
            with patch('swifts.ai_moderation.sweep') as sweep, self.assertRaises(CommandError):
                call_command('moderate_swifts', *args, stdout=StringIO())
            sweep.assert_not_called()


class DataMigrationTests(TestCase):
    def test_age_repair_uses_historical_models_is_idempotent_and_preserves_missing_dob(self):
        migration = import_module('users.migrations.0038_recalculate_age_tiers')
        historical = MigrationLoader(connection).project_state(
            [('users', '0037_user_delete_after_user_deletion_requested_at_and_more')]).apps
        for name, birthday, tier in (
            ('adult', date(2008, 9, 14), 'minor'),
            ('minor', date(2008, 9, 15), 'adult'),
            ('unknown', None, 'adult'),
        ):
            user = User.objects.create_user(username=name, email=f'{name}@example.test', date_of_birth=birthday)
            User.objects.filter(pk=user.pk).update(age_tier=tier)
        with patch.object(migration.timezone, 'localdate', return_value=date(2026, 9, 14)):
            for _ in range(2):
                with self.assertNumQueries(2):
                    migration.recalculate(historical, SimpleNamespace(connection=connection))
        self.assertEqual(dict(User.objects.values_list('username', 'age_tier')),
                         {'adult': 'adult', 'minor': 'minor', 'unknown': 'adult'})

    def test_stale_sweep_snapshot_cannot_reset_a_new_live_claim(self):
        swift = make_swift(make_user('worker-race'))
        job = SwiftModerationJob.objects.create(swift=swift, state=SwiftModerationJob.State.RUNNING)
        # The other worker reclaimed after this sweeper selected its stale IDs.
        SwiftModerationJob.objects.filter(pk=job.pk).update(updated_at=timezone.now())
        with patch('swifts.ai_moderation.due_jobs', return_value=[job.pk]):
            self.assertEqual(ai_moderation.sweep(), 0)
        job.refresh_from_db()
        self.assertEqual(job.state, SwiftModerationJob.State.RUNNING)

    def test_retry_cannot_be_claimed_before_its_backoff(self):
        job = SwiftModerationJob.objects.create(swift=make_swift(make_user('backoff')),
                next_attempt_at=timezone.now() + timedelta(minutes=1))
        self.assertIsNone(ai_moderation.claim(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.attempts, 0)


class ModerationClaimConcurrencyTests(TransactionTestCase):
    def test_postgres_concurrent_claim_has_one_winner(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires disposable PostgreSQL integration settings.')
        job = SwiftModerationJob.objects.create(swift=make_swift(make_user('claims')))
        barrier = Barrier(4)
        def claim_once(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return ai_moderation.claim(job.pk) is not None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(claim_once, range(4))), 1)
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1)
