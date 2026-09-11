"""A real device fix is a raw double, and the anchor has to accept one.

The zone screen sends the position it wants checked along with the save. It
takes that position straight from `Location.getCurrentPositionAsync`, which
returns an IEEE-754 double — `43.238015199999996`, 17 significant digits.

The anchor fields mirrored the *column*: `DecimalField(max_digits=11,
decimal_places=8)`. So every save carrying a fix was refused, with the error
under `latitude`/`longitude` — keys the zone screen does not read, which is why
it surfaced as the generic "Could not save. Please try again."

Only city and district saves carry a fix, so only they broke. Radius saves send
no position and were never affected, which is exactly how the bug was reported.

The fixture's own coordinates are clean two-decimal values, which is why the
existing anchor tests all passed throughout.
"""
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User
from users.test_zone_follow import FollowFixture


#: Orbita is (0,0)–(0.2,0.2). These sit inside it and carry the digit count a
#: phone actually produces — the point of the test is the precision, not the
#: place.
DEVICE_LAT = 0.10000000000000009
DEVICE_LON = 0.10000000000000009


class DevicePrecisionAnchorTests(FollowFixture):
    def setUp(self):
        super().setUp()
        self.user = self.make_user(at=self.IN_ORBITA)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def patch(self, **body):
        return self.client.patch(reverse('zone'), body, format='json')

    def test_a_district_saves_from_a_raw_device_fix(self):
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=DEVICE_LAT, longitude=DEVICE_LON, accuracy=12.0,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_type, User.ZoneType.DISTRICT)
        self.assertEqual(self.user.zone_region_id, self.orbita.pk)

    def test_a_city_saves_from_a_raw_device_fix(self):
        response = self.patch(
            zone_type='city', zone_region=self.almaty.pk,
            latitude=DEVICE_LAT, longitude=DEVICE_LON, accuracy=12.0,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_type, User.ZoneType.CITY)
        self.assertEqual(self.user.zone_region_id, self.almaty.pk)

    def test_full_precision_is_still_not_stored(self):
        """The looser field must not become a second way onto the map."""
        self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=DEVICE_LAT, longitude=DEVICE_LON, accuracy=12.0,
        )
        self.user.refresh_from_db()
        self.assertAlmostEqual(float(self.user.latitude), self.IN_ORBITA[0])
        self.assertAlmostEqual(float(self.user.longitude), self.IN_ORBITA[1])

    def test_the_rule_still_holds_at_full_precision(self):
        """Loosening the type must not loosen "a place you are in"."""
        response = self.patch(
            zone_type='district', zone_region=self.yesil.pk,
            latitude=DEVICE_LAT, longitude=DEVICE_LON, accuracy=12.0,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get('code'), 'outside_region')

    def test_an_impossible_coordinate_is_refused_by_name(self):
        """`max_digits` used to bound the range as a side effect. It is gone, so
        the bound is explicit — a broken client gets told which field."""
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=900.0, longitude=DEVICE_LON,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('latitude', response.data)
