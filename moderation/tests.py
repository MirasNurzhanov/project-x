"""What App Review asked for, as tests.

Organised by the four things Guideline 1.2 requires rather than by module, so a
gap in the requirement is visible as a missing class rather than as a missing
assertion three files away:

    FilterTests             a method for filtering objectionable content
    ReportEndpointTests     a mechanism to report offensive content
    BlockSignalTests        blocking, and the moderation signal it raises
    EnforcementTests        suspension actually stopping a suspended account
    TermsTests              explicit agreement, recorded, versioned
"""
from datetime import date, timedelta

from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from friends.models import BlockedUser, FriendRequest, HiRequest, Message
from stories.models import Story
from swifts.models import SwiftComment, SwiftVideo
from users import terms
from users.models import User

from .models import ContentReport, ModerationAction
from .reasons import AUTO_QUARANTINE_REPORTS, Moderation, Reason, State

#: What the tests below claim to be unless they say otherwise. Kept at the
#: enforced floor rather than at a specific release, so raising
#: TERMS_ENFORCEMENT_MIN_BUILD does not silently move these onto the
#: legacy path and stop testing the gate.
CURRENT_BUILD = 32
from . import services, text


def make_user(name, **kwargs):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='pw-a-real-one-1',
        date_of_birth=date(1995, 5, 5), **kwargs,
    )


def accepted(user):
    terms.record_acceptance(user)
    return user


# ─── E · Content filtering ────────────────────────────────────────────────────
class FilterTests(TestCase):
    """`moderation.text`, on its own.

    The negative cases matter more than the positive ones here. A filter that
    catches everything by catching ordinary speech is worse than no filter: it
    trains users to route around it and buries the queue in false positives.
    """

    def test_ordinary_text_passes(self):
        for phrase in [
            'Anyone up for coffee near the park?',
            'That set was violently good, best gig all year',
            'I could kill for a nap right now',
            'Killing it at the gym today',
            'Analysis of the match, thread below',
            'Damn, missed the bus again',
        ]:
            with self.subTest(phrase=phrase):
                self.assertTrue(text.classify(phrase).allowed, phrase)

    def test_material_involving_minors_is_blocked_outright(self):
        for phrase in [
            'selling child porn dm me',
            'underage nudes here',
            'looking for jailbait content',
        ]:
            with self.subTest(phrase=phrase):
                verdict = text.classify(phrase)
                self.assertTrue(verdict.blocked, phrase)
                self.assertEqual(verdict.reason, Reason.MINOR_SAFETY)

    def test_direct_threats_are_blocked(self):
        verdict = text.classify('i will kill you when i find you')
        self.assertTrue(verdict.blocked)
        self.assertEqual(verdict.reason, Reason.VIOLENCE)

    def test_evasion_by_spacing_digits_and_repeats_does_not_help(self):
        """The normaliser is most of the filter. Without it a denylist stops
        nobody, because the same string arrives spelled six ways."""
        for phrase in [
            'c h i l d   p o r n',
            'ch1ld p0rn',
            'chiiild pooorn',
            'C.H.I.L.D. P.O.R.N.',
        ]:
            with self.subTest(phrase=phrase):
                self.assertTrue(text.classify(phrase).blocked, phrase)

    def test_borderline_hate_is_flagged_not_blocked(self):
        """Flag, not block: the same sentence can be an attack or somebody
        quoting one to report it, and a filter confident enough to refuse both
        is wrong regularly."""
        verdict = text.classify('kill yourself')
        self.assertTrue(verdict.flagged)
        self.assertFalse(verdict.blocked)
        self.assertEqual(verdict.reason, Reason.SELF_HARM)

    def test_empty_and_whitespace_are_allowed(self):
        for value in ['', '   ', None]:
            self.assertTrue(text.classify(value).allowed)

    def test_classify_many_takes_the_strongest_verdict(self):
        verdict = text.classify_many('a normal caption', 'child porn')
        self.assertTrue(verdict.blocked)

    def test_a_deployment_can_add_its_own_terms_without_a_release(self):
        with self.settings(MODERATION_EXTRA_BLOCK=[r'\bnewabusewave\b']):
            self.assertTrue(text.classify('this is a newabusewave post').blocked)
        # And the addition is scoped to the override, not baked in.
        self.assertTrue(text.classify('this is a newabusewave post').allowed)


class FilterIntegrationTests(TestCase):
    """The filter where it actually has to hold: on the write paths."""

    def setUp(self):
        self.author = accepted(make_user('author'))
        self.client = APIClient()
        self.client.force_authenticate(self.author)

    def test_a_blocked_story_caption_is_refused_and_nothing_is_stored(self):
        res = self.client.post('/api/stories/', {
            'content': 'selling child porn dm me',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'objectionable_content')
        self.assertFalse(Story.objects.exists())

    def test_a_flagged_story_is_stored_quarantined_rather_than_published(self):
        res = self.client.post('/api/stories/', {
            'content': 'kill yourself',
        }, format='json')
        self.assertEqual(res.status_code, 201)
        story = Story.objects.get()
        self.assertEqual(story.moderation, Moderation.QUARANTINED)
        # And it is in the queue, not merely hidden - content nothing lists is
        # content nobody reviews.
        self.assertTrue(
            ContentReport.objects.filter(
                target_type=ContentReport.Target.STORY,
                target_id=story.pk,
                source=ContentReport.Source.AUTOMATED,
            ).exists()
        )

    def test_a_quarantined_story_is_invisible_to_a_friend(self):
        friend = accepted(make_user('friend'))
        FriendRequest.objects.create(
            sender=self.author, receiver=friend,
            status=FriendRequest.Status.ACCEPTED)

        self.client.post('/api/stories/', {'content': 'kill yourself'}, format='json')

        viewer = APIClient()
        viewer.force_authenticate(friend)
        res = viewer.get('/api/stories/friends/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 0)

    def test_its_own_author_still_sees_it(self):
        """Content that vanishes without explanation reads as a bug, and an
        author who cannot see their own post files a support ticket."""
        self.client.post('/api/stories/', {'content': 'kill yourself'}, format='json')
        res = self.client.get('/api/stories/mine/')
        self.assertEqual(res.status_code, 200)

    def test_a_blocked_direct_message_is_refused(self):
        other = accepted(make_user('other'))
        # A *Hi*, not a friendship: `friends.views._are_connected` is what gates
        # messaging and it reads HiRequest.
        HiRequest.objects.create(
            sender=self.author, receiver=other,
            status=HiRequest.Status.ACCEPTED)
        res = self.client.post(
            f'/api/friends/messages/{other.pk}/',
            {'content': 'i will kill you'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'objectionable_content')
        self.assertFalse(Message.objects.exists())

    def test_a_blocked_bio_is_refused(self):
        res = self.client.patch(
            '/api/users/profile/', {'bio': 'child porn here'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.author.refresh_from_db()
        self.assertEqual(self.author.bio, '')


# ─── C · Reporting ────────────────────────────────────────────────────────────
class ReportEndpointTests(TestCase):
    def setUp(self):
        self.reporter = accepted(make_user('reporter'))
        self.subject = accepted(make_user('subject'))
        self.client = APIClient()
        self.client.force_authenticate(self.reporter)

    def test_reporting_a_user_stores_reason_reporter_and_evidence(self):
        self.subject.bio = 'a bio somebody objected to'
        self.subject.save(update_fields=['bio'])

        res = self.client.post(
            f'/api/users/{self.subject.pk}/report/',
            {'reason': 'hate', 'note': 'slurs in the bio'}, format='json')

        self.assertEqual(res.status_code, 202)
        report = ContentReport.objects.get()
        self.assertEqual(report.reporter, self.reporter)
        self.assertEqual(report.target_user, self.subject)
        self.assertEqual(report.reason, Reason.HATE)
        self.assertEqual(report.note, 'slurs in the bio')
        self.assertEqual(report.state, State.OPEN)
        self.assertEqual(report.source, ContentReport.Source.REPORT)

    def test_the_same_route_is_served_under_the_moderation_prefix(self):
        res = self.client.post(
            f'/api/moderation/report/user/{self.subject.pk}/',
            {'reason': 'spam'}, format='json')
        self.assertEqual(res.status_code, 202)

    def test_a_reason_is_required(self):
        res = self.client.post(
            f'/api/users/{self.subject.pk}/report/', {'note': 'bad'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('reason', res.data)

    def test_an_invented_reason_is_refused(self):
        res = self.client.post(
            f'/api/users/{self.subject.pk}/report/',
            {'reason': 'i-made-this-up'}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_reporting_twice_files_one_report(self):
        """One person is one report, however many times they press it. Letting
        a repeat count twice is the whole mechanism a brigade needs."""
        for _ in range(4):
            res = self.client.post(
                f'/api/users/{self.subject.pk}/report/',
                {'reason': 'spam'}, format='json')
            self.assertEqual(res.status_code, 202)
        self.assertEqual(ContentReport.objects.count(), 1)

    def test_the_response_does_not_reveal_whether_it_was_a_repeat(self):
        first = self.client.post(
            f'/api/users/{self.subject.pk}/report/', {'reason': 'spam'}, format='json')
        second = self.client.post(
            f'/api/users/{self.subject.pk}/report/', {'reason': 'spam'}, format='json')
        self.assertEqual(first.status_code, second.status_code)
        self.assertEqual(first.data, second.data)

    def test_you_cannot_report_yourself(self):
        res = self.client.post(
            f'/api/users/{self.reporter.pk}/report/', {'reason': 'spam'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ContentReport.objects.exists())

    def test_reporting_a_missing_target_is_a_404(self):
        res = self.client.post(
            '/api/users/999999/report/', {'reason': 'spam'}, format='json')
        self.assertEqual(res.status_code, 404)

    def test_reporting_needs_a_session(self):
        res = APIClient().post(
            f'/api/users/{self.subject.pk}/report/', {'reason': 'spam'}, format='json')
        self.assertIn(res.status_code, (401, 403))

    def test_a_report_snapshots_the_content_so_a_deletion_cannot_empty_the_queue(self):
        story = Story.objects.create(
            user=self.subject, content='the thing somebody objected to',
            expires_at=timezone.now() + timedelta(hours=24))
        FriendRequest.objects.create(
            sender=self.reporter, receiver=self.subject,
            status=FriendRequest.Status.ACCEPTED)

        res = self.client.post(
            f'/api/moderation/report/story/{story.pk}/',
            {'reason': 'nudity'}, format='json')
        self.assertEqual(res.status_code, 202)

        story.delete()
        report = ContentReport.objects.get()
        self.assertEqual(report.target_repr, 'the thing somebody objected to')

    def test_reporting_a_story_you_cannot_see_is_a_404(self):
        """Same rule as everywhere else: absence and invisibility must be
        indistinguishable, or the report endpoint becomes an oracle."""
        story = Story.objects.create(
            user=self.subject, content='friends only',
            expires_at=timezone.now() + timedelta(hours=24))
        res = self.client.post(
            f'/api/moderation/report/story/{story.pk}/',
            {'reason': 'spam'}, format='json')
        self.assertEqual(res.status_code, 404)

    def test_only_the_recipient_may_report_a_direct_message(self):
        HiRequest.objects.create(
            sender=self.reporter, receiver=self.subject,
            status=HiRequest.Status.ACCEPTED)
        mine = Message.objects.create(
            sender=self.reporter, receiver=self.subject, content='mine')
        theirs = Message.objects.create(
            sender=self.subject, receiver=self.reporter, content='theirs')

        self.assertEqual(
            self.client.post(
                f'/api/moderation/report/message/{mine.pk}/',
                {'reason': 'spam'}, format='json').status_code, 404)
        self.assertEqual(
            self.client.post(
                f'/api/moderation/report/message/{theirs.pk}/',
                {'reason': 'spam'}, format='json').status_code, 202)


class AutoQuarantineTests(TestCase):
    """Enough independent reports hide content before a human arrives.

    "Flag and wait" is not a timely response at three in the morning, and
    timeliness is what the guideline asks for. The threshold is reversible and
    the author is told, so being wrong costs a delay rather than a deletion.
    """

    def setUp(self):
        self.author = accepted(make_user('author'))
        self.story = Story.objects.create(
            user=self.author, content='contested',
            expires_at=timezone.now() + timedelta(hours=24))

    def _report(self, n):
        for i in range(n):
            reporter = accepted(make_user(f'r{i}'))
            FriendRequest.objects.create(
                sender=reporter, receiver=self.author,
                status=FriendRequest.Status.ACCEPTED)
            client = APIClient()
            client.force_authenticate(reporter)
            client.post(
                f'/api/moderation/report/story/{self.story.pk}/',
                {'reason': 'hate'}, format='json')

    def test_one_report_flags_but_does_not_hide(self):
        self._report(1)
        self.story.refresh_from_db()
        self.assertEqual(self.story.moderation, Moderation.FLAGGED)

    def test_the_threshold_quarantines_and_writes_an_audit_row(self):
        self._report(AUTO_QUARANTINE_REPORTS)
        self.story.refresh_from_db()
        self.assertEqual(self.story.moderation, Moderation.QUARANTINED)
        self.assertTrue(
            ModerationAction.objects.filter(
                kind=ModerationAction.Kind.CONTENT_REMOVED, subject=self.author,
            ).exists()
        )

    def test_a_moderator_removal_is_never_downgraded_by_a_later_report(self):
        """A REMOVED story that gets reported again must not quietly come back
        to FLAGGED, which is a state that is still visible."""
        Story.objects.filter(pk=self.story.pk).update(moderation=Moderation.REMOVED)
        self._report(1)
        self.story.refresh_from_db()
        self.assertEqual(self.story.moderation, Moderation.REMOVED)


# ─── D · Blocking raises a signal ─────────────────────────────────────────────
class BlockSignalTests(TestCase):
    def setUp(self):
        self.blocker = accepted(make_user('blocker'))
        self.target = accepted(make_user('target'))
        self.client = APIClient()
        self.client.force_authenticate(self.blocker)

    def test_blocking_files_a_report_marked_as_coming_from_a_block(self):
        res = self.client.post(f'/api/friends/{self.target.pk}/block/', {}, format='json')
        self.assertEqual(res.status_code, 204)

        report = ContentReport.objects.get()
        self.assertEqual(report.target_user, self.target)
        self.assertEqual(report.source, ContentReport.Source.BLOCK)
        self.assertEqual(report.reason, Reason.OTHER)

    def test_a_block_raised_from_a_content_context_carries_its_reason(self):
        self.client.post(
            f'/api/friends/{self.target.pk}/block/',
            {'reason': 'hate', 'note': 'from a Swift'}, format='json')
        report = ContentReport.objects.get()
        self.assertEqual(report.reason, Reason.HATE)
        self.assertEqual(report.note, 'from a Swift')

    def test_repeated_block_and_unblock_does_not_multiply_reports(self):
        for _ in range(3):
            self.client.post(f'/api/friends/{self.target.pk}/block/', {}, format='json')
            self.client.delete(f'/api/friends/{self.target.pk}/unblock/')
        self.assertEqual(ContentReport.objects.count(), 1)

    def test_blocking_still_blocks(self):
        """The signal is an addition. If it had broken the block itself that
        would be a far worse outcome than the problem it solves."""
        self.client.post(f'/api/friends/{self.target.pk}/block/', {}, format='json')
        self.assertTrue(
            BlockedUser.objects.filter(blocker=self.blocker, blocked=self.target).exists()
        )

    def test_an_invented_reason_falls_back_rather_than_failing_the_block(self):
        res = self.client.post(
            f'/api/friends/{self.target.pk}/block/',
            {'reason': 'nonsense'}, format='json')
        self.assertEqual(res.status_code, 204)
        self.assertEqual(ContentReport.objects.get().reason, Reason.OTHER)


# ─── F · Enforcement ──────────────────────────────────────────────────────────
class EnforcementTests(TestCase):
    def setUp(self):
        self.user = accepted(make_user('offender'))
        self.client = APIClient()

    def suspend(self, days=7):
        self.user.suspended_at = timezone.now()
        self.user.suspended_until = (
            timezone.now() + timedelta(days=days) if days else None)
        self.user.suspension_reason = 'Hate or harassment'
        self.user.save(update_fields=[
            'suspended_at', 'suspended_until', 'suspension_reason'])

    def test_a_live_token_stops_working_the_moment_a_suspension_lands(self):
        """The point of the whole mechanism.

        Django's `is_active` is checked at login and nowhere else, so a ban
        would otherwise leave an already-issued access token working until it
        expired. This asserts that the *same* token stops working.
        """
        res = self.client.post('/api/users/login/', {
            'email': self.user.email, 'password': 'pw-a-real-one-1',
        }, format='json')
        self.assertEqual(res.status_code, 200)
        token = res.data['access']

        authed = APIClient()
        authed.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(authed.get('/api/users/me/').status_code, 200)

        self.suspend()

        after = authed.get('/api/users/me/')
        self.assertEqual(after.status_code, 401)

    def test_login_is_refused_with_the_reason(self):
        self.suspend()
        res = self.client.post('/api/users/login/', {
            'email': self.user.email, 'password': 'pw-a-real-one-1',
        }, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['code'], 'account_suspended')
        self.assertEqual(res.data['reason'], 'Hate or harassment')
        self.assertFalse(res.data['permanent'])

    def test_a_ban_is_permanent_and_says_so(self):
        self.suspend(days=None)
        res = self.client.post('/api/users/login/', {
            'email': self.user.email, 'password': 'pw-a-real-one-1',
        }, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertTrue(res.data['permanent'])
        self.assertIsNone(res.data['until'])

    def test_a_lapsed_suspension_lets_the_account_back_in_with_no_sweep(self):
        self.user.suspended_at = timezone.now() - timedelta(days=8)
        self.user.suspended_until = timezone.now() - timedelta(minutes=1)
        self.user.save(update_fields=['suspended_at', 'suspended_until'])
        res = self.client.post('/api/users/login/', {
            'email': self.user.email, 'password': 'pw-a-real-one-1',
        }, format='json')
        self.assertEqual(res.status_code, 200)

    def test_resolving_a_report_records_who_when_and_what(self):
        reporter = accepted(make_user('rep'))
        moderator = make_user('mod', is_staff=True)
        report = ContentReport.objects.create(
            target_type=ContentReport.Target.USER, target_id=self.user.pk,
            target_user=self.user, reporter=reporter, reason=Reason.SPAM,
        )

        services.resolve_report(
            report, moderator, state=State.REVIEWED, note='not spam')

        report.refresh_from_db()
        self.assertEqual(report.state, State.REVIEWED)
        self.assertEqual(report.reviewed_by, moderator)
        self.assertIsNotNone(report.reviewed_at)
        self.assertEqual(report.moderator_note, 'not spam')
        self.assertTrue(
            ModerationAction.objects.filter(
                kind=ModerationAction.Kind.REPORT_DISMISSED, moderator=moderator,
            ).exists()
        )

    def test_removed_content_is_gone_from_every_public_read(self):
        author = accepted(make_user('vidauthor'))
        viewer = accepted(make_user('viewer'))
        swift = SwiftVideo.objects.create(
            author=author, status=SwiftVideo.Status.READY,
            visibility=SwiftVideo.Visibility.PUBLIC, video_url='https://x/v.mp4',
        )
        client = APIClient()
        client.force_authenticate(viewer)
        self.assertEqual(client.get(f'/api/swifts/{swift.pk}/').status_code, 200)

        SwiftVideo.objects.filter(pk=swift.pk).update(moderation=Moderation.REMOVED)
        self.assertEqual(client.get(f'/api/swifts/{swift.pk}/').status_code, 404)

    def test_quarantined_content_is_hidden_by_exactly_the_same_rule(self):
        author = accepted(make_user('vidauthor2'))
        viewer = accepted(make_user('viewer2'))
        swift = SwiftVideo.objects.create(
            author=author, status=SwiftVideo.Status.READY,
            visibility=SwiftVideo.Visibility.PUBLIC, video_url='https://x/v.mp4',
        )
        SwiftVideo.objects.filter(pk=swift.pk).update(moderation=Moderation.QUARANTINED)
        client = APIClient()
        client.force_authenticate(viewer)
        self.assertEqual(client.get(f'/api/swifts/{swift.pk}/').status_code, 404)


# ─── B · The agreement ────────────────────────────────────────────────────────
class TermsTests(TestCase):
    def setUp(self):
        # Database rollback does not isolate DRF's IP-based cache counters.
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)

    def test_the_documents_are_public(self):
        res = APIClient().get('/api/users/terms/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['version'], terms.CURRENT_VERSION)
        for key in ('terms_url', 'guidelines_url', 'privacy_url'):
            self.assertTrue(res.data[key].startswith('http'), key)

    def test_registering_without_agreement_is_refused(self):
        res = APIClient().post('/api/users/register/', {
            'username': 'norules', 'email': 'norules@example.com',
            'date_of_birth': '1995-05-05',
            'password': 'a-real-password-9', 'password2': 'a-real-password-9',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('accept_terms', res.data)
        self.assertFalse(User.objects.filter(email='norules@example.com').exists())

    def test_agreeing_falsely_is_the_same_as_not_agreeing(self):
        res = APIClient().post('/api/users/register/', {
            'username': 'norules', 'email': 'norules@example.com',
            'date_of_birth': '1995-05-05',
            'password': 'a-real-password-9', 'password2': 'a-real-password-9',
            'accept_terms': False,
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('accept_terms', res.data)

    def test_registering_records_the_version_and_the_time(self):
        res = APIClient().post('/api/users/register/', {
            'username': 'agreed', 'email': 'agreed@example.com',
            'date_of_birth': '1995-05-05',
            'password': 'a-real-password-9', 'password2': 'a-real-password-9',
            'accept_terms': True,
        }, format='json')
        self.assertEqual(res.status_code, 200)
        user = User.objects.get(email='agreed@example.com')
        self.assertEqual(user.terms_version, terms.CURRENT_VERSION)
        self.assertIsNotNone(user.terms_accepted_at)
        self.assertFalse(res.data['user']['needs_terms'])

    def test_an_account_that_predates_the_gate_is_told_it_owes_one(self):
        user = make_user('legacy')
        client = APIClient()
        client.force_authenticate(user)
        self.assertTrue(client.get('/api/users/me/').data['needs_terms'])

    def test_accepting_clears_it(self):
        user = make_user('legacy2')
        client = APIClient()
        client.force_authenticate(user)

        res = client.post('/api/users/terms/accept/', {'accept': True}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['needs_terms'])
        self.assertFalse(client.get('/api/users/me/').data['needs_terms'])

    def test_accepting_without_agreeing_is_refused(self):
        user = make_user('legacy3')
        client = APIClient()
        client.force_authenticate(user)
        res = client.post('/api/users/terms/accept/', {'accept': False}, format='json')
        self.assertEqual(res.status_code, 400)
        user.refresh_from_db()
        self.assertIsNone(user.terms_accepted_at)

    def test_a_new_version_puts_an_agreed_account_back_in_front_of_the_gate(self):
        """The whole reason acceptance is versioned rather than a boolean:
        consent to the old text is not consent to the new one."""
        user = accepted(make_user('current'))
        client = APIClient()
        client.force_authenticate(user)
        self.assertFalse(client.get('/api/users/me/').data['needs_terms'])

        with self.settings():
            original = terms.CURRENT_VERSION
            terms.CURRENT_VERSION = '2099-01-01'
            try:
                self.assertTrue(client.get('/api/users/me/').data['needs_terms'])
            finally:
                terms.CURRENT_VERSION = original

    # ── Login, which is the path App Review quoted ────────────────────────────
    #
    # "The EULA or terms of use agreement presented to users before registering
    # or logging in." Registration is covered above; these cover the other half.
    # The rule is enforced on the server as well as in the app, because a gate
    # that only exists in the client is a gate that a modified client does not
    # have.

    def _login(self, user, build=CURRENT_BUILD, **extra):
        """Sign in as `user`, declaring a client build.

        `build` defaults to the current one because that is what these tests
        are about; `build=None` sends no header at all and is how the legacy
        path is exercised. It is a distinct case from any number - see
        `users.clientinfo.client_build`.
        """
        headers = {} if build is None else {'HTTP_X_APPROX_BUILD': str(build)}
        return APIClient().post('/api/users/login/', {
            'email': user.email, 'password': 'pw-a-real-one-1', **extra,
        }, format='json', **headers)

    def test_an_account_that_has_not_agreed_cannot_log_in(self):
        user = make_user('needsterms')

        res = self._login(user)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'terms_required')
        self.assertEqual(res.data['terms_version'], terms.CURRENT_VERSION)

    def test_no_token_is_issued_when_the_agreement_is_missing(self):
        """The tokens must not merely be withheld from the body - they must
        never be minted.

        `SuspensionAwareTokenObtainPairSerializer` gates inside `get_token`
        rather than inside `validate` for exactly this reason: simplejwt's
        `validate` authenticates and *then* mints, so a check placed there
        would leave an `OutstandingToken` row for a pair nobody received.
        """
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        user = make_user('notoken')
        before = OutstandingToken.objects.filter(user=user).count()

        res = self._login(user)

        self.assertNotIn('access', res.data)
        self.assertNotIn('refresh', res.data)
        self.assertEqual(OutstandingToken.objects.filter(user=user).count(), before)

    def test_sending_accept_terms_false_is_the_same_as_not_sending_it(self):
        user = make_user('explicitno')

        res = self._login(user, accept_terms=False)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'terms_required')

    def test_agreeing_at_the_login_screen_signs_the_user_in(self):
        user = make_user('agreesnow')

        res = self._login(user, accept_terms=True)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['access'])
        self.assertTrue(res.data['refresh'])

    def test_agreeing_at_the_login_screen_records_version_and_time(self):
        user = make_user('recorded')
        self.assertIsNone(user.terms_accepted_at)

        self._login(user, accept_terms=True)

        user.refresh_from_db()
        self.assertEqual(user.terms_version, terms.CURRENT_VERSION)
        self.assertIsNotNone(user.terms_accepted_at)

    def test_an_account_that_already_agreed_logs_in_without_sending_anything(self):
        """The common case, and the one that must not become friction: somebody
        who agreed last week signs in as before. The app still sends the flag,
        but the server does not require it from them."""
        user = accepted(make_user('alreadyagreed'))

        res = self._login(user)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['access'])

    def test_an_existing_acceptance_is_not_re_stamped_on_every_login(self):
        """Re-recording would move the timestamp on every sign-in and destroy
        the only evidence of *when* they actually agreed."""
        user = accepted(make_user('stable'))
        first = User.objects.get(pk=user.pk).terms_accepted_at

        self._login(user, accept_terms=True)

        user.refresh_from_db()
        self.assertEqual(user.terms_accepted_at, first)

    def test_a_new_version_puts_an_existing_account_back_behind_the_gate(self):
        user = accepted(make_user('staleversion'))
        original = terms.CURRENT_VERSION
        terms.CURRENT_VERSION = '2099-01-01'
        try:
            refused = self._login(user)
            self.assertEqual(refused.status_code, 400)
            self.assertEqual(refused.data['code'], 'terms_required')

            allowed = self._login(user, accept_terms=True)
            self.assertEqual(allowed.status_code, 200)

            user.refresh_from_db()
            self.assertEqual(user.terms_version, '2099-01-01')
        finally:
            terms.CURRENT_VERSION = original

    def test_bad_credentials_still_read_as_bad_credentials(self):
        """The terms check runs after authentication and must not shadow it.

        Answering "terms required" to a wrong password would tell an attacker
        that the address has an account - the account-existence oracle the
        ordering exists to avoid.
        """
        accepted(make_user('rightpassword'))

        res = APIClient().post('/api/users/login/', {
            'email': 'rightpassword@example.com', 'password': 'wrong-password',
        }, format='json')

        self.assertEqual(res.status_code, 401)

    def test_a_suspended_account_is_still_refused_before_the_terms_check(self):
        """Both gates live in `get_token`, and suspension is asked first: an
        account that is banned *and* owes an agreement should be told it is
        banned, not sent to read the Terms."""
        user = accepted(make_user('bannedtoo'))
        user.suspended_at = timezone.now()
        user.suspension_reason = 'Hate or harassment'
        user.save(update_fields=['suspended_at', 'suspension_reason'])

        res = self._login(user)

        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data['code'], 'account_suspended')

    # -- The compatibility window ---------------------------------------------
    #
    # The backend has to be live *during* App Review, which means it deploys
    # while build 31 is still the version everybody is running. Build 31 has
    # never heard of `accept_terms`, so enforcing against it would lock out
    # every existing user. These pin the three-way behaviour of
    # `users.terms.terms_enforced_for` - and the one that matters most is the
    # bypass test, because "build 32 omits the field" must not look like
    # "legacy client".

    def test_a_legacy_client_that_sends_no_build_header_can_still_sign_in(self):
        """Build 31, mid-review, with no way to show an agreement."""
        user = make_user('legacyclient')

        res = self._login(user, build=None)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['access'])

    def test_a_legacy_login_does_not_fabricate_an_acceptance(self):
        """Letting them in is not the same as recording that they agreed. The
        account still owes an agreement and meets `TermsGate` after updating."""
        user = make_user('legacynostamp')

        self._login(user, build=None)

        user.refresh_from_db()
        self.assertEqual(user.terms_version, '')
        self.assertIsNone(user.terms_accepted_at)

    def test_a_build_below_the_minimum_is_treated_as_legacy(self):
        user = make_user('build31')

        res = self._login(user, build=31)

        self.assertEqual(res.status_code, 200)

    def test_the_minimum_build_is_enforced(self):
        user = make_user('build32')

        res = self._login(user, build=32)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'terms_required')

    def test_a_future_build_is_enforced_too(self):
        """The rule is `>=`, not `==`. Build 40 must not fall out of the gate
        by being newer than anything this code knew about."""
        user = make_user('build40')

        res = self._login(user, build=40)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'terms_required')

    def test_build_32_cannot_bypass_the_gate_by_omitting_accept_terms(self):
        """The load-bearing test.

        The server decides whether to enforce from the *build header*, never
        from whether `accept_terms` is present. If it inferred "no
        accept_terms means an old client", every build 32 could opt out of the
        agreement by leaving the field off.
        """
        user = make_user('nobypass')

        omitted = self._login(user, build=32)
        explicit_false = self._login(user, build=32, accept_terms=False)

        self.assertEqual(omitted.status_code, 400)
        self.assertEqual(explicit_false.status_code, 400)
        self.assertEqual(omitted.data['code'], 'terms_required')
        self.assertEqual(explicit_false.data['code'], 'terms_required')
        user.refresh_from_db()
        self.assertIsNone(user.terms_accepted_at)

    def test_build_32_with_agreement_records_and_signs_in(self):
        user = make_user('build32agrees')

        res = self._login(user, build=32, accept_terms=True)

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['access'])
        user.refresh_from_db()
        self.assertEqual(user.terms_version, terms.CURRENT_VERSION)
        self.assertIsNotNone(user.terms_accepted_at)

    def test_build_32_user_who_already_agreed_is_not_re_stamped(self):
        user = accepted(make_user('build32already'))
        first = User.objects.get(pk=user.pk).terms_accepted_at

        res = self._login(user, build=32)

        self.assertEqual(res.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.terms_accepted_at, first)

    def test_no_token_is_minted_for_a_rejected_build_32_login(self):
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

        user = make_user('build32notoken')
        before = OutstandingToken.objects.filter(user=user).count()

        res = self._login(user, build=32)

        self.assertNotIn('access', res.data)
        self.assertNotIn('refresh', res.data)
        self.assertEqual(OutstandingToken.objects.filter(user=user).count(), before)

    def test_bad_credentials_are_401_whatever_the_build_says(self):
        """The build header must not turn the login endpoint into an
        account-existence oracle: a wrong password answers the same 401 whether
        or not the account owes an agreement."""
        accepted(make_user('rightpw32'))

        for build in (None, 31, 32):
            with self.subTest(build=build):
                headers = {} if build is None else {'HTTP_X_APPROX_BUILD': str(build)}
                res = APIClient().post('/api/users/login/', {
                    'email': 'rightpw32@example.com', 'password': 'wrong-password',
                }, format='json', **headers)
                self.assertEqual(res.status_code, 401)

    def test_a_mangled_build_header_degrades_to_legacy_rather_than_500(self):
        """A proxy that rewrites the header should not take login down."""
        user = make_user('mangled')

        res = self._login(user, build='not-a-number')

        self.assertEqual(res.status_code, 200)

    def test_closing_the_window_enforces_headerless_clients_too(self):
        """The end state. Once build 31 is off the store,
        `TERMS_ENFORCEMENT_ALLOW_LEGACY=False` leaves no unenforced path."""
        user = make_user('windowclosed')

        with self.settings(TERMS_ENFORCEMENT_ALLOW_LEGACY=False):
            res = self._login(user, build=None)

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'terms_required')

    def test_the_minimum_build_is_configuration_not_a_hard_coded_policy(self):
        user = make_user('configurable')

        with self.settings(TERMS_ENFORCEMENT_MIN_BUILD=99):
            allowed = self._login(user, build=32)
        self.assertEqual(allowed.status_code, 200)

    def test_a_client_cannot_claim_acceptance_through_a_profile_patch(self):
        user = make_user('sneaky')
        client = APIClient()
        client.force_authenticate(user)
        client.patch('/api/users/profile/', {
            'terms_version': terms.CURRENT_VERSION,
        }, format='json')
        user.refresh_from_db()
        self.assertEqual(user.terms_version, '')


# ─── Comments ─────────────────────────────────────────────────────────────────
class CommentModerationTests(TestCase):
    def setUp(self):
        self.author = accepted(make_user('creator'))
        self.commenter = accepted(make_user('commenter'))
        self.swift = SwiftVideo.objects.create(
            author=self.author, status=SwiftVideo.Status.READY,
            visibility=SwiftVideo.Visibility.PUBLIC, video_url='https://x/v.mp4',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.commenter)

    def test_a_blocked_comment_is_refused(self):
        res = self.client.post(
            f'/api/swifts/{self.swift.pk}/comments/',
            {'text': 'i will kill you'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(SwiftComment.objects.exists())

    def test_a_flagged_comment_is_quarantined_and_does_not_move_the_counter(self):
        res = self.client.post(
            f'/api/swifts/{self.swift.pk}/comments/',
            {'text': 'kill yourself'}, format='json')
        self.assertEqual(res.status_code, 201)
        comment = SwiftComment.objects.get()
        self.assertEqual(comment.moderation, Moderation.QUARANTINED)
        self.swift.refresh_from_db()
        self.assertEqual(self.swift.comment_count, 0)

    def test_a_quarantined_comment_is_not_in_anybody_elses_thread(self):
        self.client.post(
            f'/api/swifts/{self.swift.pk}/comments/',
            {'text': 'kill yourself'}, format='json')

        reader = APIClient()
        reader.force_authenticate(self.author)
        res = reader.get(f'/api/swifts/{self.swift.pk}/comments/')
        self.assertEqual(res.data['count'], 0)

    def test_a_comment_can_be_reported(self):
        comment = SwiftComment.objects.create(
            swift=self.swift, author=self.author, text='something objectionable')
        res = self.client.post(
            f'/api/moderation/report/comment/{comment.pk}/',
            {'reason': 'hate'}, format='json')
        self.assertEqual(res.status_code, 202)
        self.assertEqual(ContentReport.objects.get().target_user, self.author)

# ─── The socket, which is the path messages actually take ─────────────────────
class WebSocketFilterTests(TransactionTestCase):
    """Screening on the WebSocket consumers, not only the REST views.

    `TransactionTestCase`, not `TestCase`: the consumers reach the database
    through `database_sync_to_async`, which runs on a worker thread, and
    `TestCase` holds the whole test inside one atomic block on a connection
    that thread cannot share. On SQLite that surfaces as
    "database table is locked" rather than anything about threads.

    This is the test that would have caught the hole. The app sends chat over a
    socket and falls back to HTTP only when it is not connected, so a filter
    wired into the REST view alone would have screened almost nothing while
    looking, from the outside, entirely present.

    Driven through `ChannelsLiveServerTestCase`-free plumbing: the consumers'
    `receive` is an ordinary coroutine, so it is exercised directly rather than
    over a real socket. What is being asserted is that the screening step is on
    the path at all.
    """

    def setUp(self):
        self.sender = accepted(make_user('wssender'))
        self.receiver = accepted(make_user('wsreceiver'))
        HiRequest.objects.create(
            sender=self.sender, receiver=self.receiver,
            status=HiRequest.Status.ACCEPTED)

    def _dm_consumer(self):
        from friends.consumers import ChatConsumer

        consumer = ChatConsumer()
        consumer.user = self.sender
        consumer.other = self.receiver
        consumer.room_group = 'test'
        consumer.scope = {'user': self.sender}
        return consumer

    def test_a_blocked_direct_message_never_becomes_a_row(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock, patch

        consumer = self._dm_consumer()
        with patch.object(consumer, 'send', new=AsyncMock()) as send:
            asyncio.run(consumer.receive(json.dumps({'content': 'i will kill you'})))

        self.assertFalse(Message.objects.exists())
        # And the sender is told why, rather than watching a bubble hang.
        payload = json.loads(send.await_args[0][0])
        self.assertEqual(payload['type'], 'error')
        self.assertIn('Community Guidelines', payload['detail'])

    def test_an_ordinary_direct_message_still_goes_through(self):
        import asyncio
        import json
        from unittest.mock import AsyncMock, patch

        consumer = self._dm_consumer()
        with patch.object(consumer, 'send', new=AsyncMock()),                 patch.object(type(consumer), 'channel_layer', create=True) as layer:
            layer.group_send = AsyncMock()
            asyncio.run(consumer.receive(json.dumps({'content': 'coffee at 3?'})))

        self.assertEqual(Message.objects.count(), 1)
        self.assertEqual(Message.objects.get().content, 'coffee at 3?')
