"""Fixtures shared across the test suites, kept out of any one app's tests.

`check_in` is here because the map gate turned something every proximity test
used to get for free into a precondition. A user now defaults to *off* the map
(see users.visibility and proximity.views.MapCheckInView), so a test about zone
reach, blocking or presence has to put its people on the map before it can
measure anything else — otherwise it silently measures the check-in.

Deliberately *not* folded into `User.objects.create_user`. The default belongs
to production and it is off; a factory that quietly checked everyone in would
make the one property App Review cares about invisible to the suite. The tests
that assert the default is off build their users without this — see
proximity/test_check_in.py.
"""
from users.models import User


def check_in(user, minutes=None):
    """Put `user` on the map, exactly as the endpoint would.

    Goes through `User.apply_check_in` rather than setting the columns, so a
    change to what a check-in means reaches every fixture that uses this.
    """
    user.save(update_fields=user.apply_check_in(
        minutes if minutes is not None else User.CHECK_IN_DEFAULT_MINUTES,
    ))
    return user
