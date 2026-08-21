"""Throttling, reporting and the alerts engagement raises.

These cover the three gaps left open after the foundation landed. The
throttling tests are the ones worth reading twice: the reveal endpoint was
enumerable until this existed, and the limit has to hold against refusals as
well as grants or it protects nothing.
"""
from unittest.mock import patch

from django.urls import reverse
from rest_framework.settings import api_settings
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from notifications.models import Notification

from .models import SwiftReport, SwiftVideo
from .testing import SwiftTestCase, block, make_swift, make_user


def slow_rates(**rates):
    """Lower the throttle rates for one test.

    **Not** `override_settings(REST_FRAMEWORK=...)`, which silently does
    nothing here and cost an afternoon to work out. DRF binds
    `APIView.throttle_classes` and `SimpleRateThrottle.THROTTLE_RATES` as class
    attributes at *import* time, so a settings override applied afterwards
    never reaches either — every request keeps running at the real rate and the
    test passes for the wrong reason, or fails for one.

    Patching the rate table directly is what actually takes effect, and it has
    the useful side effect of leaving the wiring alone: these tests exercise
    the throttle classes the project really loaded, at a rate a test can reach.
    """
    return patch.dict(SimpleRateThrottle.THROTTLE_RATES, rates)


REVEAL_LIMIT = 3


class RevealThrottleTests(SwiftTestCase):
    """The limit that stops the reveal endpoint being a coordinate scraper."""

    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')
        self.viewer = make_user('viewer')
        self.client = APIClient()
        self.client.force_authenticate(self.viewer)

    @slow_rates(swift_location_reveal='3/hour', swift_create='3/hour')
    def test_reveals_are_throttled(self):
        swift = make_swift(self.creator, located=True, shared=True)
        self.client.post(reverse('swift-view', args=[swift.pk]))
        url = reverse('swift-reveal-location', args=[swift.pk])

        for _ in range(3):
            self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 429)

    @slow_rates(swift_location_reveal='3/hour', swift_create='3/hour')
    def test_refused_reveals_spend_quota_too(self):
        """The attack this exists to stop is made of 404s.

        Somebody walking the id space never gets a 200 — so a throttle that
        only counted successful reveals would let them enumerate the entire
        table at full speed. Probing has to cost the same as asking.
        """
        for probe in range(3):
            response = self.client.post(
                reverse('swift-reveal-location', args=[900_000 + probe]))
            self.assertEqual(response.status_code, 404)

        # A real, earned reveal — refused now, because the probing spent it.
        swift = make_swift(self.creator, located=True, shared=True)
        self.client.post(reverse('swift-view', args=[swift.pk]))
        self.assertEqual(
            self.client.post(
                reverse('swift-reveal-location', args=[swift.pk])).status_code, 429,
        )

    @slow_rates(swift_location_reveal='3/hour', swift_create='3/hour')
    def test_the_limit_is_per_user_not_global(self):
        """One noisy account must not lock everybody else out."""
        swift = make_swift(self.creator, located=True, shared=True)
        url = reverse('swift-reveal-location', args=[swift.pk])
        self.client.post(reverse('swift-view', args=[swift.pk]))
        for _ in range(4):
            self.client.post(url)
        self.assertEqual(self.client.post(url).status_code, 429)

        other = make_user('other')
        second = APIClient()
        second.force_authenticate(other)
        second.post(reverse('swift-view', args=[swift.pk]))
        self.assertEqual(second.post(url).status_code, 200)

    def test_the_throttle_is_actually_wired(self):
        """Guard on the configuration itself.

        `slow_rates` patches a rate table; it cannot conjure a throttle class
        that settings never loaded. So this asserts the wiring separately —
        otherwise deleting `DEFAULT_THROTTLE_CLASSES` from settings would make
        the reveal endpoint enumerable again and only fail these tests for a
        reason nobody would connect to the cause.
        """
        from .views import RevealSwiftLocationView

        self.assertIn(ScopedRateThrottle, api_settings.DEFAULT_THROTTLE_CLASSES)
        self.assertEqual(
            RevealSwiftLocationView.throttle_scope, 'swift_location_reveal')
        self.assertIn(
            'swift_location_reveal', SimpleRateThrottle.THROTTLE_RATES)

    def test_unthrottled_endpoints_are_untouched(self):
        """`ScopedRateThrottle` is inert without a scope.

        This is what made it safe to switch throttling on for a project that
        has never had it — the feed is polled hard and must not start 429ing.
        """
        make_swift(self.creator)
        for _ in range(30):
            self.assertEqual(
                self.client.get(reverse('swift-feed')).status_code, 200)

    @slow_rates(swift_location_reveal='3/hour', swift_create='3/hour')
    def test_posting_is_throttled(self):
        payload = {'video_url': 'https://cdn.example.com/v/x.mp4', 'duration_ms': 1000}
        client = APIClient()
        client.force_authenticate(self.creator)

        for _ in range(3):
            self.assertEqual(
                client.post(reverse('swift-create'), payload, format='json').status_code,
                201,
            )
        self.assertEqual(
            client.post(reverse('swift-create'), payload, format='json').status_code, 429,
        )


class ReportTests(SwiftTestCase):
    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')
        self.reporter = make_user('reporter')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.reporter)
        self.url = reverse('swift-report', args=[self.swift.pk])

    def test_reporting_creates_a_row_and_flags_the_swift(self):
        response = self.client.post(
            self.url, {'reason': SwiftReport.Reason.SPAM}, format='json')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(SwiftReport.objects.count(), 1)
        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, SwiftVideo.Moderation.FLAGGED)

    def test_flagging_does_not_hide_the_swift(self):
        """The anti-brigading property, asserted.

        If reporting removed content, one account could silence any other on
        demand — and a handful of coordinated ones could silence anybody.
        """
        self.client.post(self.url, {'reason': SwiftReport.Reason.SPAM}, format='json')

        body = self.client.get(reverse('swift-feed')).json()
        self.assertIn(self.swift.pk, [r['id'] for r in body['results']])

    def test_reporting_five_times_leaves_one_row(self):
        for _ in range(5):
            self.client.post(
                self.url, {'reason': SwiftReport.Reason.SPAM}, format='json')

        self.assertEqual(SwiftReport.objects.count(), 1)

    def test_repeat_reports_are_indistinguishable_from_the_first(self):
        first = self.client.post(
            self.url, {'reason': SwiftReport.Reason.SPAM}, format='json')
        second = self.client.post(
            self.url, {'reason': SwiftReport.Reason.SPAM}, format='json')

        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.json(), second.json())

    def test_a_reason_is_required(self):
        self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 400)

    def test_an_unknown_reason_is_rejected(self):
        response = self.client.post(self.url, {'reason': 'because'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_cannot_report_your_own_swift(self):
        client = APIClient()
        client.force_authenticate(self.creator)
        response = client.post(
            self.url, {'reason': SwiftReport.Reason.SPAM}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(SwiftReport.objects.count(), 0)

    def test_cannot_report_a_swift_you_cannot_watch(self):
        private = make_swift(self.creator, visibility=SwiftVideo.Visibility.PRIVATE)
        response = self.client.post(
            reverse('swift-report', args=[private.pk]),
            {'reason': SwiftReport.Reason.SPAM}, format='json',
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(SwiftReport.objects.count(), 0)

    def test_a_report_does_not_undo_a_moderator_removal(self):
        """A removed Swift must not come back to a visible state."""
        self.swift.moderation = SwiftVideo.Moderation.REMOVED
        self.swift.save(update_fields=['moderation'])

        self.client.post(self.url, {'reason': SwiftReport.Reason.SPAM}, format='json')

        self.swift.refresh_from_db()
        self.assertEqual(self.swift.moderation, SwiftVideo.Moderation.REMOVED)

    def test_privacy_is_a_reportable_reason(self):
        """The one reason that is about the location, not the video."""
        response = self.client.post(
            self.url, {'reason': SwiftReport.Reason.PRIVACY, 'note': 'that is my street'},
            format='json',
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(SwiftReport.objects.get().note, 'that is my street')


class EngagementAlertTests(SwiftTestCase):
    """Likes, comments and reposts write an alert for the creator."""

    def setUp(self):
        super().setUp()
        self.creator = make_user('creator')
        self.actor = make_user('actor')
        self.swift = make_swift(self.creator)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def alerts(self, kind=None):
        qs = Notification.objects.filter(recipient=self.creator)
        return qs.filter(notification_type=kind) if kind else qs

    def test_like_alerts_the_creator(self):
        self.client.post(reverse('swift-like', args=[self.swift.pk]))

        alert = self.alerts(Notification.Type.SWIFT_LIKE).get()
        self.assertEqual(alert.swift_id, self.swift.pk)
        self.assertEqual(alert.sender_id, self.actor.pk)
        self.assertIn('liked your Swift', alert.message)

    def test_comment_alert_carries_an_excerpt(self):
        self.client.post(
            reverse('swift-comments', args=[self.swift.pk]),
            {'text': 'this is great'}, format='json',
        )

        alert = self.alerts(Notification.Type.SWIFT_COMMENT).get()
        self.assertIn('this is great', alert.message)

    def test_repost_alerts_the_creator(self):
        self.client.post(reverse('swift-repost', args=[self.swift.pk]))
        self.assertEqual(self.alerts(Notification.Type.SWIFT_REPOST).count(), 1)

    def test_repeat_likes_do_not_stack_alerts(self):
        """One person is one piece of news, however many times they tap."""
        for _ in range(4):
            self.client.post(reverse('swift-like', args=[self.swift.pk]))

        self.assertEqual(self.alerts(Notification.Type.SWIFT_LIKE).count(), 1)

    def test_a_like_does_not_overwrite_a_comment_alert(self):
        """Why the three kinds are three types rather than one.

        Coalescing is keyed on (recipient, sender, swift, type). Folding them
        into one `swift_activity` type would mean a later like silently
        rewrote the comment alert, and the author would never learn about the
        comment.
        """
        self.client.post(
            reverse('swift-comments', args=[self.swift.pk]),
            {'text': 'hello'}, format='json',
        )
        self.client.post(reverse('swift-like', args=[self.swift.pk]))

        self.assertEqual(self.alerts().count(), 2)
        self.assertEqual(self.alerts(Notification.Type.SWIFT_COMMENT).count(), 1)
        self.assertEqual(self.alerts(Notification.Type.SWIFT_LIKE).count(), 1)

    def test_liking_your_own_swift_is_not_news(self):
        client = APIClient()
        client.force_authenticate(self.creator)
        client.post(reverse('swift-like', args=[self.swift.pk]))

        self.assertEqual(self.alerts().count(), 0)

    def test_a_blocked_actor_raises_no_alert(self):
        block(self.creator, self.actor)
        self.client.post(reverse('swift-like', args=[self.swift.pk]))

        self.assertEqual(self.alerts().count(), 0)

    def test_unliking_and_reliking_does_not_re_push(self):
        """The standing unread row is refreshed, not duplicated."""
        url = reverse('swift-like', args=[self.swift.pk])
        self.client.post(url)
        self.client.delete(url)
        self.client.post(url)

        self.assertEqual(self.alerts(Notification.Type.SWIFT_LIKE).count(), 1)

    def test_alert_appears_in_the_notifications_feed_with_its_swift(self):
        self.client.post(reverse('swift-like', args=[self.swift.pk]))

        client = APIClient()
        client.force_authenticate(self.creator)
        body = client.get(reverse('notification-list')).json()
        rows = body['results'] if isinstance(body, dict) else body

        self.assertEqual(rows[0]['swift_id'], self.swift.pk)
        self.assertEqual(rows[0]['notification_type'], 'swift_like')

    def test_push_respects_the_preference_switch(self):
        self.creator.notify_swift_activity = False
        self.creator.push_token = 'ExponentPushToken[xxx]'
        self.creator.save(update_fields=['notify_swift_activity', 'push_token'])

        with patch('notifications.push._post') as sender:
            self.client.post(reverse('swift-like', args=[self.swift.pk]))

        sender.assert_not_called()
        # The alert row is still written — the switch silences the buzz, not
        # the record. Same split every other category here makes.
        self.assertEqual(self.alerts(Notification.Type.SWIFT_LIKE).count(), 1)
