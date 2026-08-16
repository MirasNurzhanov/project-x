"""Story reactions, and the like counter underneath them.

The counter is a denormalised column rather than a `COUNT(*)`, so every path
that adds or removes a LIKE has to move it by exactly one — and has to do that
without losing a concurrent write. `_adjust_likes` is where both of those live.
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from stories.models import Story, StoryReaction
from stories.views import _adjust_likes
from users.models import User
from users.testing import share_location

#: The reaction endpoint requires the viewer to be within 500m of the story.
HERE = {'latitude': '10.000000', 'longitude': '10.000000'}
NEAR = {'latitude': '10.000500', 'longitude': '10.000500'}
FAR = {'latitude': '20.000000', 'longitude': '20.000000'}


def make_user(name, **extra):
    # On the map by default. `location_sharing` defaults to Paused now (see
    # users.visibility), and a paused viewer has no stored anchor — so a
    # fixture for a test about stories has to opt in or it measures the
    # default. proximity/test_map_visibility.py owns the assertions about that,
    # and proximity/test_private_query.py covers browsing while paused.
    return share_location(User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123',
        date_of_birth=date(1995, 1, 1), **extra
    ))


class AdjustLikesTests(TestCase):
    """The counter helper on its own — where the concurrency rule lives."""

    def setUp(self):
        self.author = make_user('author', **HERE)
        self.story = Story.objects.create(
            user=self.author, content='hello', **HERE,
            expires_at=timezone.now() + timedelta(hours=5),
        )

    def reread(self):
        return Story.objects.get(pk=self.story.pk).likes_count

    def test_increments(self):
        _adjust_likes(self.story, 1)
        self.assertEqual(self.reread(), 1)

    def test_decrements(self):
        Story.objects.filter(pk=self.story.pk).update(likes_count=3)
        _adjust_likes(self.story, -1)
        self.assertEqual(self.reread(), 2)

    def test_never_goes_negative(self):
        """The floor is applied in SQL, so it holds against a stale read too."""
        _adjust_likes(self.story, -1)
        self.assertEqual(self.reread(), 0)

    def test_does_not_lose_a_concurrent_like(self):
        """The regression this helper exists for.

        `self.story` is deliberately left holding the count it was created with
        while the database has moved on — which is exactly the state two
        overlapping requests are in. The old implementation computed
        `story.likes_count + 1` in Python from that stale value and wrote 1,
        silently discarding the four likes in between. Doing the arithmetic in
        the database cannot lose them.
        """
        Story.objects.filter(pk=self.story.pk).update(likes_count=4)
        self.assertEqual(self.story.likes_count, 0)  # stale, on purpose

        _adjust_likes(self.story, 1)

        self.assertEqual(self.reread(), 5)


class ReactToStoryTests(TestCase):
    """The endpoint, and that each route through it moves the counter once."""

    def setUp(self):
        self.client = APIClient()
        # Stories default to friends-only. These tests are about the counter,
        # not about who may see a story, so the author publishes to everyone —
        # otherwise every request below is a legitimate 404 and the counter is
        # never reached. Visibility itself is covered in users/tests.py.
        self.author = make_user(
            'author', visibility_stories=User.Audience.EVERYONE, **HERE,
        )
        self.viewer = make_user('viewer', **NEAR)
        self.story = Story.objects.create(
            user=self.author, content='hello', **HERE,
            expires_at=timezone.now() + timedelta(hours=5),
        )
        self.client.force_authenticate(self.viewer)

    def react(self, reaction):
        return self.client.post(
            f'/api/stories/{self.story.pk}/react/', {'reaction': reaction},
            format='json',
        )

    def likes(self):
        return Story.objects.get(pk=self.story.pk).likes_count

    def test_like_increments_the_counter(self):
        self.assertEqual(self.react('like').status_code, 201)
        self.assertEqual(self.likes(), 1)

    def test_liking_twice_toggles_off_and_decrements(self):
        self.react('like')
        response = self.react('like')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['user_reaction'])
        self.assertEqual(self.likes(), 0)
        self.assertFalse(StoryReaction.objects.filter(story=self.story).exists())

    def test_switching_away_from_like_decrements(self):
        self.react('like')
        self.assertEqual(self.likes(), 1)
        self.react('funny')
        self.assertEqual(self.likes(), 0)

    def test_switching_to_like_increments(self):
        self.react('funny')
        self.assertEqual(self.likes(), 0)
        self.react('like')
        self.assertEqual(self.likes(), 1)

    def test_a_non_like_reaction_leaves_the_counter_alone(self):
        self.react('funny')
        self.assertEqual(self.likes(), 0)
        self.assertEqual(
            StoryReaction.objects.get(story=self.story).reaction, 'funny',
        )

    def test_two_people_liking_counts_both(self):
        other = make_user('other', **NEAR)
        self.react('like')
        self.client.force_authenticate(other)
        self.react('like')
        self.assertEqual(self.likes(), 2)

    def test_one_reaction_per_person_per_story(self):
        self.react('like')
        self.react('funny')
        self.assertEqual(StoryReaction.objects.filter(story=self.story).count(), 1)

    def test_too_far_away_is_refused(self):
        distant = make_user('distant', **FAR)
        self.client.force_authenticate(distant)
        self.assertEqual(self.react('like').status_code, 403)
        self.assertEqual(self.likes(), 0)

    def test_expired_story_is_gone(self):
        Story.objects.filter(pk=self.story.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(self.react('like').status_code, 410)

    def test_unknown_reaction_is_rejected(self):
        self.assertEqual(self.react('shrug').status_code, 400)
        self.assertEqual(self.likes(), 0)


class StoryCoordinateTypeTests(TestCase):
    """latitude/longitude must serialize as JSON numbers, not strings.

    DRF's DecimalField stringifies by default (COERCE_DECIMAL_TO_STRING), and
    Fabric's typed view-manager props on Android refuse a string outright —
    AIRMapMarker throws rather than coercing the way the old bridge did. A
    regression here is a redbox on the map screen, not a rendering glitch.
    """

    def test_nearby_story_coordinates_are_numbers(self):
        author = make_user('author', **HERE, visibility_stories='everyone')
        viewer = make_user('viewer', **HERE)
        Story.objects.create(
            user=author, content='hi', latitude='10.000500', longitude='10.000500',
            expires_at=timezone.now() + timedelta(hours=5),
        )
        client = APIClient()
        client.force_authenticate(viewer)
        entry = client.get('/api/stories/nearby/').json()[0]
        self.assertIsInstance(entry['latitude'], float)
        self.assertIsInstance(entry['longitude'], float)
