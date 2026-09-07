"""Bounded foreground polling scenario; see docs/scaling-railway.md before use.

Run from project-x with ``python -m locust -f loadtest/locustfile.py``.
Every concurrent user leases a different synthetic staging account.
"""
import logging
import os
import random
import time
from collections import Counter

import gevent
from locust import HttpUser, events, task
from locust.exception import StopUser
from locust.runners import MasterRunner, WorkerRunner

from loadtest.scenario import AccountPool, PollClock, load_accounts, refresh_delay, validate_target


LOG = logging.getLogger(__name__)
_accounts = None
_skipped_polls = Counter()


def _check_target(host):
    validate_target(host, os.environ.get('APPROX_LOAD_STAGING_ORIGIN', ''),
                    os.environ.get('APPROX_LOAD_ALLOW_WRITES', ''))


@events.init.add_listener
def configure_scenario(environment, **kwargs):
    """Fail before spawning users, including with the Locust web UI."""
    global _accounts
    try:
        _check_target(environment.host)
        if getattr(environment.parsed_options, 'processes', None):
            raise ValueError('Automatic --processes is unsupported: use explicit worker shards.')
        if isinstance(environment.runner, MasterRunner):
            return
        is_worker = isinstance(environment.runner, WorkerRunner)
        if is_worker and not all(key in os.environ for key in (
            'APPROX_LOAD_SHARD_INDEX', 'APPROX_LOAD_SHARD_COUNT',
        )):
            raise ValueError('Every distributed worker needs an explicit account shard.')
        accounts = load_accounts(
            os.environ.get('APPROX_LOAD_ACCOUNTS', ''),
            shard_index=int(os.environ.get('APPROX_LOAD_SHARD_INDEX', '0')),
            shard_count=int(os.environ.get('APPROX_LOAD_SHARD_COUNT', '1')),
        )
        requested = getattr(environment.parsed_options, 'num_users', None)
        if not is_worker and requested and requested > len(accounts):
            raise ValueError('Requested user count exceeds the available account shard.')
        _accounts = AccountPool(accounts)
    except (OSError, ValueError) as exc:
        LOG.error('Load scenario configuration rejected: %s', exc)
        raise SystemExit(2) from exc


@events.test_stop.add_listener
def report_skipped_polls(environment, **kwargs):
    LOG.info('Skipped polling slots on this generator: %s', dict(_skipped_polls))
    _skipped_polls.clear()


class ApproxUser(HttpUser):
    # The task sleeps until a clock is due; random weights do not set RPS.
    wait_time = lambda self: 0

    def on_start(self):
        self.account = None
        try:
            # The web UI can change the host after the init event.
            _check_target(self.host)
            if _accounts is None:
                raise ValueError('Account pool was not configured.')
            self.account = _accounts.acquire()
            self.client.headers.update({
                'X-Approx-Presence': 'foreground', 'Accept-Encoding': 'gzip',
            })
            if not self._authenticate('/api/users/login/', {
                'email': self.account.email, 'password': self.account.password,
            }):
                raise StopUser()
            now = time.monotonic()
            # useMapData reports location on each 10s foreground map tick.
            # The separate background location task's 30s cadence is not this cohort.
            periods = {'map': 10, 'badges': 30, 'location': 10}
            if os.environ.get('APPROX_LOAD_CHAT', '0') == '1':
                periods['chat'] = 15
            self.clocks = {name: PollClock(period, now + random.uniform(0, period))
                           for name, period in periods.items()}
        except ValueError as exc:
            self.environment.process_exit_code = 2
            LOG.error('Stopping load run: %s', exc)
            gevent.spawn(self.environment.runner.quit)
            raise StopUser() from exc

    def on_stop(self):
        if self.account is not None:
            _accounts.release(self.account)
            self.account = None

    def _authenticate(self, path, payload):
        with self.client.post(path, json=payload, name=f'POST {path}',
                              catch_response=True, allow_redirects=False, timeout=15) as response:
            if response.status_code != 200:
                response.failure(f'Expected authentication HTTP 200; got {response.status_code}')
                return False
            try:
                data = response.json()
                access = data['access']
                delay = refresh_delay(access, time.time())
                self.refresh_token = data.get('refresh', getattr(self, 'refresh_token', None))
                if not isinstance(self.refresh_token, str) or not self.refresh_token:
                    raise ValueError('No refresh token')
            except (KeyError, TypeError, ValueError):
                response.failure('Invalid authentication token response')
                return False
            self.client.headers['Authorization'] = f'Bearer {access}'
            self.refresh_at = time.monotonic() + delay
            return True

    def _poll(self, method, path, **kwargs):
        with self.client.request(method, path, name=f'{method} {path}',
                                 catch_response=True, allow_redirects=False,
                                 timeout=15, **kwargs) as response:
            if response.status_code != 200:
                response.failure(f'Expected HTTP 200; got {response.status_code}')
                if response.status_code == 401:
                    raise StopUser()  # A revoked session is not a hidden login loop.
                return
            try:
                data = response.json()
                if path == '/api/notifications/badges/':
                    if not isinstance(data, dict) or not all(
                        type(data.get(key)) is int and data[key] >= 0 for key in ('chat', 'alerts')
                    ):
                        raise ValueError('Invalid badge response')
            except ValueError:
                response.failure('Expected valid API JSON response')

    def _position(self):
        # Small movement around each fixture position, not one global hot point.
        return {
            'latitude': f'{self.account.latitude + random.uniform(-0.0001, 0.0001):.8f}',
            'longitude': f'{self.account.longitude + random.uniform(-0.0001, 0.0001):.8f}',
            'accuracy': 10, 'speed': 0.5,
        }

    @task
    def next_poll(self):
        name, clock = min(self.clocks.items(), key=lambda item: item[1].next_due)
        gevent.sleep(max(0, min(clock.next_due, self.refresh_at) - time.monotonic()))
        if time.monotonic() >= self.refresh_at:
            if not self._authenticate('/api/users/token/refresh/', {'refresh': self.refresh_token}):
                raise StopUser()
            return
        if name == 'map':
            self._poll('GET', '/api/map/nearby/')
        elif name == 'badges':
            self._poll('GET', '/api/notifications/badges/')
        elif name == 'location':
            self._poll('PATCH', '/api/proximity/location/', json=self._position())
        elif name == 'chat':
            # Intentional stress profile: map polling also stays active.
            self._poll('GET', '/api/clubs/')
            self._poll('GET', '/api/clubs/?joined=true')
            self._poll('GET', '/api/clubs/my-location/')
        _skipped_polls[name] += clock.complete(time.monotonic())
