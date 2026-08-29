"""The check that told people they were not standing where they stood.

"You can only cover a place you are in" ran against `user.latitude` — the
position the *server* had stored, written by a different endpoint on a
different schedule. The zone screen meanwhile had a live GPS fix on screen and
was drawing its preview with it. Open the app straight into Settings → Zone
Size, or walk a few streets first, and the two disagreed. The disagreement
surfaced as a refusal naming a district the user was standing in, with advice —
"open the map for a moment so it can catch up" — that is not a thing anybody
can reliably act on.

The client had already tried to close this by firing a location report before
the save. That is a race, and the zone PATCH regularly won it.

So the request now *carries* the position it wants checked, and there is
nothing left to race. The stored copy stays as a fallback for clients too old
to send one. Nothing about the rule itself changes: these tests keep as much
attention on what is still refused as on what now works.
"""
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User
from users.test_zone_follow import FollowFixture


class ContainmentAnchorTests(FollowFixture):
    def setUp(self):
        super().setUp()
        self.user = self.make_user(at=self.IN_ORBITA)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def patch(self, **body):
        return self.client.patch(reverse('zone'), body, format='json')

    def stale_position(self, at):
        """Leave the server holding a position from somewhere the user is not.

        Written straight to the columns, which is exactly the state the bug
        produced: the map last ticked in another district.
        """
        self.user.latitude, self.user.longitude = at
        self.user.save(update_fields=['latitude', 'longitude'])

    # ── The bug ───────────────────────────────────────────────────────────────
    def test_a_fresh_fix_beats_a_stale_stored_position(self):
        """The report, in one test: standing in Kazakhfilm, server still holding
        Orbita, choosing Kazakhfilm."""
        self.stale_position(self.IN_ORBITA)
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='district', zone_region=self.kazakhfilm.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_region_id, self.kazakhfilm.pk)

    def test_the_supplied_position_is_never_stored(self):
        """Configuring a zone must not write a location, or this endpoint
        becomes a second and unaudited way onto the map — one that works while
        sharing is paused."""
        self.stale_position(self.IN_ORBITA)
        lat, lon = self.IN_KAZAKHFILM
        self.patch(
            zone_type='district', zone_region=self.kazakhfilm.pk,
            latitude=lat, longitude=lon, accuracy=12.0,
        )
        self.user.refresh_from_db()
        self.assertAlmostEqual(float(self.user.latitude), self.IN_ORBITA[0])
        self.assertAlmostEqual(float(self.user.longitude), self.IN_ORBITA[1])

    def test_no_position_is_echoed_back(self):
        lat, lon = self.IN_KAZAKHFILM
        body = self.patch(
            zone_type='district', zone_region=self.kazakhfilm.pk,
            latitude=lat, longitude=lon, accuracy=12.0,
        ).json()
        for field in ('latitude', 'longitude', 'accuracy'):
            self.assertNotIn(field, body)

    def test_a_paused_user_can_still_configure_a_zone(self):
        """Setting up a zone should not require broadcasting where you are.
        Same arrangement as private browsing in `NearbyQueryView`: a coordinate
        used for one request and never stored."""
        self.user.location_sharing = User.LocationSharing.PAUSED
        self.user.save(update_fields=['location_sharing'])
        lat, lon = self.IN_ORBITA
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 200, response.data)

    # ── The rule still holds ──────────────────────────────────────────────────
    def test_a_place_you_are_genuinely_not_in_is_still_refused(self):
        """The anchor changes *which* position is trusted, not whether the rule
        applies. Somebody in Almaty still cannot blanket a district of Astana."""
        lat, lon = self.IN_ORBITA
        response = self.patch(
            zone_type='district', zone_region=self.yesil.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 400)

    def test_no_position_anywhere_is_still_refused(self):
        user = self.make_user('positionless')
        client = APIClient()
        client.force_authenticate(user)
        response = client.patch(reverse('zone'), {
            'zone_type': 'district', 'zone_region': self.orbita.pk,
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_an_old_client_sending_no_position_still_works(self):
        """The stored fallback. An installed build that has never heard of the
        anchor has to keep behaving exactly as it did."""
        response = self.patch(zone_type='district', zone_region=self.orbita.pk)
        self.assertEqual(response.status_code, 200, response.data)

    def test_an_old_client_with_a_stale_stored_position_is_still_refused(self):
        """Which is the old bug, and it is *kept* for old clients rather than
        loosened for everyone — the fix is that the current app no longer takes
        this path, not that the check got weaker."""
        self.stale_position(self.IN_ORBITA)
        response = self.patch(zone_type='district', zone_region=self.kazakhfilm.pk)
        self.assertEqual(response.status_code, 400)

    # ── Accuracy ──────────────────────────────────────────────────────────────
    def test_a_poor_fix_just_outside_a_border_is_allowed_in(self):
        """A fix is wrong by a knowable amount, and refusing somebody at a
        border because their phone is 200m out is the same bug in a new coat."""
        # ~140m north of Orbita's northern edge (lat 0.2), inside the reported
        # error of the fix.
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=0.20125, longitude=0.1, accuracy=400.0,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_accuracy_slack_is_capped(self):
        """Otherwise a client claims a kilometre of error and covers the next
        district over."""
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=lat, longitude=lon, accuracy=999999.0,
        )
        self.assertEqual(response.status_code, 400)

    def test_a_negative_accuracy_is_rejected_rather_than_used(self):
        lat, lon = self.IN_ORBITA
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=lat, longitude=lon, accuracy=-5.0,
        )
        self.assertEqual(response.status_code, 400)

    def test_a_missing_accuracy_is_treated_as_a_perfect_fix(self):
        lat, lon = self.IN_ORBITA
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_half_a_position_is_ignored_rather_than_half_trusted(self):
        """A latitude with no longitude is not a position. It falls back to the
        stored one instead of being combined with it, which would test a point
        that exists nowhere."""
        self.stale_position(self.IN_ORBITA)
        response = self.patch(
            zone_type='district', zone_region=self.kazakhfilm.pk,
            latitude=self.IN_KAZAKHFILM[0],
        )
        self.assertEqual(response.status_code, 400)

    # ── The refusal is actionable ─────────────────────────────────────────────
    def test_a_refusal_names_the_place_you_are_actually_in(self):
        """A rejection on its own is a dead end for anybody who does not know
        their district's official name."""
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'outside_region')
        self.assertEqual(response.data['you_are_in']['id'], self.kazakhfilm.pk)
        self.assertEqual(response.data['you_are_in']['name'], 'Kazakhfilm')

    def test_the_suggestion_is_the_same_size_as_the_one_refused(self):
        """Offering a city to somebody who asked for a district would be
        offering a zone orders of magnitude larger than the one they chose."""
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='city', zone_region=self.astana.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['you_are_in']['kind'], 'city')
        self.assertEqual(response.data['you_are_in']['id'], self.almaty.pk)

    def test_a_refusal_with_nothing_cached_nearby_still_refuses_cleanly(self):
        """The suggestion is a convenience, not a requirement: somewhere with no
        cached boundaries has to produce a plain 400, not a 500."""
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=40.0, longitude=40.0,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(response.data['you_are_in'])

    # ── It composes with following ────────────────────────────────────────────
    def test_setting_a_following_zone_from_a_fresh_fix_works(self):
        self.stale_position(self.IN_ORBITA)
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='district', zone_region=self.kazakhfilm.pk,
            zone_follows_location=True, latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.zone_follows_location)
        self.assertEqual(self.user.zone_region_id, self.kazakhfilm.pk)


class LeavingAZoneYouAlreadyHaveTests(FollowFixture):
    """Moving out of the place you cover must not lock you out of the screen.

    The failure this covers is the one that actually stopped people: it does not
    look like a rule being enforced, it looks like the app being broken.

      1. You set your zone to your district. Fine.
      2. You move to another district.
      3. You open Zone Size and tap **Place**, to pick your new one.
      4. The tab re-submits the region you already have, the server checks
         whether you are still inside it, refuses — and the tab snaps back to
         Radius.

    So the picker that would have let you choose the new district is
    unreachable, and every attempt ends in an alert naming the *old* district,
    which reads as "Approx will not let me leave this place".

    The check now runs only when the region is actually changing, and the app
    no longer re-sends an unchanged one. Either fix alone would close this;
    both are here because either one alone leaves the other free to bring it
    back.
    """

    def setUp(self):
        super().setUp()
        self.user = self.make_user(at=self.IN_ORBITA)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        # Zone set while standing in Orbita, the way it really happens.
        response = self.client.patch(reverse('zone'), {
            'zone_type': 'district', 'zone_region': self.orbita.pk,
            'latitude': self.IN_ORBITA[0], 'longitude': self.IN_ORBITA[1],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        # ...and then they walk to Kazakhfilm.
        self.user.refresh_from_db()

    def patch(self, **body):
        return self.client.patch(reverse('zone'), body, format='json')

    def test_re_sending_the_region_you_already_have_is_accepted(self):
        """Step 4. Refusing this does not un-set the zone — it was already
        stored — it only blocks the edit the user came to make."""
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='district', zone_region=self.orbita.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_switching_back_to_the_place_tab_after_moving_works(self):
        """What the app actually sends now: the type, and no region at all."""
        response = self.patch(zone_type='district')
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_region_id, self.orbita.pk)

    def test_and_then_the_new_district_can_be_chosen(self):
        """The whole point of getting onto the tab. This is the assertion that
        says the reported symptom — "it cannot change to another district" — is
        gone."""
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='district', zone_region=self.kazakhfilm.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.zone_region_id, self.kazakhfilm.pk)

    def test_an_unrelated_edit_is_not_refused_because_you_moved(self):
        """A radius change, or a music setting, with a polygon zone stored and
        its owner now elsewhere. None of that is a claim about where they are."""
        response = self.patch(zone_radius=1200)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_different_place_you_are_not_in_is_still_refused(self):
        """The rule itself is untouched: this is what stops somebody in Almaty
        blanketing a district of Astana."""
        lat, lon = self.IN_KAZAKHFILM
        response = self.patch(
            zone_type='district', zone_region=self.yesil.pk,
            latitude=lat, longitude=lon,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'outside_region')

    def test_a_first_time_pick_is_still_checked(self):
        """"Unchanged" has to mean unchanged from what is *stored*, not merely
        from what was sent. Somebody with no zone at all gets the full check."""
        fresh = self.make_user('freshly-arrived', at=self.IN_ORBITA)
        client = APIClient()
        client.force_authenticate(fresh)
        response = client.patch(reverse('zone'), {
            'zone_type': 'district', 'zone_region': self.yesil.pk,
            'latitude': self.IN_ORBITA[0], 'longitude': self.IN_ORBITA[1],
        }, format='json')
        self.assertEqual(response.status_code, 400)
