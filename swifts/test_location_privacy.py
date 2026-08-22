"""The location rule, asserted rather than trusted.

These are the tests that make the privacy claim in `swifts/serializers.py` and
`swifts/visibility.py` mean something. If a future change makes a coordinate
reachable from the feed, this module is what fails.

The central claim, in one sentence: **the only route in this project that can
answer with a Swift's latitude is `POST /api/swifts/<id>/reveal-location/`, and
it answers only for someone who has genuinely watched the Swift and whose
creator has said the location may be asked for.**
"""
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User

from .models import SwiftLocationReveal, SwiftVideo, SwiftView
from .testing import (
    SwiftTestCase,
    LAT, LON, befriend, block, fake_video, find_forbidden, make_swift, make_user,
)


class FeedNeverLeaksCoordinatesTests(SwiftTestCase):
    """Requirements 1, 2 and 3 — the feed serializer, with and without a
    location on the row."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def test_swift_with_no_location_reports_unavailable(self):
        """Requirement 1. A locationless Swift is a normal Swift."""
        make_swift(self.creator)
        body = self.client.get(reverse('swift-feed'), {'chrono': '1'}).json()

        self.assertEqual(body['count'], 1)
        row = body['results'][0]
        self.assertFalse(row['location_available'])
        self.assertEqual(find_forbidden(row), [])

    def test_swift_with_shared_location_reports_available_but_sends_nothing(self):
        """Requirement 2 and 3, together — the case that matters.

        The row advertises that a location *can* be asked for and carries not
        one field of it. This is the whole contract with the client: a boolean
        to draw a button from, and a separate request to press it.
        """
        make_swift(self.creator, located=True, shared=True)
        body = self.client.get(reverse('swift-feed'), {'chrono': '1'}).json()

        row = body['results'][0]
        self.assertTrue(row['location_available'])

        leaks = find_forbidden(row)
        self.assertEqual(leaks, [], f'feed leaked location fields: {leaks}')

    def test_raw_feed_body_contains_no_coordinate_substring(self):
        """A belt-and-braces pass over the serialized bytes.

        `find_forbidden` walks keys; this checks the *values*, so a coordinate
        smuggled out under an innocent name — `meta`, `debug`, a stringified
        blob — still fails. Keys can be renamed; the number cannot.
        """
        make_swift(self.creator, located=True, shared=True)
        raw = self.client.get(reverse('swift-feed'), {'chrono': '1'}).content.decode()

        self.assertNotIn(LAT, raw)
        self.assertNotIn(LON, raw)
        self.assertNotIn('Kok-Tobe', raw)

    def test_location_available_is_false_when_creator_disabled_sharing(self):
        """A stored coordinate with sharing off draws no button."""
        make_swift(self.creator, located=True, shared=False)
        body = self.client.get(reverse('swift-feed'), {'chrono': '1'}).json()

        self.assertFalse(body['results'][0]['location_available'])
        self.assertEqual(find_forbidden(body), [])


class DetailNeverLeaksBeforeRevealTests(SwiftTestCase):
    """Requirement 4 — the detail endpoint is not a way around the feed."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator, located=True, shared=True)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-detail', args=[self.swift.pk])

    def test_detail_has_no_coordinates_before_reveal(self):
        body = self.client.get(self.url).json()
        self.assertTrue(body['location_available'])
        self.assertEqual(find_forbidden(body), [])

    def test_detail_has_no_coordinates_after_reveal(self):
        """Revealing grants the *reveal endpoint*, not the detail one.

        Worth stating because the intuitive implementation of "unlock" is a
        flag the detail serializer then honours — which would mean the
        coordinate travels on every subsequent poll of an endpoint the client
        hits constantly, long after the screen that asked for it has gone.
        """
        SwiftView.objects.create(swift=self.swift, viewer=self.viewer)
        reveal = self.client.post(reverse('swift-reveal-location', args=[self.swift.pk]))
        self.assertEqual(reveal.status_code, 200)

        body = self.client.get(self.url).json()
        self.assertEqual(find_forbidden(body), [])

    def test_creator_own_detail_also_has_no_coordinates(self):
        """The creator is not an exception here.

        They can always *reveal* their own (see `RevealForCreatorTests`), but
        the detail payload stays uniform for everybody — one shape, one set of
        fields, no branch where a coordinate appears for some readers.
        """
        client = APIClient()
        client.force_authenticate(self.creator)
        body = client.get(self.url).json()

        self.assertTrue(body['location_available'])
        self.assertEqual(find_forbidden(body), [])


class RevealRequiresAGenuineViewTests(SwiftTestCase):
    """Requirements 5 and 6 — the gate that cannot be forged."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator, located=True, shared=True)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-reveal-location', args=[self.swift.pk])

    def test_cannot_reveal_without_having_viewed(self):
        """Requirement 5."""
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(find_forbidden(response.json()), [])
        self.assertFalse(SwiftLocationReveal.objects.exists())

    def test_client_cannot_claim_to_have_viewed(self):
        """The explicit instruction: do not trust a frontend boolean.

        Every shape of the lie in one test — a body flag, a camelCase one, and
        a query parameter. None of them writes a `SwiftView`, so none of them
        moves the gate.
        """
        for payload in ({'hasViewed': True}, {'has_viewed': True}, {'viewed': True}):
            with self.subTest(payload=payload):
                response = self.client.post(self.url, payload, format='json')
                self.assertEqual(response.status_code, 404)

        response = self.client.post(f'{self.url}?hasViewed=true')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(SwiftLocationReveal.objects.exists())

    def test_can_reveal_after_recording_a_view(self):
        """Requirement 6 — the happy path, end to end through the API.

        The view is recorded through the real endpoint rather than by creating
        the row directly, so this also proves the two halves are actually
        wired to each other.
        """
        recorded = self.client.post(reverse('swift-view', args=[self.swift.pk]))
        self.assertEqual(recorded.status_code, 201)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)

        body = response.json()
        self.assertAlmostEqual(body['latitude'], float(LAT), places=6)
        self.assertAlmostEqual(body['longitude'], float(LON), places=6)
        self.assertEqual(body['place_name'], 'Kok-Tobe Park')
        self.assertEqual(body['precision'], 'exact')
        self.assertTrue(
            SwiftLocationReveal.objects.filter(
                swift=self.swift, viewer=self.viewer).exists()
        )

    def test_view_of_one_swift_does_not_unlock_another(self):
        """The gate is per Swift, not per session.

        Otherwise watching a single Swift would unlock every location its
        creator ever attached, which is the aggregation risk the whole design
        is trying to avoid.
        """
        other = make_swift(self.creator, located=True, shared=True)
        self.client.post(reverse('swift-view', args=[other.pk]))

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)


class DisabledLocationCannotBeRevealedTests(SwiftTestCase):
    """Requirement 7."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def _viewed(self, swift):
        SwiftView.objects.create(swift=swift, viewer=self.viewer)
        return reverse('swift-reveal-location', args=[swift.pk])

    def test_stored_coordinates_with_sharing_off_are_not_revealed(self):
        """The row has a latitude. The answer is still 404.

        This is the case that separates "has a location" from "shares one",
        and it is why `location_shared` is a column rather than being inferred
        from the coordinates being non-null.
        """
        swift = make_swift(self.creator, located=True, shared=False)
        response = self.client.post(self._viewed(swift))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(find_forbidden(response.json()), [])

    def test_turning_sharing_off_afterwards_closes_the_door(self):
        """Reversibility, which is what makes the switch worth having.

        Somebody who revealed the location yesterday keeps what they learned —
        that cannot be taken back — but the endpoint stops answering, and no
        new person gets through.
        """
        swift = make_swift(self.creator, located=True, shared=True)
        url = self._viewed(swift)
        self.assertEqual(self.client.post(url).status_code, 200)

        swift.location_shared = False
        swift.save(update_fields=['location_shared'])

        self.assertEqual(self.client.post(url).status_code, 404)

    def test_no_coordinates_at_all_cannot_be_revealed(self):
        swift = make_swift(self.creator)
        self.assertEqual(self.client.post(self._viewed(swift)).status_code, 404)


class RevealIsIdempotentTests(SwiftTestCase):
    """Requirement 11."""

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator, located=True, shared=True)
        SwiftView.objects.create(swift=self.swift, viewer=self.viewer)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-reveal-location', args=[self.swift.pk])

    def test_repeated_reveals_return_identical_bodies(self):
        first = self.client.post(self.url)
        second = self.client.post(self.url)
        third = self.client.post(self.url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second.json(), third.json())

    def test_repeated_reveals_create_exactly_one_audit_row(self):
        for _ in range(5):
            self.client.post(self.url)

        rows = SwiftLocationReveal.objects.filter(swift=self.swift, viewer=self.viewer)
        self.assertEqual(rows.count(), 1)
        # The repeats are still counted — idempotent means the *outcome* does
        # not change, not that the server learns nothing.
        self.assertEqual(rows.first().reveal_count, 5)

    def test_revealed_at_records_the_first_reveal_and_never_moves(self):
        """The audit answers "since when does this person know"."""
        self.client.post(self.url)
        original = SwiftLocationReveal.objects.get(
            swift=self.swift, viewer=self.viewer).revealed_at

        self.client.post(self.url)
        after = SwiftLocationReveal.objects.get(
            swift=self.swift, viewer=self.viewer).revealed_at

        self.assertEqual(original, after)


class RevealForCreatorTests(SwiftTestCase):
    """The creator can always see the location on their own Swift."""

    def setUp(self):
        self.creator = make_user('creator')
        self.client = APIClient()
        self.client.force_authenticate(self.creator)

    def test_creator_reveals_own_without_having_viewed(self):
        swift = make_swift(self.creator, located=True, shared=True)
        self.assertFalse(SwiftView.objects.filter(swift=swift).exists())

        response = self.client.post(reverse('swift-reveal-location', args=[swift.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()['latitude'], float(LAT), places=6)

    def test_creator_reveals_own_even_with_sharing_off(self):
        """Turning sharing off is a statement about other people."""
        swift = make_swift(self.creator, located=True, shared=False)
        response = self.client.post(reverse('swift-reveal-location', args=[swift.pk]))
        self.assertEqual(response.status_code, 200)

    def test_creator_looking_at_their_own_leaves_no_audit_row(self):
        """The "who looked" list is for other people, not a self-log."""
        swift = make_swift(self.creator, located=True, shared=True)
        self.client.post(reverse('swift-reveal-location', args=[swift.pk]))
        self.assertFalse(SwiftLocationReveal.objects.exists())

    def test_audit_list_is_creator_only(self):
        swift = make_swift(self.creator, located=True, shared=True)
        stranger = make_user('stranger')
        SwiftView.objects.create(swift=swift, viewer=stranger)

        other = APIClient()
        other.force_authenticate(stranger)
        other.post(reverse('swift-reveal-location', args=[swift.pk]))

        self.assertEqual(
            other.get(reverse('swift-reveal-audit', args=[swift.pk])).status_code, 404,
        )
        mine = self.client.get(reverse('swift-reveal-audit', args=[swift.pk])).json()
        self.assertEqual(mine['count'], 1)
        self.assertEqual(mine['results'][0]['viewer']['username'], 'stranger')


class WriteTimeLocationPolicyTests(SwiftTestCase):
    """The creator's own location-sharing mode decides what is stored.

    Lifted from the story rules so the two products cannot answer "what does
    paused mean" differently. Not one of the twelve required tests, but the
    reveal endpoint is only as private as what reaches the columns.
    """

    def _post(self, user, **extra):
        client = APIClient()
        client.force_authenticate(user)
        payload = {
            'video': fake_video(),
            'duration_ms': 10_000,
            'latitude': LAT, 'longitude': LON,
            'place_name': 'Kok-Tobe Park',
            'location_shared': True,
        }
        payload.update(extra)
        # `None` means "the caller wants this not sent at all" — multipart,
        # unlike the JSON this suite used before requiring a file, has no way
        # to encode a null, so the key has to be dropped rather than passed.
        payload = {k: v for k, v in payload.items() if v is not None}
        return client.post(reverse('swift-create'), payload, format='multipart')

    def test_paused_creator_stores_no_location_at_all(self):
        """Not the coordinate, not the place name, not the flag."""
        paused = make_user('paused', sharing=False)
        response = self._post(paused)

        self.assertEqual(response.status_code, 201)
        swift = SwiftVideo.objects.get(pk=response.json()['id'])
        self.assertIsNone(swift.latitude)
        self.assertIsNone(swift.longitude)
        self.assertEqual(swift.place_name, '')
        self.assertFalse(swift.location_shared)
        self.assertFalse(response.json()['location_available'])

    def test_approximate_creator_has_the_point_snapped_before_storage(self):
        """Fuzzing happens on the way in, so no later reader can undo it."""
        user = make_user('approx')
        user.location_sharing = User.LocationSharing.APPROXIMATE
        user.save(update_fields=['location_sharing'])

        response = self._post(user)
        swift = SwiftVideo.objects.get(pk=response.json()['id'])

        self.assertIsNotNone(swift.latitude)
        self.assertNotEqual(str(swift.latitude), LAT)
        self.assertNotEqual(str(swift.longitude), LON)

    def test_approximate_reveal_says_so(self):
        """A fuzzed point returned as `exact` would be a lie the map draws."""
        user = make_user('approx2')
        user.location_sharing = User.LocationSharing.APPROXIMATE
        user.save(update_fields=['location_sharing'])
        swift_id = self._post(user).json()['id']

        client = APIClient()
        client.force_authenticate(user)
        body = client.post(reverse('swift-reveal-location', args=[swift_id])).json()
        self.assertEqual(body['precision'], 'approximate')

    def test_live_creator_stores_what_was_sent(self):
        response = self._post(make_user('live'))
        swift = SwiftVideo.objects.get(pk=response.json()['id'])

        self.assertEqual(str(swift.latitude), LAT)
        self.assertEqual(str(swift.longitude), LON)
        self.assertTrue(swift.location_shared)

    def test_sharing_a_location_that_was_not_sent_is_rejected(self):
        response = self._post(
            make_user('nocoords'), latitude=None, longitude=None,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('location_shared', response.json())


class RevealRespectsWatchabilityTests(SwiftTestCase):
    """A reveal can never outrun the audience gate.

    The interesting shape here is that a `SwiftView` row is durable while
    permission is not: somebody who watched a public Swift keeps their view row
    after the creator narrows it to friends, and the view row must not become a
    standing key.
    """

    def setUp(self):
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.swift = make_swift(self.creator, located=True, shared=True)
        SwiftView.objects.create(swift=self.swift, viewer=self.viewer)
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)
        self.url = reverse('swift-reveal-location', args=[self.swift.pk])

    def test_view_row_does_not_survive_the_audience_narrowing(self):
        self.assertEqual(self.client.post(self.url).status_code, 200)

        self.swift.visibility = SwiftVideo.Visibility.FRIENDS
        self.swift.save(update_fields=['visibility'])

        self.assertEqual(self.client.post(self.url).status_code, 404)

    def test_friends_only_swift_reveals_to_a_friend(self):
        self.swift.visibility = SwiftVideo.Visibility.FRIENDS
        self.swift.save(update_fields=['visibility'])
        befriend(self.creator, self.viewer)

        self.assertEqual(self.client.post(self.url).status_code, 200)

    def test_blocking_closes_an_already_granted_reveal(self):
        self.assertEqual(self.client.post(self.url).status_code, 200)
        block(self.creator, self.viewer)
        self.assertEqual(self.client.post(self.url).status_code, 404)

    def test_private_account_public_swift_does_not_reveal_to_a_stranger(self):
        """A private account does not publish by posting."""
        self.creator.is_private = True
        self.creator.save(update_fields=['is_private'])
        self.assertEqual(self.client.post(self.url).status_code, 404)

    def test_soft_deleted_swift_reveals_nothing(self):
        self.swift.deleted_at = self.swift.created_at
        self.swift.save(update_fields=['deleted_at'])
        self.assertEqual(self.client.post(self.url).status_code, 404)

    def test_removed_swift_reveals_nothing(self):
        self.swift.moderation = SwiftVideo.Moderation.REMOVED
        self.swift.save(update_fields=['moderation'])
        self.assertEqual(self.client.post(self.url).status_code, 404)


class RevealRefusalsAreIndistinguishableTests(SwiftTestCase):
    """Every refusal is the same 404 with the same body.

    The reason is stated in `swifts/visibility.py`: a response that told the
    caller *why* would let them tell "no such Swift" apart from "a Swift whose
    location you have not earned", and the second answer is itself the fact
    being protected.
    """

    def test_all_denial_paths_answer_identically(self):
        creator = make_user('creator')
        viewer = make_user('viewer')
        client = APIClient()
        client.force_authenticate(viewer)

        no_location = make_swift(creator, located=True, shared=False)
        not_viewed = make_swift(creator, located=True, shared=True)
        plain = make_swift(creator)
        private = make_swift(creator, located=True, shared=True,
                             visibility=SwiftVideo.Visibility.PRIVATE)

        responses = [
            client.post(reverse('swift-reveal-location', args=[s.pk]))
            for s in (no_location, not_viewed, plain, private)
        ]
        # A Swift id that does not exist at all — the baseline every refusal
        # above has to be indistinguishable from.
        responses.append(client.post(reverse('swift-reveal-location', args=[999_999])))

        for response in responses:
            self.assertEqual(response.status_code, 404)
        bodies = {r.content for r in responses}
        self.assertEqual(len(bodies), 1, f'refusals differ: {bodies}')


class EndToEndLocationJourneyTests(SwiftTestCase):
    """The whole creator → viewer path, as the client actually walks it.

    Each of the other classes pins one rule. This one walks the sequence the
    product describes — publish, watch, press, see — because the rules holding
    individually is not the same claim as the journey working, and a regression
    that breaks the *order* would pass everything above.
    """

    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.creator_client = APIClient()
        self.creator_client.force_authenticate(self.creator)
        self.viewer_client = APIClient()
        self.viewer_client.force_authenticate(self.viewer)

    def publish(self, **extra):
        payload = {
            'video': fake_video(),
            'duration_ms': 12_000,
        }
        payload.update(extra)
        # See the identical line in WriteTimeLocationPolicyTests._post: `None`
        # means "omit", and multipart cannot encode a null to say that itself.
        payload = {k: v for k, v in payload.items() if v is not None}
        response = self.creator_client.post(
            reverse('swift-create'), payload, format='multipart')
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def feed_row(self, swift_id):
        body = self.viewer_client.get(reverse('swift-feed'), {'chrono': '1'}).json()
        return next((r for r in body['results'] if r['id'] == swift_id), None)

    # ── The two publishing paths ─────────────────────────────────────────────
    def test_publishing_without_a_location_is_the_default(self):
        """No location fields sent at all — the ordinary case."""
        created = self.publish()
        row = self.feed_row(created['id'])

        self.assertFalse(row['location_available'])
        self.assertEqual(find_forbidden(row), [])

        swift = SwiftVideo.objects.get(pk=created['id'])
        self.assertIsNone(swift.latitude)
        self.assertFalse(swift.location_shared)

    def test_publishing_with_a_location_the_creator_chose(self):
        created = self.publish(
            latitude=LAT, longitude=LON,
            place_name='Kok-Tobe Park', location_shared=True,
        )
        row = self.feed_row(created['id'])

        self.assertTrue(row['location_available'])
        self.assertEqual(find_forbidden(row), [])

    # ── The viewer's sequence ────────────────────────────────────────────────
    def test_the_full_journey(self):
        created = self.publish(
            latitude=LAT, longitude=LON,
            place_name='Kok-Tobe Park', location_shared=True,
        )
        swift_id = created['id']
        reveal_url = reverse('swift-reveal-location', args=[swift_id])

        # 1. The viewer encounters it. No coordinate anywhere in the payload,
        #    only the flag that draws the button.
        row = self.feed_row(swift_id)
        self.assertTrue(row['location_available'])
        self.assertEqual(find_forbidden(row), [])

        # 2. Pressing before watching gets nothing. This is the step a client
        #    cannot skip by asserting it watched.
        self.assertEqual(self.viewer_client.post(reveal_url).status_code, 404)

        # 3. The client records the view.
        self.assertEqual(
            self.viewer_client.post(reverse('swift-view', args=[swift_id])).status_code,
            201,
        )

        # 4. Now the press works.
        revealed = self.viewer_client.post(reveal_url)
        self.assertEqual(revealed.status_code, 200)
        body = revealed.json()
        self.assertAlmostEqual(body['latitude'], float(LAT), places=6)
        self.assertEqual(body['place_name'], 'Kok-Tobe Park')

        # 5. And the feed still carries nothing, afterwards as before.
        self.assertEqual(find_forbidden(self.feed_row(swift_id)), [])

        # 6. The creator can see who looked.
        audit = self.creator_client.get(
            reverse('swift-reveal-audit', args=[swift_id])).json()
        self.assertEqual(audit['count'], 1)
        self.assertEqual(audit['results'][0]['viewer']['username'], 'viewer')

    def test_removing_the_location_afterwards_stops_later_reveals(self):
        """A creator changing their mind has to actually close the door.

        Somebody who already revealed keeps what they learned — that cannot be
        taken back — but nobody new gets through, and the button stops being
        drawn for viewers who had not pressed it.
        """
        created = self.publish(
            latitude=LAT, longitude=LON, location_shared=True)
        swift_id = created['id']
        reveal_url = reverse('swift-reveal-location', args=[swift_id])

        self.viewer_client.post(reverse('swift-view', args=[swift_id]))
        self.assertEqual(self.viewer_client.post(reveal_url).status_code, 200)

        # The creator takes it off, through the API the app uses.
        patched = self.creator_client.patch(
            reverse('swift-detail', args=[swift_id]),
            {'location_shared': False}, format='json',
        )
        self.assertEqual(patched.status_code, 200)

        self.assertEqual(self.viewer_client.post(reveal_url).status_code, 404)
        self.assertFalse(self.feed_row(swift_id)['location_available'])

        # A second viewer who watched but never revealed is locked out too.
        other = make_user('other')
        other_client = APIClient()
        other_client.force_authenticate(other)
        other_client.post(reverse('swift-view', args=[swift_id]))
        self.assertEqual(other_client.post(reveal_url).status_code, 404)

    def test_deleting_the_swift_stops_later_reveals(self):
        created = self.publish(
            latitude=LAT, longitude=LON, location_shared=True)
        swift_id = created['id']
        reveal_url = reverse('swift-reveal-location', args=[swift_id])

        self.viewer_client.post(reverse('swift-view', args=[swift_id]))
        self.assertEqual(self.viewer_client.post(reveal_url).status_code, 200)

        self.creator_client.delete(reverse('swift-detail', args=[swift_id]))

        self.assertEqual(self.viewer_client.post(reveal_url).status_code, 404)
        self.assertIsNone(self.feed_row(swift_id))

    def test_a_whole_feed_page_carries_no_coordinates(self):
        """Every row, located and not, mixed together at page size.

        The single-row assertions elsewhere would not catch a leak that only
        appears once a serializer is handed `many=True`.
        """
        for i in range(6):
            self.publish(
                latitude=LAT if i % 2 else None,
                longitude=LON if i % 2 else None,
                place_name='Kok-Tobe Park' if i % 2 else '',
                location_shared=bool(i % 2),
            )

        response = self.viewer_client.get(reverse('swift-feed'), {'chrono': '1'})
        body = response.json()

        self.assertEqual(body['count'], 6)
        self.assertEqual(find_forbidden(body), [])
        raw = response.content.decode()
        self.assertNotIn(LAT, raw)
        self.assertNotIn('Kok-Tobe', raw)
        # Exactly half advertise a revealable location, and none carry one.
        self.assertEqual(
            sum(1 for r in body['results'] if r['location_available']), 3)


class RankedFeedPrivacyTests(SwiftTestCase):
    """The same guarantees, on the personalised path.

    The suite above reads `?chrono=1` because it needs a stable oracle — the
    ranked feed personalises, never serves the same Swift twice, and leaves out
    the viewer's own content, so reading it repeatedly returns different rows.
    That makes it the wrong instrument for "what is in a row", and it would
    make it a *terrible* place to lose the location guarantee quietly.

    So the ranked path is asserted here, separately and directly. Both feeds go
    through one serializer and one `watchable_q`, which is exactly why this
    should never fail — and exactly why it is worth a test, because "should
    never" is what the guarantee rests on.
    """

    def setUp(self):
        super().setUp()
        self.creator = make_user('ranked_creator')
        self.viewer = make_user('ranked_viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    def _publish(self, *, located):
        return make_swift(
            self.creator,
            located=located,
            shared=located,
            caption='ranked feed row',
        )

    def test_the_ranked_feed_carries_no_coordinates(self):
        for i in range(6):
            self._publish(located=bool(i % 2))

        response = self.client.get(reverse('swift-feed'))
        body = response.json()

        self.assertTrue(body['results'], 'ranked feed returned nothing to check')
        self.assertEqual(find_forbidden(body), [])
        raw = response.content.decode()
        self.assertNotIn(LAT, raw)
        self.assertNotIn(LON, raw)

    def test_the_ranked_feed_still_advertises_availability(self):
        """`location_available` is a boolean about this viewer, and survives.

        If ranking had dropped it the reveal button would vanish; if ranking
        had derived it from column nullity instead of the gate, it would tell
        viewers which Swifts are hiding something from them specifically.
        """
        self._publish(located=True)
        self._publish(located=False)

        rows = self.client.get(reverse('swift-feed')).json()['results']

        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for r in rows if r['location_available']), 1)

    def test_the_sources_map_carries_ids_and_source_names_only(self):
        """The one field the ranked response adds that chrono does not."""
        self._publish(located=True)

        body = self.client.get(reverse('swift-feed')).json()

        self.assertEqual(find_forbidden(body.get('sources', {})), [])
        for key, value in body.get('sources', {}).items():
            self.assertRegex(str(key), r'^\d+$')
            self.assertRegex(value, r'^[a-z_]*$')

    def test_the_cursor_carries_no_content(self):
        """It names a cached session, not the rows in it."""
        for i in range(12):
            self._publish(located=bool(i % 2))

        cursor = self.client.get(reverse('swift-feed'), {'limit': 3}).json()['next_cursor']

        self.assertTrue(cursor)
        self.assertNotIn(LAT, cursor)
        self.assertNotIn('Kok-Tobe', cursor)
        for token in ('lat', 'lon', 'place'):
            self.assertNotIn(token, cursor.lower())

    def test_paging_through_the_whole_ranked_feed_leaks_nothing(self):
        """Every page, not just the first.

        A guarantee that holds on page one and not on page three is the kind
        that gets shipped, because page one is what anybody checks by hand.
        """
        for i in range(12):
            self._publish(located=bool(i % 2))

        cursor = None
        pages = 0
        while pages < 10:
            response = self.client.get(
                reverse('swift-feed'),
                {'limit': 3, **({'cursor': cursor} if cursor else {})},
            )
            body = response.json()
            self.assertEqual(find_forbidden(body), [], f'page {pages}')
            self.assertNotIn(LAT, response.content.decode(), f'page {pages}')
            cursor = body['next_cursor']
            pages += 1
            if not cursor:
                break
        self.assertGreater(pages, 1, 'expected more than one page to check')
