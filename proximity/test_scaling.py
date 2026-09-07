"""Visibility and unique-user limits must survive proximity query tuning."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from friends.models import BlockedUser
from interests.models import Interest
from proximity.views import _trigger_nearby_notifications
from users.models import User
from users.testing import share_location


class SharedInterestCandidateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.music = Interest.objects.create(name='Scaling music')
        cls.hiking = Interest.objects.create(name='Scaling hiking')
        cls.chess = Interest.objects.create(name='Scaling chess')
        cls.me = cls.make_user('scaling-me', cls.music, cls.hiking)
        cls.multiple = cls.make_user('multiple-match', cls.music, cls.hiking)
        cls.single = cls.make_user('single-match', cls.hiking)
        cls.unmatched = cls.make_user('unmatched', cls.chess)
        cls.opted_out = cls.make_user('opted-out', cls.music, cls.hiking)
        cls.opted_out.discoverable_by_interest = False
        cls.opted_out.save(update_fields=['discoverable_by_interest'])
        cls.blocked = cls.make_user('blocked', cls.music)
        BlockedUser.objects.create(blocker=cls.blocked, blocked=cls.me)

    @staticmethod
    def make_user(name, *interests):
        user = share_location(User.objects.create_user(
            username=name, email=f'{name}@example.com',
            latitude='10.0', longitude='10.0',
        ))
        user.interests.set(interests)
        return user

    @mock.patch('proximity.views.MAX_ZONE_CANDIDATES', 2)
    def test_multiple_shared_interests_do_not_consume_extra_candidate_slots(self):
        client = APIClient()
        client.force_authenticate(self.me)
        response = client.get('/api/proximity/nearby/')
        self.assertEqual(response.status_code, 200)
        ids = [entry['id'] for entry in response.json()]
        self.assertCountEqual(ids, [self.multiple.pk, self.single.pk])

    @mock.patch('proximity.views.record_encounters')
    @mock.patch('proximity.views.notify_nearby_matches')
    def test_write_side_interest_matching_keeps_unique_visible_candidates(
        self, notify, record,
    ):
        _trigger_nearby_notifications(self.me)
        for consumer in (notify, record):
            consumer.assert_called_once()
            reporter, matches = consumer.call_args.args
            self.assertEqual(reporter.pk, self.me.pk)
            self.assertCountEqual(
                [candidate.pk for candidate, _distance in matches],
                [self.multiple.pk, self.single.pk],
            )
