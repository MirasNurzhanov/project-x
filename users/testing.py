"""Fixtures shared across the test suites, kept out of any one app's tests.

`share_location` is here because being on the map takes a *fresh position*, not
just a setting: `users.visibility.fresh_position_q` withholds a live setting
over a coordinate nobody has refreshed for hours, so a test about zone reach,
blocking or presence has to stamp one before it can measure anything else —
otherwise it silently measures a stale row.

The sharing default itself is now on (`ghost_mode=False`), so this no longer
has to flip a switch. It stays because the freshness half is still a
precondition, and because every caller already reads as "put this person on the
map", which is what it does.
"""
from django.utils import timezone


def share_location(user, mode=None, reported_at=None):
    """Put `user` on the map, with a position fresh enough to publish.

    `mode` is accepted and ignored. It used to select Live or Approximate, and
    precision is not the sharer's to choose any more — a friend sees the
    precise point and everybody else a ~500m cell, decided per viewer at read
    time by `users.visibility.coordinates_for`. Kept in the signature so the
    many existing call sites that pass it still read sensibly; a test that
    wants to assert on precision should assert on what a *viewer* receives.

    Pass `reported_at` to build a stale position on purpose.
    """
    user.ghost_mode = False
    user.location_updated_at = reported_at or timezone.now()
    # "On the map" includes non-friends' maps, which since the tier rework
    # (users/0039) takes a check-in in the current session. Tests about the
    # check-in itself — and about friends being visible without one — build
    # their users without this helper, or end the check-in explicitly.
    fields = ['ghost_mode', 'location_updated_at']
    if user.discovery_mode == user.DiscoveryMode.DISCOVERABLE:
        fields += user.start_check_in()
    user.save(update_fields=fields)
    return user
