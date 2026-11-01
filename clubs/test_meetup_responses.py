"""Members answer a meetup ("I'm in" / "Nope"), and a join is announced in the club."""
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from users.models import User
from .models import Club, ClubEvent, ClubEventResponse, ClubInvitation, ClubMember, ClubMessage


class MeetupResponseTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner = self.make_user('owner')
        self.proposer = self.make_user('proposer')
        self.amina = self.make_user('amina')
        self.dan = self.make_user('dan')
        self.outsider = self.make_user('outsider')
        self.club = Club.objects.create(name='Runners', creator=self.owner)
        ClubMember.objects.create(club=self.club, user=self.owner, role=ClubMember.Role.OWNER)
        for user in (self.proposer, self.amina, self.dan):
            ClubMember.objects.create(club=self.club, user=user)
        self.event = ClubEvent.objects.create(
            club=self.club, created_by=self.proposer, title='Saturday run',
            event_datetime=timezone.now() + timedelta(days=2),
        )
        ClubMessage.objects.create(
            club=self.club, sender=self.proposer, event=self.event,
            message_type=ClubMessage.MessageType.MEETUP_PROPOSAL,
        )
        push = patch('notifications.utils.send_push_notification')
        self.push = push.start()
        self.addCleanup(push.stop)

    def make_user(self, name):
        return User.objects.create_user(username=name, email=f'{name}@example.test')

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def answer(self, user, answer):
        return self.client_for(user).post(
            f'/api/clubs/events/{self.event.pk}/responses/', {'answer': answer}, format='json',
        )

    def card(self, viewer):
        response = self.client_for(viewer).get(f'/api/clubs/{self.club.pk}/messages/')
        self.assertEqual(response.status_code, 200, response.content)
        return next(m for m in response.json() if m['event'])['event']['responses']

    def test_answers_are_counted_and_the_card_shows_them(self):
        self.assertEqual(self.answer(self.amina, 'in').status_code, 200)
        self.assertEqual(self.answer(self.dan, 'out').status_code, 200)
        card = self.card(self.amina)
        self.assertEqual((card['in_count'], card['out_count']), (1, 1))
        self.assertEqual([p['username'] for p in card['preview']], ['amina', 'dan'])
        self.assertEqual(card['my_answer'], 'in')
        self.assertIsNone(self.card(self.owner)['my_answer'])

    def test_changing_your_mind_moves_you_rather_than_counting_you_twice(self):
        self.answer(self.amina, 'in')
        summary = self.answer(self.amina, 'out').json()
        self.assertEqual((summary['in_count'], summary['out_count'], summary['my_answer']), (0, 1, 'out'))
        summary = self.answer(self.amina, None).json()
        self.assertEqual((summary['in_count'], summary['out_count'], summary['my_answer']), (0, 0, None))
        self.assertFalse(ClubEventResponse.objects.exists())

    def test_the_full_list_splits_in_from_out(self):
        self.answer(self.amina, 'in')
        self.answer(self.owner, 'in')
        self.answer(self.dan, 'out')
        listing = self.client_for(self.dan).get(f'/api/clubs/events/{self.event.pk}/responses/').json()
        self.assertEqual({p['username'] for p in listing['in']}, {'amina', 'owner'})
        self.assertEqual([p['username'] for p in listing['out']], ['dan'])

    def test_only_members_other_than_the_proposer_may_answer(self):
        self.assertEqual(self.answer(self.proposer, 'in').status_code, 400)
        self.assertEqual(self.answer(self.outsider, 'in').status_code, 404)
        self.assertEqual(
            self.client_for(self.outsider).get(f'/api/clubs/events/{self.event.pk}/responses/').status_code, 404)
        self.assertEqual(self.answer(self.amina, 'maybe').status_code, 400)

    def test_no_answers_to_a_rejected_or_past_meetup(self):
        self.event.status = ClubEvent.Status.REJECTED
        self.event.save(update_fields=['status'])
        self.assertEqual(self.answer(self.amina, 'in').status_code, 400)
        self.event.status = ClubEvent.Status.APPROVED
        self.event.event_datetime = timezone.now() - timedelta(hours=1)
        self.event.save(update_fields=['status', 'event_datetime'])
        self.assertEqual(self.answer(self.amina, 'in').status_code, 400)

    def test_open_chats_hear_about_an_answer(self):
        with patch('clubs.responses.get_channel_layer') as layer:
            self.answer(self.amina, 'in')
        sent = layer.return_value.group_send.call_args
        self.assertEqual(sent.args[0], f'club_{self.club.pk}')
        self.assertEqual(sent.args[1]['type'], 'club.event_responses')
        self.assertEqual(sent.args[1]['responses']['in_count'], 1)
        self.assertNotIn('my_answer', sent.args[1]['responses'])


class JoinAnnouncementTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner = User.objects.create_user(username='owner', email='owner@example.test')
        self.member = User.objects.create_user(
            username='member', email='member@example.test', push_token='ExponentPushToken[member]',
        )
        self.joiner = User.objects.create_user(username='joiner', email='joiner@example.test')
        self.club = Club.objects.create(name='Runners', creator=self.owner)
        ClubMember.objects.create(club=self.club, user=self.owner, role=ClubMember.Role.OWNER)
        ClubMember.objects.create(club=self.club, user=self.member)
        push = patch('notifications.utils.push_to_users', return_value=1)
        self.push = push.start()
        self.addCleanup(push.stop)

    def test_accepting_an_invitation_posts_a_joined_line_and_tells_the_members(self):
        invitation = ClubInvitation.objects.create(club=self.club, sender=self.owner, receiver=self.joiner)
        client = APIClient()
        client.force_authenticate(self.joiner)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(f'/api/clubs/invitations/{invitation.pk}/accept/')
        self.assertEqual(response.status_code, 201, response.content)
        line = ClubMessage.objects.get(message_type=ClubMessage.MessageType.MEMBER_JOINED)
        self.assertEqual(line.sender, self.joiner)
        self.assertEqual(line.club, self.club)

        history = APIClient()
        history.force_authenticate(self.member)
        rows = history.get(f'/api/clubs/{self.club.pk}/messages/').json()
        self.assertEqual(rows[-1]['message_type'], 'member_joined')
        self.assertEqual(rows[-1]['sender']['username'], 'joiner')

        recipients, _title, body = self.push.call_args.args
        self.assertEqual([u.pk for u in recipients], [self.member.pk])
        self.assertEqual(body, '@joiner joined the club')
