"""Nearby-match alerts, and that batching them changed nothing but the cost.

`notify_nearby_matches` replaced a per-pair function that ran about ten queries
each on the hottest write path in the app. These pin the rules it has to keep
while doing that: both directions, each gated on the *sender's* own settings,
one alert per pair per cooldown window.
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from friends.models import BlockedUser, FriendRequest
from notifications.models import Notification
from notifications.utils import (
    NEARBY_COOLDOWN_MINUTES, notify_nearby_match, notify_nearby_matches,
)
from users.models import User
from users.testing import share_location


def make_user(name, **kwargs):
    """On the map by default.

    A coordinate as well as a fresh stamp, because being on the map takes
    both — `has_fresh_position` requires a position to exist before asking how
    old it is. These fixtures used to get away without one: sharing defaulted
    to off so nothing asked, and the freshness fallback reads `updated_at`,
    which a brand-new row has. Sharing defaults to on now and the guard is
    explicit, so a user with no coordinate is correctly not on any map — and a
    nearby-alert test built on one would measure that instead of its subject.
    """
    kwargs.setdefault('latitude', '10.0')
    kwargs.setdefault('longitude', '10.0')
    return share_location(User.objects.create_user(
        username=name, email=f'{name}@example.com', password='pw',
        date_of_birth=date(1995, 1, 1), **kwargs
    ))


class NearbyMatchAlertTests(TestCase):
    def setUp(self):
        self.me = make_user('me')
        self.other = make_user('other')

    def alerts(self, recipient, sender):
        return Notification.objects.filter(
            recipient=recipient, sender=sender,
            notification_type=Notification.Type.NEARBY_USER,
        )

    def test_writes_both_directions(self):
        notify_nearby_matches(self.me, [(self.other, 0.2)])
        self.assertEqual(self.alerts(self.me, self.other).count(), 1)
        self.assertEqual(self.alerts(self.other, self.me).count(), 1)

    def test_message_names_the_sender_and_the_distance(self):
        notify_nearby_matches(self.me, [(self.other, 0.25)])
        alert = self.alerts(self.me, self.other).get()
        self.assertEqual(
            alert.message, 'other with shared interests is 0.25 km away.',
        )

    def test_cooldown_suppresses_a_second_alert(self):
        notify_nearby_matches(self.me, [(self.other, 0.2)])
        notify_nearby_matches(self.me, [(self.other, 0.3)])
        self.assertEqual(self.alerts(self.me, self.other).count(), 1)

    def test_alerts_again_once_the_cooldown_has_passed(self):
        notify_nearby_matches(self.me, [(self.other, 0.2)])
        Notification.objects.update(
            created_at=timezone.now() - timedelta(minutes=NEARBY_COOLDOWN_MINUTES + 1),
        )
        notify_nearby_matches(self.me, [(self.other, 0.3)])
        self.assertEqual(self.alerts(self.me, self.other).count(), 2)

    def test_a_block_stops_both_directions(self):
        BlockedUser.objects.create(blocker=self.other, blocked=self.me)
        notify_nearby_matches(self.me, [(self.other, 0.2)])
        self.assertEqual(Notification.objects.count(), 0)

    def test_ghost_mode_stops_nearby_alerts_both_ways(self):
        """A ghost cannot be announced or learn who is nearby through alerts."""
        self.other.ghost_mode = True
        self.other.save(update_fields=['ghost_mode'])

        notify_nearby_matches(self.me, [(self.other, 0.2)])
        # Nothing about the paused user reaches me...
        self.assertEqual(self.alerts(self.me, self.other).count(), 0)
        # Mutual invisibility also prevents them hearing about me.
        self.assertEqual(self.alerts(self.other, self.me).count(), 0)

    def test_interest_opt_out_stops_a_stranger_alert_only_one_way(self):
        self.other.discoverable_by_interest = False
        self.other.save(update_fields=['discoverable_by_interest'])

        notify_nearby_matches(self.me, [(self.other, 0.2)])
        self.assertEqual(self.alerts(self.me, self.other).count(), 0)
        self.assertEqual(self.alerts(self.other, self.me).count(), 1)

    def test_interest_opt_out_does_not_apply_between_friends(self):
        """Friendship outranks the discovery surface — you already found them."""
        self.other.discoverable_by_interest = False
        self.other.save(update_fields=['discoverable_by_interest'])
        FriendRequest.objects.create(
            sender=self.me, receiver=self.other,
            status=FriendRequest.Status.ACCEPTED,
        )
        notify_nearby_matches(self.me, [(self.other, 0.2)])
        self.assertEqual(self.alerts(self.me, self.other).count(), 1)

    def test_self_is_never_a_match(self):
        notify_nearby_matches(self.me, [(self.me, 0.0)])
        self.assertEqual(Notification.objects.count(), 0)

    def test_single_pair_wrapper_matches_the_batch(self):
        notify_nearby_match(self.me, self.other, 0.2)
        self.assertEqual(self.alerts(self.me, self.other).count(), 1)
        self.assertEqual(self.alerts(self.other, self.me).count(), 1)

    def test_query_count_does_not_grow_with_the_sweep(self):
        """The whole point: a bigger crowd must not cost more round trips."""
        crowd = [(make_user(f'p{i}'), 0.1) for i in range(12)]
        with self.assertNumQueries(5):
            notify_nearby_matches(self.me, crowd)
        self.assertEqual(Notification.objects.count(), 24)

    def test_empty_sweep_touches_the_database_not_at_all(self):
        with self.assertNumQueries(0):
            notify_nearby_matches(self.me, [])
