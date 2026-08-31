"""Privacy enforcement tests.

Every assertion here is written against the API response, not the serializer,
because the threat model is a modified client: the question is always "what
bytes did the server actually send", never "what would the app have rendered".
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from friends.models import BlockedUser, FriendRequest, HiRequest
from interests.models import Interest
from stories.models import Story
from users.models import User
from users.testing import share_location
from proximity.utils import haversine
from users.visibility import APPROX_GRID_M, snap_coordinates

#: Stands in for "this user has a zone track" in tests that only care about
#: visibility gating, not about what the track actually is.
TEST_TRACK_URL = 'https://example.com/test-track.mp3'


def make_user(name, **extra):
    # On the map by default. `location_sharing` defaults to Paused now (see
    # users.visibility), so a fixture for a test about anything else has to opt
    # in or it measures the default instead of its own subject.
    # proximity/test_map_visibility.py owns the assertions about the default.
    return share_location(User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    ))


def befriend(a, b):
    FriendRequest.objects.create(sender=a, receiver=b, status=FriendRequest.Status.ACCEPTED)


def connect(a, b):
    HiRequest.objects.create(sender=a, receiver=b, status=HiRequest.Status.ACCEPTED)


# ─── Private accounts ─────────────────────────────────────────────────────────
class PrivateAccountTests(TestCase):
    """A private account shows non-approved viewers a username and an avatar.
    Everything else must be absent from every endpoint that returns user data."""

    HIDDEN = ('bio', 'interests', 'stats', 'connections',
              'stories', 'zone_audio_url', 'date_joined')

    def setUp(self):
        self.client = APIClient()
        self.interest = Interest.objects.create(name='Test Climbing')
        self.viewer = make_user('viewer', latitude='10.0', longitude='10.0')
        self.viewer.interests.set([self.interest])

        self.target = make_user(
            'target',
            latitude='10.001', longitude='10.001',
            is_private=True,
            zone_apple_preview_url=TEST_TRACK_URL,
            zone_active=True,
        )
        self.target.interests.set([self.interest])
        Story.objects.create(
            user=self.target, content='hello', latitude='10.001', longitude='10.001',
            expires_at=timezone.now() + timezone.timedelta(hours=5),
        )
        self.client.force_authenticate(self.viewer)

    def test_public_profile_is_minimal_for_stranger(self):
        body = self.client.get(f'/api/users/profile/{self.target.pk}/').json()
        self.assertEqual(body['username'], 'target')
        self.assertTrue(body['is_private'])
        self.assertIn('profile_picture', body)
        for field in self.HIDDEN:
            self.assertNotIn(field, body, f'{field} leaked on a private profile')

    def test_nearby_is_minimal_for_stranger(self):
        entries = {e['id']: e for e in self.client.get('/api/proximity/nearby/').json()}
        entry = entries[self.target.pk]
        self.assertTrue(entry['is_private'])
        for field in ('bio', 'interests', 'shared_interests', 'zone_audio', 'zone_audio_url'):
            self.assertNotIn(field, entry, f'{field} leaked on the map')

    def test_search_is_minimal_for_stranger(self):
        results = self.client.get('/api/users/search/?q=target').json()
        self.assertEqual(len(results), 1)
        self.assertNotIn('interests', results[0])

    def test_stories_hidden_from_stranger(self):
        feed = self.client.get('/api/stories/nearby/').json()
        self.assertEqual([s for s in feed if s['author']['id'] == self.target.pk], [])

    def test_approved_connection_sees_full_profile(self):
        connect(self.viewer, self.target)
        body = self.client.get(f'/api/users/profile/{self.target.pk}/').json()
        self.assertIn('bio', body)
        self.assertIn('interests', body)

    def test_existing_connections_are_grandfathered_when_going_private(self):
        """Switching to private must not drop people who were already
        connected — they keep full access without re-approval."""
        target = make_user('grandfathered')
        connect(self.viewer, target)
        before = self.client.get(f'/api/users/profile/{target.pk}/').json()
        self.assertIn('bio', before)

        target.is_private = True
        target.save(update_fields=['is_private'])

        after = self.client.get(f'/api/users/profile/{target.pk}/').json()
        self.assertIn('bio', after)
        self.assertIn('interests', after)


# ─── Per-field visibility ─────────────────────────────────────────────────────
class FieldVisibilityTests(TestCase):
    """Each setting must actually remove its field from the API output — for
    the tier it excludes, and not for the tier it allows."""

    def setUp(self):
        self.client = APIClient()
        self.interest = Interest.objects.create(name='Test Pottery')
        self.viewer = make_user('viewer', latitude='10.0', longitude='10.0')
        self.viewer.interests.set([self.interest])
        self.target = make_user(
            'target', latitude='10.001', longitude='10.001',
            zone_apple_preview_url=TEST_TRACK_URL, zone_active=True,
        )
        self.target.interests.set([self.interest])
        self.client.force_authenticate(self.viewer)

    def profile(self):
        return self.client.get(f'/api/users/profile/{self.target.pk}/').json()

    def set_visibility(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self.target, key, value)
        self.target.save(update_fields=list(kwargs))

    def test_interests_nobody_removes_them_everywhere(self):
        self.set_visibility(visibility_interests=User.Audience.NOBODY)
        self.assertNotIn('interests', self.profile())

        entries = {e['id']: e for e in self.client.get('/api/proximity/nearby/').json()}
        # Still on the map (interests match server-side), just not disclosed.
        self.assertNotIn('interests', entries[self.target.pk])
        self.assertNotIn('shared_interests', entries[self.target.pk])

        results = self.client.get('/api/users/search/?q=target').json()
        self.assertNotIn('interests', results[0])

    def test_interests_friends_hides_from_connection_shows_to_friend(self):
        self.set_visibility(visibility_interests=User.Audience.FRIENDS)
        connect(self.viewer, self.target)
        self.assertNotIn('interests', self.profile())

        befriend(self.viewer, self.target)
        self.assertIn('interests', self.profile())

    def test_stats_hidden_by_default(self):
        # Default is Friends, so a stranger gets nothing.
        body = self.profile()
        self.assertNotIn('stats', body)

        befriend(self.viewer, self.target)
        body = self.profile()
        self.assertIn('stats', body)
        self.assertEqual(set(body['stats']), {'friends', 'connections'})

    def test_connections_list_gated(self):
        other = make_user('mutual')
        connect(self.target, other)
        self.assertNotIn('connections', self.profile())

        self.set_visibility(visibility_connections=User.Audience.EVERYONE)
        self.assertEqual(
            [c['id'] for c in self.profile()['connections']], [other.pk]
        )

    def test_connections_list_still_respects_viewer_blocks(self):
        """A block holds even when the pair appears on a third party's profile."""
        blocked = make_user('blocked')
        connect(self.target, blocked)
        BlockedUser.objects.create(blocker=self.viewer, blocked=blocked)
        self.set_visibility(visibility_connections=User.Audience.EVERYONE)
        self.assertEqual(self.profile()['connections'], [])

    def test_stories_gated(self):
        Story.objects.create(
            user=self.target, content='hi', latitude='10.001', longitude='10.001',
            expires_at=timezone.now() + timezone.timedelta(hours=5),
        )
        # Default Friends: absent from both the profile and the map feed.
        self.assertNotIn('stories', self.profile())
        feed = self.client.get('/api/stories/nearby/').json()
        self.assertEqual([s for s in feed if s['author']['id'] == self.target.pk], [])

        befriend(self.viewer, self.target)
        self.assertEqual(len(self.profile()['stories']), 1)
        feed = self.client.get('/api/stories/nearby/').json()
        self.assertEqual(len([s for s in feed if s['author']['id'] == self.target.pk]), 1)

    def test_stories_never_reach_a_stranger(self):
        """"Everyone" is not literal for stories, exactly as it is not for zone
        audio: a story is for the people the author chose, not for whoever
        happens to be standing nearby. Only "Nobody" differs from the default.
        """
        Story.objects.create(
            user=self.target, content='hi', latitude='10.001', longitude='10.001',
            expires_at=timezone.now() + timezone.timedelta(hours=5),
        )
        self.set_visibility(visibility_stories=User.Audience.EVERYONE)

        self.assertNotIn('stories', self.profile())
        feed = self.client.get('/api/stories/nearby/').json()
        self.assertEqual([s for s in feed if s['author']['id'] == self.target.pk], [])

        # And not to an accepted connection either — a Hi is a hello. This is
        # one tier tighter than zone audio, which does reach a connection.
        connect(self.viewer, self.target)
        self.assertNotIn('stories', self.profile())

    def test_stories_nobody_hides_them_from_friends_too(self):
        Story.objects.create(
            user=self.target, content='hi', latitude='10.001', longitude='10.001',
            expires_at=timezone.now() + timezone.timedelta(hours=5),
        )
        befriend(self.viewer, self.target)
        self.set_visibility(visibility_stories=User.Audience.NOBODY)
        self.assertNotIn('stories', self.profile())

    def test_a_friends_story_survives_them_leaving_the_map(self):
        """The two are separate products. Location sharing decides whether a
        story can be a *pin*; it has never been what decides whether a friend
        may read one — see stories/visibility.py."""
        story = Story.objects.create(
            user=self.target, content='hi', latitude='10.001', longitude='10.001',
            expires_at=timezone.now() + timezone.timedelta(hours=5),
        )
        befriend(self.viewer, self.target)
        self.target.ghost_mode = True
        self.target.save(update_fields=['ghost_mode'])

        feed = self.client.get('/api/stories/friends/').json()['results']
        self.assertIn(story.pk, {s['id'] for s in feed})
        # The pin, though, is gone with the dot.
        nearby = self.client.get('/api/stories/nearby/').json()
        self.assertEqual([s for s in nearby if s['id'] == story.pk], [])

    def test_hidden_story_cannot_be_reacted_to(self):
        """Hiding a story must remove the ability to act on it, not just the
        ability to see it — otherwise the id is still a working handle."""
        story = Story.objects.create(
            user=self.target, content='hi', latitude='10.0', longitude='10.0',
            expires_at=timezone.now() + timezone.timedelta(hours=5),
        )
        self.assertEqual(
            self.client.post(f'/api/stories/{story.pk}/react/', {'reaction': 'like'}).status_code,
            404,
        )

    def test_zone_audio_never_reaches_a_stranger(self):
        entries = {e['id']: e for e in self.client.get('/api/proximity/nearby/').json()}
        self.assertNotIn('zone_audio_url', entries[self.target.pk])

        # "Everyone" is not literal for zone audio — it still caps at friend
        # or connected, exactly like the default "Friends" setting. A
        # stranger gets nothing from either.
        self.set_visibility(visibility_zone_audio=User.Audience.EVERYONE)
        entries = {e['id']: e for e in self.client.get('/api/proximity/nearby/').json()}
        self.assertNotIn('zone_audio_url', entries[self.target.pk])

    def test_zone_audio_reaches_a_connection_by_default(self):
        # Default is Friends, but zone audio treats an accepted connection the
        # same as a friend — see users.visibility.can_view.
        connect(self.viewer, self.target)
        entries = {e['id']: e for e in self.client.get('/api/proximity/nearby/').json()}
        self.assertEqual(entries[self.target.pk]['zone_audio_url'], TEST_TRACK_URL)


# ─── Discoverability ──────────────────────────────────────────────────────────
class DiscoverabilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.interest = Interest.objects.create(name='Test Birding')
        self.viewer = make_user('viewer', latitude='10.0', longitude='10.0')
        self.viewer.interests.set([self.interest])
        self.target = make_user('findme', latitude='10.001', longitude='10.001')
        self.target.interests.set([self.interest])
        self.client.force_authenticate(self.viewer)

    def test_username_search_excludes_opted_out_user(self):
        self.assertEqual(len(self.client.get('/api/users/search/?q=findme').json()), 1)
        self.target.discoverable_by_username = False
        self.target.save(update_fields=['discoverable_by_username'])
        self.assertEqual(self.client.get('/api/users/search/?q=findme').json(), [])

    def test_interest_search_excludes_opted_out_user(self):
        url = f'/api/users/search/?interest={self.interest.pk}'
        self.assertEqual(len(self.client.get(url).json()), 1)
        self.target.discoverable_by_interest = False
        self.target.save(update_fields=['discoverable_by_interest'])
        self.assertEqual(self.client.get(url).json(), [])

    def test_the_two_surfaces_are_independent(self):
        """Opting out of username search must not remove you from interest
        search, and vice versa."""
        self.target.discoverable_by_username = False
        self.target.save(update_fields=['discoverable_by_username'])
        self.assertEqual(self.client.get('/api/users/search/?q=findme').json(), [])
        self.assertEqual(
            len(self.client.get(f'/api/users/search/?interest={self.interest.pk}').json()), 1
        )

    def test_interest_opt_out_removes_stranger_from_the_map(self):
        entries = {e['id'] for e in self.client.get('/api/proximity/nearby/').json()}
        self.assertIn(self.target.pk, entries)

        self.target.discoverable_by_interest = False
        self.target.save(update_fields=['discoverable_by_interest'])
        entries = {e['id'] for e in self.client.get('/api/proximity/nearby/').json()}
        self.assertNotIn(self.target.pk, entries)

    def test_empty_query_returns_nothing(self):
        """`username__icontains=''` used to match every row, handing any caller
        the whole user directory."""
        self.assertEqual(self.client.get('/api/users/search/').json(), [])
        self.assertEqual(self.client.get('/api/users/search/?q=').json(), [])
        self.assertEqual(self.client.get('/api/users/search/?q=f').json(), [])


# ─── Location ─────────────────────────────────────────────────────────────────
class ApproximateLocationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_user('walker')
        self.client.force_authenticate(self.user)

    def _report_position(self):
        """Put a precise position on the row through the real endpoint."""
        self.client.force_authenticate(self.user)
        self.client.patch(
            '/api/proximity/location/',
            {'latitude': '52.37021900', 'longitude': '4.89516700'},
            format='json',
        )

    def test_a_stranger_never_receives_the_precise_point(self):
        """The load-bearing one.

        The database holds the truth now, so this is the assertion standing
        between a stranger and it. Written against the API response rather than
        the serializer for the reason at the top of this file: the question is
        what bytes went out.
        """
        viewer = make_user('stranger', latitude='52.3702', longitude='4.8951')
        self.user.latitude, self.user.longitude = Decimal('52.37021900'), Decimal('4.89516700')
        self.user.save()
        self._report_position()

        self.client.force_authenticate(viewer)
        entry = {
            e['id']: e for e in self.client.get('/api/proximity/nearby/').json()
        }[self.user.pk]

        self.assertEqual(entry['location_precision'], 'approximate')
        self.assertNotEqual(Decimal(entry['latitude']), Decimal('52.37021900'))
        self.assertNotEqual(Decimal(entry['longitude']), Decimal('4.89516700'))

    def test_a_friend_receives_the_precise_point(self):
        """The other half. A rule that hid the position from everybody would
        pass the test above and still be wrong."""
        viewer = make_user('friend', latitude='52.3702', longitude='4.8951')
        befriend(viewer, self.user)
        self.user.latitude, self.user.longitude = Decimal('52.37021900'), Decimal('4.89516700')
        self.user.save()
        self._report_position()

        self.client.force_authenticate(viewer)
        entry = {
            e['id']: e for e in self.client.get('/api/proximity/nearby/').json()
        }[self.user.pk]

        self.assertEqual(entry['location_precision'], 'precise')
        # Floats over the wire, so compared as floats: the point is that this
        # is the reported position and not a cell centre.
        self.assertAlmostEqual(float(entry['latitude']), 52.370219, places=6)
        self.assertAlmostEqual(float(entry['longitude']), 4.895167, places=6)

    def test_the_distance_a_stranger_sees_matches_the_pin_they_see(self):
        """A coarse pin beside a precise distance is a precise pin.

        Two or three readings of an exact distance to a known viewer position
        trilaterate the point the pin was fuzzed to hide, so the number has to
        be derived from the same cell the pin is drawn at.
        """
        viewer = make_user('measuring-stranger', latitude='52.3702', longitude='4.8951')
        self.user.latitude, self.user.longitude = Decimal('52.37021900'), Decimal('4.89516700')
        self.user.save()
        self._report_position()

        self.client.force_authenticate(viewer)
        entry = {
            e['id']: e for e in self.client.get('/api/proximity/nearby/').json()
        }[self.user.pk]

        expected = haversine(
            52.3702, 4.8951,
            float(entry['latitude']), float(entry['longitude']),
        )
        self.assertAlmostEqual(entry['distance_km'], round(expected, 2), places=2)

    def test_snapping_is_stable_and_within_a_cell(self):
        """Deterministic, not jittered: repeated readings inside one cell must
        return the identical point, or an observer could average the noise away."""
        first = snap_coordinates(self.user, Decimal('52.37021900'), Decimal('4.89516700'))
        again = snap_coordinates(self.user, Decimal('52.37021900'), Decimal('4.89516700'))
        self.assertEqual(first, again)

        # A metre away still lands in the same cell.
        nudged = snap_coordinates(self.user, Decimal('52.37022800'), Decimal('4.89516700'))
        self.assertEqual(first, nudged)

        offset_m = abs(float(first[0]) - 52.370219) * 111_320
        self.assertLess(offset_m, APPROX_GRID_M)

    def test_the_stored_point_stays_precise(self):
        """The inversion, asserted at the row.

        Fuzzing used to happen before storage, so an Approximate sharer had no
        precise coordinate in the database at all. Friends are entitled to the
        real point now, so the row holds it and the fuzzing moved to the read —
        `test_a_stranger_never_receives_the_precise_point` is the other half of
        this, and the one that would catch a leak.
        """
        self.user.latitude, self.user.longitude = Decimal('52.37021900'), Decimal('4.89516700')
        self.user.save()
        self.client.patch('/api/users/privacy/', {'ghost_mode': False}, format='json')
        self.user.refresh_from_db()
        self.assertEqual(self.user.latitude, Decimal('52.37021900'))
        self.assertEqual(self.user.longitude, Decimal('4.89516700'))

    def test_ghost_removes_the_user_from_the_map_both_ways(self):
        viewer = make_user('viewer', latitude='10.0', longitude='10.0')
        befriend(viewer, self.user)
        self.user.latitude, self.user.longitude = Decimal('10.001'), Decimal('10.001')
        self.user.ghost_mode = True
        self.user.save()

        self.client.force_authenticate(viewer)
        entries = {e['id'] for e in self.client.get('/api/proximity/nearby/').json()}
        self.assertNotIn(self.user.pk, entries)

        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get('/api/proximity/nearby/').json(), [])

    def test_paused_does_not_hide_the_profile(self):
        """Location sharing and profile privacy are separate axes — pausing
        one must not silently apply the other."""
        viewer = make_user('viewer')
        self.user.ghost_mode = True
        self.user.save(update_fields=['ghost_mode'])
        self.client.force_authenticate(viewer)
        response = self.client.get(f'/api/users/profile/{self.user.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('bio', response.json())


# ─── Blocking, and indistinguishability ───────────────────────────────────────
class BlockedProfileTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.other = make_user('other')

    def test_blocked_user_cannot_load_the_blockers_profile(self):
        BlockedUser.objects.create(blocker=self.me, blocked=self.other)
        self.client.force_authenticate(self.other)
        self.assertEqual(
            self.client.get(f'/api/users/profile/{self.me.pk}/').status_code, 404
        )

    def test_block_is_symmetric_for_profiles(self):
        BlockedUser.objects.create(blocker=self.me, blocked=self.other)
        self.client.force_authenticate(self.me)
        self.assertEqual(
            self.client.get(f'/api/users/profile/{self.other.pk}/').status_code, 404
        )

    def test_invisible_and_nonexistent_are_indistinguishable(self):
        """Same status and same body, or the error itself confirms the account
        exists and has blocked you."""
        BlockedUser.objects.create(blocker=self.me, blocked=self.other)
        self.client.force_authenticate(self.other)
        blocked = self.client.get(f'/api/users/profile/{self.me.pk}/')
        missing = self.client.get('/api/users/profile/99999999/')
        self.assertEqual(blocked.status_code, missing.status_code)
        self.assertEqual(blocked.json(), missing.json())


# ─── Settings endpoint ────────────────────────────────────────────────────────
class PrivacySettingsEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = make_user('me')
        self.client.force_authenticate(self.user)

    def test_get_returns_every_control(self):
        body = self.client.get('/api/users/privacy/').json()
        for key in (
            'ghost_mode', 'is_private', 'visibility_interests',
            'visibility_stories', 'visibility_stats', 'visibility_zone_audio',
            'visibility_connections',
            'discoverable_by_username', 'discoverable_by_interest',
        ):
            self.assertIn(key, body)

    def test_patch_updates_settings(self):
        self.client.patch(
            '/api/users/privacy/',
            {'is_private': True, 'visibility_stats': 'nobody'},
            format='json',
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_private)
        self.assertEqual(self.user.visibility_stats, 'nobody')

    def test_legacy_privacy_mode_still_accepted(self):
        """App builds predating Ghost Mode send active/ghost/invisible.

        The collapse is lossy on purpose: old `ghost` meant "visible, but
        fuzzed", and fuzzing for strangers is now automatic rather than a mode.
        So it maps to visible, and the echo says `active` — the nearest true
        thing, rather than pretending a setting still exists.
        """
        response = self.client.patch(
            '/api/users/privacy/', {'privacy_mode': 'ghost'}, format='json'
        )
        self.user.refresh_from_db()
        self.assertFalse(self.user.ghost_mode)
        self.assertEqual(response.json()['privacy_mode'], 'active')

    def test_legacy_invisible_still_ghosts(self):
        response = self.client.patch(
            '/api/users/privacy/', {'privacy_mode': 'invisible'}, format='json'
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.ghost_mode)
        self.assertEqual(response.json()['privacy_mode'], 'invisible')

    def test_legacy_location_sharing_paused_still_ghosts(self):
        """The other old spelling, from builds after the first split."""
        self.client.patch(
            '/api/users/privacy/', {'location_sharing': 'paused'}, format='json'
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.ghost_mode)

    def test_self_profile_still_exposes_legacy_privacy_mode(self):
        """`privacy_mode` is a model property now, not a column — confirm it
        still reaches old clients through both self endpoints."""
        self.user.ghost_mode = True
        self.user.save(update_fields=['ghost_mode'])
        for url in ('/api/users/profile/', '/api/users/me/'):
            body = self.client.get(url).json()
            self.assertEqual(body['privacy_mode'], 'invisible', url)
            self.assertEqual(body['location_sharing'], 'paused', url)

    def test_invalid_value_rejected(self):
        response = self.client.patch(
            '/api/users/privacy/', {'location_sharing': 'sideways'}, format='json'
        )
        self.assertEqual(response.status_code, 400)


# ─── Defaults ─────────────────────────────────────────────────────────────────
class DefaultsTests(TestCase):
    def test_new_account_defaults(self):
        # `make_user` shares location for the rest of this module's benefit, so
        # the one assertion that is *about* the default builds its own user.
        # The default is *not* ghosted — sharing follows the OS permission the
        # client already holds, and proximity/test_map_visibility.py is where
        # that is pinned from every direction.
        user = User.objects.create_user(
            username='fresh-default', email='fresh-default@example.com',
            password='testpass-123',
        )
        self.assertFalse(user.is_private)
        self.assertFalse(user.ghost_mode)
        # Interests stay open: they are the matching primitive.
        self.assertEqual(user.visibility_interests, User.Audience.EVERYONE)
        for field in ('visibility_stories', 'visibility_stats',
                      'visibility_zone_audio', 'visibility_connections'):
            self.assertEqual(getattr(user, field), User.Audience.FRIENDS, field)
        self.assertTrue(user.discoverable_by_username)
        self.assertTrue(user.discoverable_by_interest)
