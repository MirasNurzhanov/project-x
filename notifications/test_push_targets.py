"""Who gets notified about a message, a meetup, or an invitation — and who does not.

These pin the decisions, not the transport. `core.test_settings` sets
`PUSH_DISABLED`, so `push_to_users` builds its message list, reports how many
it built, and stops before the thread that would talk to Expo. That count is
what "was pushed" means below.

The rules worth keeping are the ones that were wrong or absent before:

  - a message sent over the *socket* notifies, which is the path the app uses
    and the one that used to notify nobody,
  - a chatty conversation or club is one buzz, not one per line,
  - every switch on the Notifications screen actually silences something,
  - a block is honoured in both directions everywhere, including club chat.
"""
from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from clubs.models import Club, ClubEvent, ClubInvitation, ClubMember
from friends.models import BlockedUser
from notifications.models import Notification
from notifications import push
from notifications.push import allows, channel_for, push_to_users
from notifications.utils import (
    notify_club_event_created, notify_club_event_decided, notify_club_invite,
    notify_club_message, notify_direct_message,
)
from users.models import Device, User
from users.testing import share_location


def make_user(name, *, token='tok', **kwargs):
    # On the map by default. `location_sharing` defaults to Paused now (see
    # users.visibility), so a fixture for a test about anything else has to opt
    # in or it measures the default instead of its own subject.
    # proximity/test_map_visibility.py owns the assertions about the default.
    return share_location(User.objects.create_user(
        username=name, email=f'{name}@example.com', password='pw',
        date_of_birth=date(1995, 1, 1), push_token=token, **kwargs
    ))


class PushTargetTestCase(TestCase):
    """Cooldowns live in the cache, which is process-wide and outlives a test."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


# ─── Transport ────────────────────────────────────────────────────────────────
class PushTransportTests(PushTargetTestCase):
    def test_counts_one_message_per_recipient(self):
        people = [make_user(f'p{i}', token=f'tok{i}') for i in range(3)]
        self.assertEqual(push_to_users(people, 'T', 'B'), 3)

    def test_skips_users_without_a_token(self):
        self.assertEqual(push_to_users([make_user('a', token='')], 'T', 'B'), 0)
        self.assertEqual(push_to_users([make_user('b', token=None)], 'T', 'B'), 0)

    def test_one_device_signed_into_two_accounts_is_pushed_once(self):
        """Two accounts can share a phone, and a phone is what buzzes."""
        shared = [make_user('a', token='same'), make_user('b', token='same')]
        self.assertEqual(push_to_users(shared, 'T', 'B'), 1)

    def test_category_preference_drops_the_recipient(self):
        muted = make_user('muted', notify_club_messages=False)
        self.assertEqual(push_to_users([muted], 'T', 'B', category='club_message'), 0)
        # ...and only that category.
        self.assertEqual(push_to_users([muted], 'T', 'B', category='message'), 1)

    def test_unknown_category_still_sends(self):
        """A caller that forgets to register itself must be loud, not dead."""
        self.assertEqual(push_to_users([make_user('a')], 'T', 'B', category='novel'), 1)
        self.assertTrue(allows(make_user('b'), 'novel'))

    def test_every_category_names_an_android_channel(self):
        for category in ('message', 'club_message', 'club_event', 'social', 'story'):
            self.assertTrue(channel_for(category))

    def test_large_audience_is_chunked_into_expo_sized_requests(self):
        crowd = [make_user(f'p{i}', token=f'tok{i}') for i in range(5)]
        submit = push._executor.submit
        with override_settings(PUSH_DISABLED=False), \
                patch('notifications.push.requests.post') as post, \
                patch('notifications.push._executor.submit', side_effect=lambda *args: submit(*args).result(timeout=5)), \
                patch('notifications.push.MAX_PER_REQUEST', 2):
            push_to_users(crowd, 'T', 'B')
            # Wait on this task's Future, not unrelated daemon threads.
        self.assertEqual(post.call_count, 3)  # 2 + 2 + 1

    def test_payload_carries_the_deep_link(self):
        user = make_user('a')
        submit = push._executor.submit
        with override_settings(PUSH_DISABLED=False), \
                patch('notifications.push._executor.submit', side_effect=lambda *args: submit(*args).result(timeout=5)), \
                patch('notifications.push.requests.post') as post:
            push_to_users([user], 'T', 'B', data={'type': 'message', 'user_id': 7})
        sent = post.call_args.kwargs['json'][0]
        self.assertEqual(sent['data'], {'type': 'message', 'user_id': 7})
        self.assertEqual(sent['to'], 'tok')


# ─── Direct messages ──────────────────────────────────────────────────────────
class DirectMessageAlertTests(PushTargetTestCase):
    def setUp(self):
        super().setUp()
        self.sender = make_user('sender')
        self.recipient = make_user('recipient')

    def test_notifies_the_recipient(self):
        self.assertTrue(notify_direct_message(self.sender, self.recipient, 'hey'))

    def test_a_burst_of_messages_is_one_notification(self):
        self.assertTrue(notify_direct_message(self.sender, self.recipient, 'hey'))
        self.assertFalse(notify_direct_message(self.sender, self.recipient, 'you there'))
        self.assertFalse(notify_direct_message(self.sender, self.recipient, 'hello?'))

    def test_the_cooldown_is_per_pair_not_global(self):
        other = make_user('other')
        self.assertTrue(notify_direct_message(self.sender, self.recipient, 'hey'))
        self.assertTrue(notify_direct_message(other, self.recipient, 'hey'))

    def test_blocked_sender_reaches_nobody(self):
        BlockedUser.objects.create(blocker=self.recipient, blocked=self.sender)
        self.assertFalse(notify_direct_message(self.sender, self.recipient, 'hey'))

    def test_a_note_to_self_is_not_news(self):
        self.assertFalse(notify_direct_message(self.sender, self.sender, 'hey'))

    def test_writes_no_alert_row(self):
        """The inbox is already the list of messages; a row would duplicate it."""
        notify_direct_message(self.sender, self.recipient, 'hey')
        self.assertEqual(Notification.objects.count(), 0)

    def test_muted_recipient_is_not_pushed(self):
        self.recipient.notify_messages = False
        self.recipient.save(update_fields=['notify_messages'])
        # The cooldown is still claimed, but nothing is sent — assert on the
        # push itself rather than the return value, which only reports the gate.
        with patch('notifications.utils.push_to_user') as push:
            notify_direct_message(self.sender, self.recipient, 'hey')
        self.assertEqual(push.call_count, 1)
        self.assertEqual(push.call_args.kwargs['category'], 'message')
        self.assertFalse(allows(self.recipient, 'message'))

    def test_photo_with_no_caption_says_so(self):
        with patch('notifications.utils.push_to_user') as push:
            notify_direct_message(self.sender, self.recipient, '', image='x.jpg')
        self.assertIn('Photo', push.call_args.args[2])

    def test_long_message_is_truncated(self):
        with patch('notifications.utils.push_to_user') as push:
            notify_direct_message(self.sender, self.recipient, 'x' * 500)
        self.assertLessEqual(len(push.call_args.args[2]), 121)


# ─── Club chat ────────────────────────────────────────────────────────────────
class ClubMessageAlertTests(PushTargetTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name='Runners')
        self.sender = make_user('sender')
        self.members = [make_user(f'm{i}', token=f'tok{i}') for i in range(3)]
        for user in [self.sender, *self.members]:
            ClubMember.objects.create(user=user, club=self.club)

    def recipients_of(self, content, by=None):
        """The usernames a club message actually reaches.

        Counts alone made these tests ambiguous — "two people" does not say
        whether the right two — and the interesting cases here are all about
        *which* member a cooldown or a switch left out.
        """
        with patch('notifications.utils.push_to_users') as push:
            notify_club_message(self.club, by or self.sender, content)
        if not push.call_args:
            return set()
        return {user.username for user in push.call_args.args[0]}

    def test_notifies_every_member_but_the_sender(self):
        self.assertEqual(notify_club_message(self.club, self.sender, 'hi all'), 3)

    def test_a_busy_club_buzzes_each_member_once(self):
        """A reply reaches whoever has not just been buzzed — and only them.

        The first message claims a cooldown for all three members. The reply
        that follows finds them all inside it, so the only person it reaches
        is the one who spoke first and therefore never held one.
        """
        self.assertEqual(notify_club_message(self.club, self.sender, 'one'), 3)
        self.assertEqual(self.recipients_of('two', by=self.members[0]), {'sender'})

    def test_a_member_registered_only_as_a_device_is_reached(self):
        # How every current build registers: a Device row, and the old
        # `User.push_token` column left empty. Club chat used to read the
        # column alone and so skipped these members entirely.
        phone = make_user('phone', token='')
        Device.objects.create(user=phone, install_id='install-phone', push_token='ExponentPushToken[phone]')
        ClubMember.objects.create(user=phone, club=self.club)
        self.assertIn('phone', self.recipients_of('hi'))

    def test_non_members_hear_nothing(self):
        make_user('outsider', token='outsider-tok')
        self.assertEqual(notify_club_message(self.club, self.sender, 'hi'), 3)

    def test_a_muted_member_is_skipped_and_keeps_no_cooldown(self):
        self.members[0].notify_club_messages = False
        self.members[0].save(update_fields=['notify_club_messages'])
        self.assertEqual(self.recipients_of('hi'), {'m1', 'm2'})

        # Turning the switch back on works immediately, rather than after a
        # cooldown a skipped member should never have claimed. m2 is still
        # inside theirs; 'sender' spoke last time and so never held one.
        self.members[0].notify_club_messages = True
        self.members[0].save(update_fields=['notify_club_messages'])
        self.assertEqual(self.recipients_of('hi', by=self.members[1]), {'m0', 'sender'})

    def test_a_block_hides_club_chatter_in_both_directions(self):
        BlockedUser.objects.create(blocker=self.members[0], blocked=self.sender)
        BlockedUser.objects.create(blocker=self.sender, blocked=self.members[1])
        self.assertEqual(notify_club_message(self.club, self.sender, 'hi'), 1)

    def test_writes_no_alert_rows(self):
        notify_club_message(self.club, self.sender, 'hi')
        self.assertEqual(Notification.objects.count(), 0)

    def test_body_names_the_speaker_and_the_club(self):
        with patch('notifications.utils.push_to_users') as push:
            notify_club_message(self.club, self.sender, 'hi all')
        self.assertIn('Runners', push.call_args.args[1])
        self.assertIn('sender', push.call_args.args[2])


# ─── Meetups ──────────────────────────────────────────────────────────────────
class ClubEventAlertTests(PushTargetTestCase):
    def setUp(self):
        super().setUp()
        self.club = Club.objects.create(name='Runners')
        self.owner = make_user('owner')
        self.proposer = make_user('proposer')
        self.member = make_user('member')
        ClubMember.objects.create(user=self.owner, club=self.club, role=ClubMember.Role.OWNER)
        ClubMember.objects.create(user=self.proposer, club=self.club)
        ClubMember.objects.create(user=self.member, club=self.club)
        self.event = ClubEvent.objects.create(
            club=self.club, created_by=self.proposer, title='Park run',
            event_datetime=timezone.now() + timedelta(days=1),
        )

    def alerts(self, kind):
        return Notification.objects.filter(notification_type=kind)

    def test_a_proposal_reaches_every_other_member(self):
        notify_club_event_created(self.event)
        recipients = set(self.alerts(Notification.Type.CLUB_EVENT)
                         .values_list('recipient_id', flat=True))
        self.assertEqual(recipients, {self.owner.pk, self.member.pk})

    def test_a_proposal_alert_can_be_opened(self):
        notify_club_event_created(self.event)
        alert = self.alerts(Notification.Type.CLUB_EVENT).first()
        self.assertEqual(alert.club_id, self.club.pk)
        self.assertEqual(alert.event_id, self.event.pk)
        self.assertIn('Park run', alert.message)

    def test_approval_tells_the_whole_club(self):
        notify_club_event_decided(self.event, self.owner, approved=True)
        recipients = set(self.alerts(Notification.Type.CLUB_EVENT_STATUS)
                         .values_list('recipient_id', flat=True))
        self.assertEqual(recipients, {self.proposer.pk, self.member.pk})

    def test_rejection_tells_only_the_proposer(self):
        notify_club_event_decided(self.event, self.owner, approved=False)
        recipients = list(self.alerts(Notification.Type.CLUB_EVENT_STATUS)
                          .values_list('recipient_id', flat=True))
        self.assertEqual(recipients, [self.proposer.pk])

    def test_rejecting_your_own_proposal_notifies_nobody(self):
        event = ClubEvent.objects.create(
            club=self.club, created_by=self.owner, title='Own idea',
            event_datetime=timezone.now() + timedelta(days=1),
        )
        notify_club_event_decided(event, self.owner, approved=False)
        self.assertEqual(self.alerts(Notification.Type.CLUB_EVENT_STATUS).count(), 0)

    def test_deleting_the_club_takes_its_alerts_with_it(self):
        notify_club_event_created(self.event)
        self.club.delete()
        self.assertEqual(Notification.objects.count(), 0)

    def test_an_invitation_asks_the_invitee_by_push_without_an_activity_row(self):
        newcomer = make_user('newcomer')
        invitation = ClubInvitation.objects.create(club=self.club, sender=self.owner, receiver=newcomer)
        with patch('notifications.utils.push_to_user') as push:
            notify_club_invite(invitation)
        recipient, _title, message = push.call_args.args
        self.assertEqual(recipient.pk, newcomer.pk)
        self.assertIn('Runners', message)
        self.assertEqual(push.call_args.kwargs['data'],
                         {'type': 'request', 'club_id': self.club.pk, 'club_name': self.club.name})
        self.assertEqual(push.call_args.kwargs['category'], 'social')
        self.assertEqual(self.alerts(Notification.Type.CLUB_INVITE).count(), 0)

    def test_event_alerts_answer_to_the_club_events_switch(self):
        self.member.notify_club_events = False
        self.member.save(update_fields=['notify_club_events'])
        with patch('notifications.utils.push_to_users') as push:
            notify_club_event_created(self.event)
        # The row is still written — the alerts feed is where a meetup lives,
        # and the switch is about being buzzed, not about being told at all.
        self.assertEqual(self.alerts(Notification.Type.CLUB_EVENT).count(), 2)
        self.assertEqual(push.call_args.kwargs['category'], 'club_event')
        self.assertFalse(allows(self.member, 'club_event'))
