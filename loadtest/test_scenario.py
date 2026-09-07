"""Offline checks: python -m unittest loadtest.test_scenario (no Locust required)."""
import base64
import csv
import json
import tempfile
import unittest
from pathlib import Path

from loadtest.scenario import Account, AccountPool, PollClock, load_accounts, refresh_delay, validate_target


class ScenarioTests(unittest.TestCase):
    def test_target_requires_opt_in_and_exact_origin(self):
        stage = 'https://staging.example.invalid'
        validate_target(stage + '/', stage + ':443', 'staging-only')
        for host, origin, flag in (
            (stage, stage, ''),
            ('https://production.example.invalid', stage, 'staging-only'),
            ('http://staging.example.invalid', stage, 'staging-only'),
            (stage + '/api', stage, 'staging-only'),
            ('https://secret:password@staging.example.invalid', stage, 'staging-only'),
            (stage + '?target=production', stage, 'staging-only'),
            (None, stage, 'staging-only'),
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_target(host, origin, flag)

    def _csv(self, rows):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'accounts.csv'
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['email', 'password', 'latitude', 'longitude'])
            writer.writerows(rows)
        return path

    def test_account_shards_are_disjoint_and_cover_fixture(self):
        path = self._csv([[f'load-{index}@example.invalid', 'test password', 25.2, 55.2]
                          for index in range(10)])
        shards = [load_accounts(path, shard_index=index, shard_count=3) for index in range(3)]
        self.assertEqual([len(shard) for shard in shards], [4, 3, 3])
        self.assertEqual(len({account.email for shard in shards for account in shard}), 10)

    def test_rejects_duplicate_even_if_in_another_shard(self):
        path = self._csv([['load@example.invalid', 'secret', 25.2, 55.2],
                          ['LOAD@example.invalid', 'secret', 25.2, 55.2]])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            load_accounts(path, shard_index=0, shard_count=2)

    def test_rejects_real_account_or_invalid_position(self):
        for email, lat in [('person@example.com', 25), ('load@example.invalid', 'nan')]:
            with self.subTest(email=email, lat=lat), self.assertRaises(ValueError):
                load_accounts(self._csv([[email, 'secret', lat, 55.2]]))

    def test_account_leases_never_overlap_or_release_twice(self):
        account = Account('load@example.invalid', 'secret', 25.2, 55.2)
        pool = AccountPool([account])
        self.assertEqual(pool.acquire(), account)
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            pool.acquire()
        pool.release(account)
        pool.release(account)
        self.assertEqual(pool.acquire(), account)
        with self.assertRaises(ValueError):
            pool.acquire()

    def test_poll_clock_keeps_period_and_skips_missed_slots(self):
        clock = PollClock(10, 5)
        self.assertEqual(clock.complete(5.2), 0)
        self.assertEqual(clock.next_due, 15)
        self.assertEqual(clock.complete(46), 3)
        self.assertEqual(clock.next_due, 55)

    def test_a_full_minute_preserves_requested_poll_counts(self):
        for period, expected in ((10, 6), (30, 2), (15, 4)):
            clock, count = PollClock(period, 0), 0
            while clock.next_due < 60:
                clock.complete(clock.next_due + 0.1)
                count += 1
            self.assertEqual(count, expected)

    def test_refresh_uses_expiry_and_rejects_bad_tokens(self):
        encoded = base64.urlsafe_b64encode(json.dumps({'exp': 4600}).encode()).decode().rstrip('=')
        token = f'header.{encoded}.signature'
        self.assertEqual(refresh_delay(token, 1000), 3540)
        self.assertEqual(refresh_delay(token, 4599), 1)
        for invalid in (None, 'not-a-jwt', 'header.broken.signature', token):
            with self.subTest(token=invalid), self.assertRaises(ValueError):
                refresh_delay(invalid, 5000)


if __name__ == '__main__':
    unittest.main()
