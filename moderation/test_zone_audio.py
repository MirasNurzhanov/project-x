"""Zone Music is user-generated audio other people hear, so it is reportable
(App Review 1.2) — by the people who can hear it — and removable by a moderator
through the same queue and audit trail as everything else."""
from unittest import mock

from django.contrib import admin
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from friends.models import FriendRequest
from users import terms
from users.models import User

from .admin import ContentReportAdmin
from .models import ContentReport, ModerationAction
from .reasons import Action, State

PREVIEW = 'https://audio-ssl.itunes.apple.com/preview.m4a'


def make_user(name, **kwargs):
    user = User.objects.create_user(username=name, email=f'{name}@example.com', password='pw-123456', **kwargs)
    user.terms_version = terms.CURRENT_VERSION
    user.save(update_fields=['terms_version'])
    return user


def run_action(name, queryset):
    moderator = User.objects.filter(is_superuser=True).first() or User.objects.create_superuser(
        username='mod', email='mod@example.com', password='pw-123456')
    request = RequestFactory().post('/admin/')
    request.user = moderator
    model_admin = ContentReportAdmin(ContentReport, admin.site)
    with mock.patch.object(model_admin, 'message_user'):
        getattr(model_admin, name)(request, queryset)


class ZoneAudioReportTests(TestCase):
    def setUp(self):
        self.owner = make_user('owner', zone_apple_preview_url=PREVIEW, zone_apple_title='Song', zone_apple_artist='Band')
        self.friend = make_user('friend')
        FriendRequest.objects.create(sender=self.owner, receiver=self.friend, status=FriendRequest.Status.ACCEPTED)
        self.client = APIClient()
        self.client.force_authenticate(self.friend)
        self.url = f'/api/moderation/report/zone-audio/{self.owner.pk}/'

    def test_a_listener_can_report_it_and_the_track_is_snapshotted(self):
        response = self.client.post(self.url, {'reason': 'hate', 'note': 'slurs in the lyrics'}, format='json')
        self.assertEqual(response.status_code, 202, response.content)
        report = ContentReport.objects.get()
        self.assertEqual(report.target_type, ContentReport.Target.ZONE_AUDIO)
        self.assertEqual(report.target_user, self.owner)
        self.assertIn(PREVIEW, report.target_repr)
        self.assertIn('Song', report.target_repr)

    def test_somebody_who_cannot_hear_it_cannot_report_it(self):
        stranger = make_user('stranger')
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.post(self.url, {'reason': 'spam'}, format='json').status_code, 404)

    def test_there_has_to_be_a_track(self):
        User.objects.filter(pk=self.owner.pk).update(zone_apple_preview_url='')
        self.assertEqual(self.client.post(self.url, {'reason': 'spam'}, format='json').status_code, 404)
        self.assertFalse(ContentReport.objects.exists())

    def test_a_moderator_removes_it_through_the_queue(self):
        self.client.post(self.url, {'reason': 'hate'}, format='json')
        moderator = User.objects.create_superuser(username='mod', email='mod@example.com', password='pw-123456')
        request = RequestFactory().post('/admin/')
        request.user = moderator
        model_admin = ContentReportAdmin(ContentReport, admin.site)
        with mock.patch.object(model_admin, 'message_user'):
            model_admin.action_remove_content(request, ContentReport.objects.all())

        self.owner.refresh_from_db()
        self.assertFalse(self.owner.has_zone_track)
        report = ContentReport.objects.get()
        self.assertEqual(report.state, State.ACTIONED)
        self.assertEqual(report.action_taken, Action.CONTENT_REMOVED)
        self.assertTrue(ModerationAction.objects.filter(subject=self.owner).exists())
        # The evidence survives the removal.
        self.assertIn(PREVIEW, report.target_repr)

    def test_it_is_in_the_same_queue_as_every_other_report(self):
        self.client.post(self.url, {'reason': 'hate'}, format='json')
        self.assertIn(ContentReport, admin.site._registry)
        self.assertIsInstance(admin.site._registry[ContentReport], ContentReportAdmin)
        self.assertEqual(ContentReport.objects.filter(target_type=ContentReport.Target.ZONE_AUDIO).count(), 1)

    def test_the_uploader_can_be_suspended_from_the_report(self):
        self.client.post(self.url, {'reason': 'hate'}, format='json')
        run_action('action_suspend_7d', ContentReport.objects.all())
        self.owner.refresh_from_db()
        self.assertIsNotNone(self.owner.suspended_at)
        self.assertIsNotNone(self.owner.suspended_until)
        self.assertEqual(ContentReport.objects.get().action_taken, Action.USER_SUSPENDED)

    def test_the_uploader_can_be_banned_from_the_report(self):
        self.client.post(self.url, {'reason': 'hate'}, format='json')
        run_action('action_ban', ContentReport.objects.all())
        self.owner.refresh_from_db()
        self.assertIsNotNone(self.owner.suspended_at)
        self.assertIsNone(self.owner.suspended_until)
        self.assertEqual(ContentReport.objects.get().action_taken, Action.USER_BANNED)


class ZoneAudioAutoMuteTests(TestCase):
    """Three distinct listeners reporting mutes the track for everyone but its
    owner until a moderator decides — the Zone Music counterpart of the
    quarantine stories, comments and club messages get."""

    def setUp(self):
        from users import visibility

        self.visibility = visibility
        self.owner = make_user('owner', zone_apple_preview_url=PREVIEW, zone_apple_title='Song')
        self.listeners = []
        for i in range(3):
            listener = make_user(f'listener{i}')
            FriendRequest.objects.create(sender=self.owner, receiver=listener, status=FriendRequest.Status.ACCEPTED)
            self.listeners.append(listener)
        self.url = f'/api/moderation/report/zone-audio/{self.owner.pk}/'

    def report_from(self, listener):
        client = APIClient()
        client.force_authenticate(listener)
        return client.post(self.url, {'reason': 'hate'}, format='json').status_code

    def audible_to(self, listener):
        self.owner.refresh_from_db()
        return self.visibility.can_view(listener, self.owner, 'zone_audio')

    def test_two_reports_do_not_mute_three_do(self):
        self.report_from(self.listeners[0])
        self.report_from(self.listeners[1])
        self.assertTrue(self.audible_to(self.listeners[2]))
        self.report_from(self.listeners[2])
        self.assertFalse(self.audible_to(self.listeners[0]))
        self.assertEqual(self.owner.zone_audio_moderation, 'quarantined')
        self.assertFalse(self.owner.broadcasts_zone_track)
        # Its owner still has it, and is told why nobody hears it.
        self.assertTrue(self.visibility.can_view(self.owner, self.owner, 'zone_audio'))
        client = APIClient()
        client.force_authenticate(self.owner)
        self.assertEqual(client.get('/api/users/zone/').json()['zone_audio_moderation'], 'quarantined')
        self.assertTrue(ModerationAction.objects.filter(subject=self.owner, detail__contains='muted automatically').exists())

    def test_a_moderator_restore_unmutes_it(self):
        for listener in self.listeners:
            self.report_from(listener)
        run_action('action_restore_content', ContentReport.objects.all())
        self.assertTrue(self.audible_to(self.listeners[0]))
        self.assertEqual(ContentReport.objects.filter(state=State.ACTIONED).count(), 1)

    def test_a_moderator_removal_takes_it_down_for_good(self):
        for listener in self.listeners:
            self.report_from(listener)
        run_action('action_remove_content', ContentReport.objects.all()[:1])
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.has_zone_track)
        self.assertEqual(self.owner.zone_audio_moderation, 'removed')

    def test_a_new_track_starts_unmoderated(self):
        for listener in self.listeners:
            self.report_from(listener)
        User.objects.filter(pk=self.owner.pk).update(zone_apple_preview_url='')
        with mock.patch('users.applemusic.get_song', return_value={
                'id': '1', 'title': 'Other', 'artist': 'A', 'artwork': '', 'preview_url': PREVIEW + '?2'}):
            client = APIClient()
            client.force_authenticate(self.owner)
            response = client.post('/api/users/zone/apple-music/', {'song_id': '1'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(self.audible_to(self.listeners[0]))
