"""How many web worker processes run, and whether Redis and the database can carry them (review finding M20)."""
import os

from django.core.exceptions import ImproperlyConfigured

#: The Procfile's `${WEB_CONCURRENCY:-2}`. The two must agree; `core.test_capacity` holds them together.
DEFAULT_WEB_WORKERS = 2


def web_workers(environ=os.environ):
    """The uvicorn worker count the Procfile starts, read from the same variable."""
    raw = (environ.get('WEB_CONCURRENCY') or '').strip()
    if not raw:
        return DEFAULT_WEB_WORKERS
    try:
        workers = int(raw)
    except ValueError:
        raise ImproperlyConfigured(f'WEB_CONCURRENCY must be a whole number, not {raw!r}.') from None
    if workers < 1:
        raise ImproperlyConfigured('WEB_CONCURRENCY must be at least 1.')
    return workers


def connection_budget(workers, pool_max_size, reserve):
    """Connections one web replica's pools can hold at once, plus the reserve kept for everything else."""
    return workers * pool_max_size + reserve


def require_worker_capacity(*, debug, workers, redis_url, pool_enabled, pool_max_size, reserve, max_connections):
    """Refuse to serve production traffic with more workers than Redis or the database can carry."""
    if debug:
        return
    if workers > 1 and not redis_url:
        raise ImproperlyConfigured(
            f'REDIS_URL is required to run {workers} web workers: channel groups, throttles and the '
            'cache have to be shared by every process.'
        )
    if pool_enabled and max_connections:
        needed = connection_budget(workers, pool_max_size, reserve)
        if needed > max_connections:
            raise ImproperlyConfigured(
                f'{workers} web workers x DB_POOL_MAX_SIZE {pool_max_size} + DB_CONNECTION_RESERVE {reserve} '
                f'= {needed} connections, more than DB_MAX_CONNECTIONS {max_connections}. '
                'Lower WEB_CONCURRENCY or DB_POOL_MAX_SIZE.'
            )
