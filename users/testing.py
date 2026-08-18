"""Fixtures shared across the test suites, kept out of any one app's tests.

`share_location` is here because the map gate turned something every proximity
test used to get for free into a precondition. `location_sharing` defaults to
PAUSED now (see users.visibility), so a test about zone reach, blocking or
presence has to put its people on the map before it can measure anything else —
otherwise it silently measures the default.

Deliberately *not* folded into `User.objects.create_user`. The default belongs
to production and it is off; a factory that quietly shared everyone's location
would make the one property App Review cares about invisible to the suite. The
tests that assert the default build their users without this — see
proximity/test_map_visibility.py.
"""
from django.utils import timezone

from users.models import User


def share_location(user, mode=None, reported_at=None):
    """Put `user` on the map, at `mode` precision (Live unless told otherwise).

    Stamps `location_updated_at` as well as the setting, because being on the
    map takes both now: a live setting over a coordinate nobody has refreshed
    for hours is exactly the state `users.visibility.fresh_position_q` exists to
    withhold. Pass `reported_at` to build that stale case on purpose.
    """
    user.location_sharing = mode or User.LocationSharing.LIVE
    user.location_updated_at = reported_at or timezone.now()
    user.save(update_fields=['location_sharing', 'location_updated_at'])
    return user
