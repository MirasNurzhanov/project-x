"""Pure helpers for load testing; importing this module never sends requests."""
import base64
import csv
import json
import math
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Account:
    email: str
    password: str
    latitude: float
    longitude: float


def _origin(value):
    url = urlsplit(value)
    if (url.scheme not in ('http', 'https') or not url.hostname
        or url.username is not None or url.password is not None
        or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('Use an HTTP(S) origin without credentials, path or query.')
    return url.scheme, url.hostname.lower(), url.port or (443 if url.scheme == 'https' else 80)


def validate_target(host, staging_origin, allow_writes):
    if allow_writes != 'staging-only':
        raise ValueError('Set APPROX_LOAD_ALLOW_WRITES=staging-only for this writing scenario.')
    if not isinstance(host, str) or not host or not staging_origin:
        raise ValueError('Supply --host and APPROX_LOAD_STAGING_ORIGIN explicitly.')
    if _origin(host) != _origin(staging_origin):
        raise ValueError('--host must match APPROX_LOAD_STAGING_ORIGIN exactly.')


def load_accounts(path, *, shard_index=0, shard_count=1):
    if shard_count < 1 or not 0 <= shard_index < shard_count:
        raise ValueError('Account shard index must be in [0, shard_count).')
    if not path:
        raise ValueError('Set APPROX_LOAD_ACCOUNTS to a private synthetic-account CSV.')
    accounts, seen = [], set()
    with open(path, newline='', encoding='utf-8-sig') as handle:
        rows = csv.DictReader(handle)
        if not {'email', 'password', 'latitude', 'longitude'}.issubset(rows.fieldnames or ()):
            raise ValueError('CSV needs email,password,latitude,longitude columns.')
        for index, row in enumerate(rows):
            email = (row['email'] or '').strip().lower()
            password = row['password'] or ''
            if '@' not in email or not email.rsplit('@', 1)[1].endswith('.invalid'):
                raise ValueError(f'CSV row {index + 2}: use synthetic .invalid email addresses.')
            if email in seen:
                raise ValueError(f'CSV row {index + 2}: duplicate account.')
            if not password:
                raise ValueError(f'CSV row {index + 2}: missing password.')
            try:
                lat, lon = float(row['latitude']), float(row['longitude'])
            except (ValueError, TypeError) as exc:
                raise ValueError(f'CSV row {index + 2}: invalid coordinates.') from exc
            if (not math.isfinite(lat) or not math.isfinite(lon)
                or not (-89.99 < lat < 89.99 and -179.99 < lon < 179.99)):
                raise ValueError(f'CSV row {index + 2}: coordinates outside the usable range.')
            seen.add(email)
            if index % shard_count == shard_index:
                accounts.append(Account(email, password, lat, lon))
    if not accounts:
        raise ValueError('Account shard is empty.')
    return accounts


class AccountPool:
    """One lease per account per process; sharding separates worker processes."""

    def __init__(self, accounts):
        self._available = deque(accounts)
        if len({account.email for account in accounts}) != len(accounts):
            raise ValueError('Duplicate account in pool.')
        self._leased = set()

    def acquire(self):
        if not self._available:
            raise ValueError('Account shard exhausted; never reuse a concurrently active account.')
        account = self._available.popleft()
        self._leased.add(account)
        return account

    def release(self, account):
        if account in self._leased:
            self._leased.remove(account)
            self._available.append(account)


@dataclass
class PollClock:
    period: float
    next_due: float

    def __post_init__(self):
        if not math.isfinite(self.period) or self.period <= 0 or not math.isfinite(self.next_due):
            raise ValueError('Polling clocks need a finite positive period and deadline.')

    def complete(self, now):
        """Advance to a future slot; missed slots never trigger catch-up bursts."""
        if now < self.next_due:
            raise ValueError('A polling slot cannot complete before its deadline.')
        skipped = int((now - self.next_due) // self.period)
        self.next_due += (skipped + 1) * self.period
        return skipped


def refresh_delay(access_token, now):
    """Read expiry to schedule refresh; JWT verification remains on the server."""
    try:
        encoded = access_token.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        expires = payload['exp']
        if type(expires) not in (float, int) or not math.isfinite(expires) or expires <= now:
            raise ValueError('Invalid or expired access token')
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError('Invalid or expired access token') from exc
    return max(1, expires - now - 60)
