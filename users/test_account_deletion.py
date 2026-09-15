from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from clubs.models import Club, ClubMessage
from core.websockets import account_can_use_socket
from friends.models import BlockedUser, HiRequest, Message
from moderation.models import ContentReport, ModerationAction
from users.deletion import GRACE_DAYS, finalize_deletion
from users.models import Device, SocialAccount, User
from users.test_socialauth import GOOGLE_AUD, APPLE_AUD, make_token, stub_keys


class AccountDeletionTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username='owner', email='owner@test.example', password='current-password')
        self.other = User.objects.create_user(username='peer', email='peer@test.example', password='peer-password')
        self.refresh = RefreshToken.for_user(self.user)
        self.access = str(self.refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.access}')

    def delete(self, **proof):
        return self.client.delete('/api/users/delete/', proof, format='json')

    def request(self):
        response = self.delete(password='current-password')
        self.assertEqual(response.status_code, 202, response.data)
        self.user.refresh_from_db()
        return response

    def test_access_token_alone_or_wrong_password_cannot_delete(self):
        for proof in ({}, {'password': 'wrong'}, {'password': ['current-password']}, {'provider': 'unknown'}):
            self.assertEqual(self.delete(**proof).status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIsNone(self.user.deletion_requested_at)

    def test_current_password_required_after_password_change(self):
        self.user.set_password('changed')
        self.user.save()
        self.assertEqual(self.delete(password='current-password').status_code, 400)
        self.assertEqual(self.delete(password='changed').status_code, 202)

    def test_request_disables_sessions_and_retains_data_during_grace(self):
        Device.objects.create(user=self.user, install_id='phone', push_token='ExpoPushToken[test]')
        message = Message.objects.create(sender=self.user, receiver=self.other, content='evidence')
        self.request()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.user.ghost_mode)
        self.assertAlmostEqual((self.user.delete_after - self.user.deletion_requested_at).total_seconds(), GRACE_DAYS * 86400)
        self.assertTrue(Message.objects.filter(pk=message.pk).exists())
        self.assertEqual(self.user.devices.get().push_token, '')
        self.assertFalse(account_can_use_socket(self.user.pk))
        self.assertEqual(self.client.get('/api/users/me/').status_code, 401)
        self.client.credentials()
        self.assertEqual(self.client.post('/api/users/token/refresh/', {'refresh': str(self.refresh)}).status_code, 401)
        self.assertEqual(self.client.post('/api/users/login/',
                                         {'email': self.user.email, 'password': 'current-password'}).status_code, 401)

    def test_pending_account_cannot_reactivate_by_is_active_alone(self):
        self.request()
        User.objects.filter(pk=self.user.pk).update(is_active=True)
        self.assertEqual(self.client.get('/api/users/me/').status_code, 401)
        self.assertFalse(account_can_use_socket(self.user.pk))

    def test_deletion_methods_match_password_and_social_account(self):
        self.assertEqual(self.client.get('/api/users/delete/').data['has_password'], True)
        self.user.set_unusable_password()
        self.user.save()
        SocialAccount.objects.create(user=self.user, provider='apple', subject='sub-1')
        self.assertEqual(self.client.get('/api/users/delete/').data['providers'], ['apple'])
        self.assertFalse(self.client.get('/api/users/delete/').data['has_password'])
        self.assertEqual(self.delete(password='').status_code, 400)

    def test_pending_profile_and_map_are_not_visible(self):
        self.request()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(f'/api/users/{self.user.pk}/profile/').status_code, 404)
        from users.visibility import visible_users
        self.assertFalse(visible_users(self.other).filter(pk=self.user.pk).exists())

    def test_pending_and_retired_swifts_are_hidden_by_both_visibility_paths(self):
        from swifts.models import SwiftVideo
        from swifts.visibility import can_watch, watchable
        swift = SwiftVideo.objects.create(author=self.user, video='swifts/retained.mp4',
                                          status='ready', visibility='public')
        self.request()
        # Even if somebody accidentally flips ghost mode off, deletion still wins.
        User.objects.filter(pk=self.user.pk).update(ghost_mode=False)
        swift = SwiftVideo.objects.select_related('author').get(pk=swift.pk)
        self.assertFalse(can_watch(self.other, swift))
        self.assertFalse(watchable(self.other).filter(pk=swift.pk).exists())

    def test_active_or_unrequested_accounts_are_never_finalized(self):
        User.objects.filter(pk=self.user.pk).update(delete_after=timezone.now() - timedelta(days=1))
        self.assertFalse(finalize_deletion(self.user.pk))
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertFalse(finalize_deletion(self.user.pk))

    def test_deletion_is_throttled(self):
        for _ in range(5):
            self.assertEqual(self.delete(password='wrong').status_code, 400)
        self.assertEqual(self.delete(password='wrong').status_code, 429)

    def test_no_hard_delete_before_deadline_and_dry_run_is_read_only(self):
        self.request()
        self.assertFalse(finalize_deletion(self.user.pk))
        User.objects.filter(pk=self.user.pk).update(delete_after=timezone.now() - timedelta(seconds=1))
        output = StringIO()
        call_command('purge_deleted_accounts', stdout=output)
        self.assertIn('Dry run: 1', output.getvalue())
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_due_purge_preserves_both_message_evidence_and_reports(self):
        original_id = self.user.pk
        message = Message.objects.create(sender=self.user, receiver=self.other, content='evidence', image='chat_images/evidence.jpg')
        reply = Message.objects.create(sender=self.other, receiver=self.user, content='reply')
        report = ContentReport.objects.create(reporter=self.user, target_user=self.other,
            target_type='message', target_id=reply.pk, target_repr='reply', reason='harassment')
        action = ModerationAction.objects.create(subject=self.user, moderator=self.other, kind='user_reinstated', detail='audit')
        club = Club.objects.create(name='Shared club', creator=self.user)
        club_message = ClubMessage.objects.create(club=club, sender=self.user, content='club evidence')
        SocialAccount.objects.create(user=self.user, provider='google', subject='sub-1')
        Device.objects.create(user=self.user, install_id='phone')
        self.request()
        User.objects.filter(pk=original_id).update(delete_after=timezone.now() - timedelta(seconds=1))
        call_command('purge_deleted_accounts', execute=True, stdout=StringIO())
        self.assertFalse(User.objects.filter(pk=original_id).exists())
        retired = User.objects.get(retired_account_id=original_id)
        self.assertFalse(retired.is_active)
        self.assertFalse(retired.has_usable_password())
        self.assertFalse(retired.social_accounts.exists())
        self.assertFalse(retired.devices.exists())
        self.assertIsNone(retired.date_of_birth)
        self.assertIsNone(retired.latitude)
        message.refresh_from_db()
        reply.refresh_from_db()
        report.refresh_from_db()
        action.refresh_from_db()
        club_message.refresh_from_db()
        self.assertEqual(message.sender_id, retired.pk)
        self.assertEqual(message.image.name, 'chat_images/evidence.jpg')
        self.assertFalse(message.deleted_for_receiver)
        self.assertFalse(reply.deleted_for_sender)
        self.assertEqual(report.target_repr, 'reply')
        self.assertEqual(report.reporter_id, retired.pk)
        self.assertEqual(action.subject_id, retired.pk)
        self.assertEqual(club_message.sender_id, retired.pk)
        self.client.force_authenticate(self.other)
        response = self.client.get(f'/api/friends/messages/{original_id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 2)
        self.assertEqual(response.data['results'][0]['sender']['username'], 'Deleted account')
        self.assertFalse(finalize_deletion(original_id))

    def test_reported_swifts_and_stories_survive_finalization(self):
        from swifts.models import SwiftVideo, SwiftReport
        from stories.models import Story
        swift = SwiftVideo.objects.create(author=self.user, video='swifts/evidence.mp4')
        report = SwiftReport.objects.create(swift=swift, reporter=self.other, reason='harassment')
        story = Story.objects.create(user=self.user, image='stories/evidence.jpg')
        self.request()
        self.assertTrue(finalize_deletion(self.user.pk, now=self.user.delete_after))
        swift.refresh_from_db()
        report.refresh_from_db()
        story.refresh_from_db()
        self.assertEqual(report.swift_id, swift.pk)
        self.assertEqual(swift.video.name, 'swifts/evidence.mp4')
        self.assertEqual(story.user_id, swift.author_id)

    def test_failed_purge_rolls_back_transfers(self):
        message = Message.objects.create(sender=self.user, receiver=self.other, content='keep')
        self.request()
        with patch.object(User, 'delete', side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(RuntimeError):
                finalize_deletion(self.user.pk, now=self.user.delete_after)
        message.refresh_from_db()
        self.assertEqual(message.sender_id, self.user.pk)
        self.assertFalse(User.objects.filter(retired_account_id=self.user.pk).exists())

    def test_existing_blocks_survive_purge(self):
        BlockedUser.objects.create(blocker=self.other, blocked=self.user)
        self.request()
        finalize_deletion(self.user.pk, now=self.user.delete_after)
        retired = User.objects.get(retired_account_id=self.user.pk)
        self.assertTrue(BlockedUser.objects.filter(blocker=self.other, blocked=retired).exists())


@override_settings(GOOGLE_OAUTH_CLIENT_IDS=(GOOGLE_AUD,), APPLE_SIGN_IN_CLIENT_IDS=(APPLE_AUD,))
class SocialDeletionTests(APITestCase):
    delete = AccountDeletionTests.delete
    # Separate class uses the real JWT verifier with locally signed tokens.
    def setUp(self):
        AccountDeletionTests.setUp(self)
        self.user.set_unusable_password()
        self.user.save()
        for patcher in stub_keys():
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fresh_linked_google_and_apple_proofs(self):
        for provider, issuer, audience in (
            ('google', 'https://accounts.google.com', GOOGLE_AUD),
            ('apple', 'https://appleid.apple.com', APPLE_AUD),
        ):
            cache.clear()
            user = User.objects.create_user(username=provider, email=f'{provider}@test.example')
            SocialAccount.objects.create(user=user, provider=provider, subject='sub-1')
            self.client.force_authenticate(user)
            self.assertEqual(self.delete(provider=provider, id_token=make_token(iss=issuer, aud=audience)).status_code, 202)

    def test_other_identity_stale_and_expired_proofs_rejected(self):
        SocialAccount.objects.create(user=self.user, provider='google', subject='sub-1')
        for token in (
            make_token(sub='different'),
            make_token(iat=int(timezone.now().timestamp()) - 301),
            make_token(exp_delta=-120),
            'forged',
        ):
            self.assertEqual(self.delete(provider='google', id_token=token).status_code, 400)
        self.assertTrue(User.objects.get(pk=self.user.pk).is_active)

    def test_pending_social_account_cannot_sign_in(self):
        SocialAccount.objects.create(user=self.user, provider='google', subject='sub-1')
        token = make_token()
        self.assertEqual(self.delete(provider='google', id_token=token).status_code, 202)
        self.client.credentials()
        response = self.client.post('/api/users/social/', {'provider': 'google', 'id_token': token})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('tokens', response.data)


@skipUnlessDBFeature('has_select_for_update')
class DeletionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='concurrent', email='concurrent@test.example', password='password')

    def together(self, function):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_deletion_requests_schedule_only_once(self):
        from rest_framework.exceptions import ValidationError
        from users.deletion import request_deletion

        def request():
            try:
                request_deletion(self.user.pk, {'password': 'password'})
                return True
            except ValidationError:
                return False

        self.assertEqual(sorted(self.together(request)), [False, True])
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertIsNotNone(self.user.delete_after)

    def test_concurrent_purges_create_one_retired_identity(self):
        from users.deletion import request_deletion
        user = request_deletion(self.user.pk, {'password': 'password'})
        results = self.together(lambda: finalize_deletion(self.user.pk, now=user.delete_after))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(User.objects.filter(retired_account_id=self.user.pk).count(), 1)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
