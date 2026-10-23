"""Give a city or district zone the boundary it is missing.

New accounts start as a city zone with no boundary, and the location report
only reads boundaries that are already stored — it never asks the map service
for one. So the first people in a city Approx has not seen sat on the 500m
circle that `proximity.views` falls back to, with nothing telling them so.

Two ways in:

  schedule   from the location report. Never blocks it: the fetch runs on a
             small background pool after the position is saved, at most once
             per user per `REPAIR_COOLDOWN_S`, so a failing map service is
             retried later rather than hammered. Until it succeeds the request
             is answered from the circle, exactly as before.
  repair     the same fetch, inline, for the Retry button on the map.

Either way the boundary comes from the user's stored position, the same way
`users.zones.follow_location` picks one, and it is attached only while the
zone still has none — a boundary the user chose in the meantime wins.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections, connection

from .models import User

logger = logging.getLogger(__name__)

#: How long after one attempt the location report may start another.
REPAIR_COOLDOWN_S = 10 * 60

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='zone-repair')


def needs_boundary(user):
    return user.zone_type in User.POLYGON_ZONE_TYPES and user.zone_region_id is None


def repair(user):
    """Fetch and attach the boundary for `user`'s zone. Returns the Region or None."""
    from regions import places, upstream

    from . import zones

    if not needs_boundary(user):
        return user.zone_region
    try:
        place = places.snapshot_for(user, allow_upstream=True, force=True)
    except upstream.UpstreamError as exc:
        logger.warning('zone_boundary_repair_failed', extra={'error_type': type(exc).__name__})
        return None
    region = zones._tier_matching(place, user.zone_type) if place is not None else None
    if region is None:
        logger.warning('zone_boundary_repair_unresolved', extra={'zone_type': user.zone_type})
        return None
    attached = User.objects.filter(pk=user.pk, zone_region__isnull=True).update(
        zone_region=region, zone_follow_stale=False,
    )
    if attached:
        user.zone_region = region
        user.zone_follow_stale = False
    return region if attached else None


def schedule(user):
    """Start a background repair for `user` if one is due. Returns whether it started."""
    mode = getattr(settings, 'ZONE_BOUNDARY_REPAIR', 'background')
    if mode == 'off' or not needs_boundary(user):
        return False
    if not cache.add(f'zone-repair:{user.pk}', '1', timeout=REPAIR_COOLDOWN_S):
        return False
    if mode == 'inline':
        repair(user)
    else:
        _executor.submit(_repair_in_background, user.pk)
    return True


def _repair_in_background(user_id):
    close_old_connections()
    try:
        user = User.objects.filter(pk=user_id).first()
        if user is not None:
            repair(user)
    except Exception as exc:  # noqa: BLE001
        logger.warning('zone_boundary_repair_failed', extra={'error_type': type(exc).__name__})
    finally:
        connection.close()
