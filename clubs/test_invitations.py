"""H2: an owner can ask someone to join a club, never add them, and blocks or privacy hide the invitee."""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from friends.models import BlockedUser, FriendRequest, HiRequest
from notifications.badges import navigation_badges
from notifications.models import Notification
from users.models import User
from .models import Club, ClubInvitation, ClubMember


class ClubInvitationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner = self.make_user('owner')
        self.invitee = self.make_user('invitee')
        self.club = Club.objects.create(name='Runners', creator=self.owner)
        ClubMember.objects.create(club=self.club, user=self.owner, role=ClubMember.Role.OWNER)
        push = patch('notifications.utils.send_push_notification')
        self.push = push.start()
        self.addCleanup(push.stop)

    def make_user(self, name, **fields):
        return User.objects.create_user(username=name, email=f'{name}@example.test', **fields)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def invite(self, target_id, by=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client_for(by or self.owner).post(f'/api/clubs/{self.club.pk}/invite/{target_id}/')

    def answer(self, invitation_id, action, user=None):
        return self.client_for(user or self.invitee).post(f'/api/clubs/invitations/{invitation_id}/{action}/')

    def is_member(self, user):
        return ClubMember.objects.filter(club=self.club, user=user).exists()

    def test_an_invitation_is_pending_until_the_invitee_accepts(self):
        response = self.invite(self.invitee.pk)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], ClubInvitation.Status.PENDING)
        self.assertFalse(self.is_member(self.invitee))
        self.assertEqual(
            self.client_for(self.invitee).get(f'/api/clubs/{self.club.pk}/messages/').status_code, 404)

        self.push.assert_called_once()
        recipient, _title, message = self.push.call_args.args
        self.assertEqual(recipient.pk, self.invitee.pk)
        self.assertIn('invited you to join Runners', message)
        self.assertEqual(self.push.call_args.kwargs['data']['type'], 'request')
        self.assertEqual(self.push.call_args.kwargs['data']['club_id'], self.club.pk)
        self.assertFalse(Notification.objects.filter(notification_type=Notification.Type.CLUB_INVITE).exists())

        listed = self.client_for(self.invitee).get('/api/clubs/invitations/')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([(i['id'], i['club']['name'], i['sender']['username']) for i in listed.data],
                         [(response.data['id'], 'Runners', 'owner')])
        self.assertEqual(self.client_for(self.owner).get('/api/clubs/invitations/').data, [])

        accepted = self.answer(response.data['id'], 'accept')
        self.assertEqual(accepted.status_code, 201)
        self.assertTrue(self.is_member(self.invitee))
        self.assertEqual(ClubInvitation.objects.get().status, ClubInvitation.Status.ACCEPTED)
        self.assertEqual(
            self.client_for(self.invitee).get(f'/api/clubs/{self.club.pk}/messages/').status_code, 200)
        self.assertEqual(self.answer(response.data['id'], 'accept').status_code, 404)
        self.assertEqual(ClubMember.objects.filter(club=self.club, user=self.invitee).count(), 1)

    def test_repeat_invites_keep_one_invitation_and_notify_once(self):
        first, repeat = self.invite(self.invitee.pk), self.invite(self.invitee.pk)
        self.assertEqual([first.status_code, repeat.status_code], [201, 200])
        self.assertEqual(first.data['id'], repeat.data['id'])
        self.assertEqual(ClubInvitation.objects.count(), 1)
        self.push.assert_called_once()

    def test_a_decline_stands_against_the_owner(self):
        invitation_id = self.invite(self.invitee.pk).data['id']
        self.assertEqual(self.answer(invitation_id, 'decline').status_code, 204)
        self.assertFalse(self.is_member(self.invitee))
        self.push.reset_mock()

        again = self.invite(self.invitee.pk)
        self.assertEqual((again.status_code, again.data['status']), (200, ClubInvitation.Status.DECLINED))
        self.push.assert_not_called()
        self.assertEqual(self.answer(invitation_id, 'accept').status_code, 404)
        self.assertEqual(self.answer(invitation_id, 'decline').status_code, 404)
        self.assertEqual(self.client_for(self.invitee).get('/api/clubs/invitations/').data, [])
        self.assertFalse(self.is_member(self.invitee))

    def test_missing_blocked_self_inactive_and_private_accounts_are_indistinguishable(self):
        missing = self.invite(999999)
        self.assertEqual(missing.status_code, 404)

        inactive = self.make_user('inactive', is_active=False)
        private = self.make_user('private', discoverable_by_username=False)
        cases = [('self', self.owner.pk), ('inactive', inactive.pk), ('private stranger', private.pk)]
        for label, target_id in cases:
            with self.subTest(label):
                response = self.invite(target_id)
                self.assertEqual((response.status_code, response.data), (missing.status_code, missing.data))

        for blocker, blocked in ((self.owner, self.invitee), (self.invitee, self.owner)):
            with self.subTest(blocker=blocker.username):
                block = BlockedUser.objects.create(blocker=blocker, blocked=blocked)
                response = self.invite(self.invitee.pk)
                self.assertEqual((response.status_code, response.data), (missing.status_code, missing.data))
                block.delete()

        self.assertFalse(ClubInvitation.objects.exists())
        self.push.assert_not_called()

    def test_friends_and_connections_can_be_invited_after_opting_out_of_search(self):
        friend = self.make_user('friend', discoverable_by_username=False)
        FriendRequest.objects.create(sender=friend, receiver=self.owner, status=FriendRequest.Status.ACCEPTED)
        connection = self.make_user('connection', discoverable_by_username=False)
        HiRequest.objects.create(sender=self.owner, receiver=connection, status=HiRequest.Status.ACCEPTED)
        for user in (friend, connection):
            with self.subTest(user.username):
                self.assertEqual(self.invite(user.pk).status_code, 201)

    def test_a_block_after_the_invitation_withdraws_it(self):
        for blocker, blocked in ((self.invitee, self.owner), (self.owner, self.invitee)):
            with self.subTest(blocker=blocker.username):
                ClubInvitation.objects.all().delete()
                invitation_id = self.invite(self.invitee.pk).data['id']
                block = BlockedUser.objects.create(blocker=blocker, blocked=blocked)
                self.assertEqual(self.client_for(self.invitee).get('/api/clubs/invitations/').data, [])
                self.assertEqual(navigation_badges(self.invitee)['alerts'], 0)
                self.assertEqual(self.answer(invitation_id, 'accept').status_code, 404)
                self.assertEqual(self.answer(invitation_id, 'decline').status_code, 404)
                self.assertFalse(self.is_member(self.invitee))
                block.delete()

    def test_only_owners_invite_and_members_are_not_invited_again(self):
        member = self.make_user('member')
        ClubMember.objects.create(club=self.club, user=member)
        self.assertEqual(self.invite(self.invitee.pk, by=member).status_code, 403)
        self.assertEqual(self.invite(member.pk).status_code, 400)
        self.assertFalse(ClubInvitation.objects.exists())
        self.push.assert_not_called()

    def test_only_the_invitee_can_answer(self):
        invitation_id = self.invite(self.invitee.pk).data['id']
        stranger = self.make_user('stranger')
        for user in (stranger, self.owner):
            with self.subTest(user.username):
                self.assertEqual(self.answer(invitation_id, 'accept', user=user).status_code, 404)
                self.assertEqual(self.answer(invitation_id, 'decline', user=user).status_code, 404)
        self.assertEqual(ClubInvitation.objects.get().status, ClubInvitation.Status.PENDING)

    def test_someone_who_left_needs_to_accept_again(self):
        invitation_id = self.invite(self.invitee.pk).data['id']
        self.answer(invitation_id, 'accept')
        self.assertEqual(self.client_for(self.invitee).post(f'/api/clubs/{self.club.pk}/leave/').status_code, 204)
        self.push.reset_mock()

        again = self.invite(self.invitee.pk)
        self.assertEqual((again.status_code, again.data['id'], again.data['status']),
                         (201, invitation_id, ClubInvitation.Status.PENDING))
        self.push.assert_called_once()
        self.assertFalse(self.is_member(self.invitee))
        self.assertEqual(self.answer(invitation_id, 'accept').status_code, 201)
        self.assertTrue(self.is_member(self.invitee))

    def test_pending_invitations_count_toward_alerts_until_answered(self):
        self.assertEqual(navigation_badges(self.invitee)['alerts'], 0)
        invitation_id = self.invite(self.invitee.pk).data['id']
        self.assertEqual(navigation_badges(self.invitee)['alerts'], 1)
        self.answer(invitation_id, 'accept')
        self.assertEqual(navigation_badges(self.invitee)['alerts'], 0)

    def test_invites_share_the_social_request_budget(self):
        other = self.make_user('other')
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'social_request': '1/min'}):
            self.assertEqual(self.invite(self.invitee.pk).status_code, 201)
            self.assertEqual(self.invite(other.pk).status_code, 429)
