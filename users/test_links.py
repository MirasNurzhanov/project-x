"""Profile link validation, normalization and visibility.

The validation tests are deliberately adversarial: the input is attacker-
controlled and the output gets rendered on other people's screens and handed
to `Linking.openURL`, so "rejected" has to mean rejected, not "sanitized on
the way out".
"""
from django.test import TestCase
from rest_framework.test import APIClient

from friends.models import BlockedUser, FriendRequest, HiRequest
from users.links import LinkError, normalize_link
from users.models import ProfileLink, User


def make_user(name, **extra):
    return User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123', **extra
    )


class LinkValidationTests(TestCase):
    def assertRejected(self, value, hint=None):
        with self.assertRaises(LinkError, msg=f'{value!r} was accepted'):
            normalize_link(value, platform_hint=hint)

    # ── Schemes ─────────────────────────────────────────────────────────────
    def test_dangerous_schemes_rejected(self):
        for value in (
            'javascript:alert(1)',
            'JavaScript:alert(1)',
            'data:text/html;base64,PHNjcmlwdD4=',
            'file:///etc/passwd',
            'ftp://example.com/x',
            'intent://scan#Intent;scheme=zxing;end',
            'vbscript:msgbox(1)',
        ):
            self.assertRejected(value)

    def test_http_is_upgraded_to_https(self):
        url, _, _, _ = normalize_link('http://example.com/me')
        self.assertTrue(url.startswith('https://'))

    def test_bare_domain_gets_https(self):
        url, _, _, _ = normalize_link('example.com/me')
        self.assertEqual(url, 'https://example.com/me')

    # ── Internal targets ────────────────────────────────────────────────────
    def test_internal_hosts_rejected(self):
        for value in (
            'http://localhost/admin',
            'https://localhost:8000',
            'https://printer.local',
            'https://db.internal',
            'https://wiki.corp',
            'https://thing.home.arpa',
            'https://intranet',
            'https://metadata.google.internal/computeMetadata/v1/',
        ):
            self.assertRejected(value)

    def test_ip_literals_rejected(self):
        for value in (
            'https://127.0.0.1/',
            'https://10.0.0.5/',
            'https://192.168.1.1/',
            'https://172.16.0.1/',
            'https://169.254.169.254/latest/meta-data/',
            'https://[::1]/',
            'https://[fd00::1]/',
            'https://8.8.8.8/',  # public, but still never a real profile link
        ):
            self.assertRejected(value)

    def test_custom_ports_rejected(self):
        self.assertRejected('https://example.com:8080/x')

    # ── Display-spoofing ────────────────────────────────────────────────────
    def test_userinfo_prefix_cannot_disguise_the_host(self):
        """`https://instagram.com@evil.com/x` reads as Instagram but resolves
        to evil.com. The stored URL must be rebuilt from the real hostname."""
        url, platform, _, label = normalize_link('https://instagram.com@evil.com/x')
        self.assertNotIn('instagram.com', url)
        self.assertTrue(url.startswith('https://evil.com/'))
        self.assertEqual(platform, 'website')
        self.assertEqual(label, 'evil.com')

    def test_control_characters_rejected(self):
        self.assertRejected('https://example.com/\u202Ex')
        self.assertRejected('https://exam\nple.com/')

    def test_overlong_url_rejected(self):
        self.assertRejected('https://example.com/' + 'a' * 400)

    # ── Normalization ───────────────────────────────────────────────────────
    def test_tracking_params_stripped(self):
        url, _, _, _ = normalize_link(
            'https://open.spotify.com/user/abc?si=deadbeef&utm_source=copy&foo=keep'
        )
        self.assertNotIn('si=', url)
        self.assertNotIn('utm_source', url)
        self.assertIn('foo=keep', url)

    def test_meaningful_params_kept(self):
        """Stripping is a deny-list — losing YouTube's `v` would break the link."""
        url, _, _, _ = normalize_link('https://youtube.com/watch?v=abc123&feature=share')
        self.assertIn('v=abc123', url)
        self.assertNotIn('feature=', url)

    def test_fragment_and_trailing_slash_dropped(self):
        url, _, _, _ = normalize_link('https://example.com/#section')
        self.assertEqual(url, 'https://example.com')

    # ── Platform recognition ────────────────────────────────────────────────
    def test_platforms_recognised_with_handles(self):
        cases = [
            ('https://instagram.com/someone', 'instagram', '@someone'),
            ('https://www.instagram.com/someone/', 'instagram', '@someone'),
            ('https://x.com/someone', 'x', '@someone'),
            ('https://twitter.com/someone', 'x', '@someone'),
            ('https://github.com/someone', 'github', '@someone'),
            ('https://t.me/someone', 'telegram', '@someone'),
            ('https://tiktok.com/@someone', 'tiktok', '@someone'),
            ('https://youtube.com/@someone', 'youtube', '@someone'),
        ]
        for value, platform, label in cases:
            url, got_platform, _, got_label = normalize_link(value)
            self.assertEqual(got_platform, platform, value)
            self.assertEqual(got_label, label, value)

    def test_content_urls_do_not_produce_a_fake_handle(self):
        _, platform, handle, label = normalize_link('https://instagram.com/p/Cxyz123/')
        self.assertEqual(platform, 'instagram')
        self.assertEqual(handle, '')
        self.assertEqual(label, 'Instagram')

    def test_unknown_https_url_allowed_with_generic_label(self):
        url, platform, _, label = normalize_link('https://some-blog.example/about')
        self.assertEqual(platform, 'website')
        self.assertEqual(label, 'some-blog.example')
        self.assertEqual(url, 'https://some-blog.example/about')

    # ── Bare handles ────────────────────────────────────────────────────────
    def test_bare_handle_expands_with_a_platform_hint(self):
        url, platform, _, label = normalize_link('@someone', platform_hint='instagram')
        self.assertEqual(url, 'https://instagram.com/someone')
        self.assertEqual(platform, 'instagram')
        self.assertEqual(label, '@someone')

    def test_bare_handle_without_hint_rejected(self):
        self.assertRejected('@someone')


class LinkApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.me = make_user('me')
        self.client.force_authenticate(self.me)

    def add(self, url, platform=None):
        body = {'url': url}
        if platform:
            body['platform'] = platform
        return self.client.post('/api/users/links/', body, format='json')

    def test_add_and_list(self):
        self.assertEqual(self.add('https://github.com/me').status_code, 201)
        body = self.client.get('/api/users/links/').json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]['label'], '@me')
        self.assertEqual(body[0]['platform'], 'github')

    def test_bad_link_rejected_by_api(self):
        response = self.add('javascript:alert(1)')
        self.assertEqual(response.status_code, 400)
        self.assertIn('url', response.json())

    def test_cap_enforced(self):
        for i in range(5):
            self.assertEqual(self.add(f'https://example{i}.com').status_code, 201)
        self.assertEqual(self.add('https://sixth.com').status_code, 400)
        self.assertEqual(self.me.links.count(), 5)

    def test_duplicate_rejected(self):
        self.add('https://github.com/me')
        self.assertEqual(self.add('https://github.com/me/').status_code, 400)

    def test_reorder(self):
        ids = [self.add(f'https://example{i}.com').json()['id'] for i in range(3)]
        reordered = [ids[2], ids[0], ids[1]]
        body = self.client.patch(
            '/api/users/links/', {'order': reordered}, format='json'
        ).json()
        self.assertEqual([row['id'] for row in body], reordered)

    def test_reorder_ignores_links_owned_by_someone_else(self):
        other = make_user('other')
        theirs = ProfileLink.objects.create(
            user=other, url='https://x.com/other', platform='x',
            handle='other', label='@other',
        )
        mine = self.add('https://github.com/me').json()['id']
        body = self.client.patch(
            '/api/users/links/', {'order': [theirs.pk, mine]}, format='json'
        ).json()
        self.assertEqual([row['id'] for row in body], [mine])

    def test_delete(self):
        link_id = self.add('https://github.com/me').json()['id']
        self.assertEqual(
            self.client.delete(f'/api/users/links/{link_id}/').status_code, 204
        )
        self.assertEqual(self.me.links.count(), 0)

    def test_cannot_delete_someone_elses_link(self):
        other = make_user('other')
        theirs = ProfileLink.objects.create(
            user=other, url='https://x.com/other', platform='x',
            handle='other', label='@other',
        )
        self.assertEqual(self.client.delete(f'/api/users/links/{theirs.pk}/').status_code, 404)
        self.assertTrue(ProfileLink.objects.filter(pk=theirs.pk).exists())


class LinkVisibilityTests(TestCase):
    """Links default to friends-only, and hidden means absent from the payload."""

    def setUp(self):
        self.client = APIClient()
        self.viewer = make_user('viewer')
        self.target = make_user('target')
        ProfileLink.objects.create(
            user=self.target, url='https://instagram.com/target',
            platform='instagram', handle='target', label='@target',
        )
        self.client.force_authenticate(self.viewer)

    def profile(self):
        return self.client.get(f'/api/users/profile/{self.target.pk}/').json()

    def test_default_is_friends_only(self):
        self.assertEqual(self.target.visibility_links, User.Audience.FRIENDS)
        self.assertNotIn('links', self.profile())

    def test_connection_still_cannot_see_them(self):
        HiRequest.objects.create(
            sender=self.viewer, receiver=self.target, status=HiRequest.Status.ACCEPTED
        )
        self.assertNotIn('links', self.profile())

    def test_friend_can_see_them(self):
        FriendRequest.objects.create(
            sender=self.viewer, receiver=self.target,
            status=FriendRequest.Status.ACCEPTED,
        )
        body = self.profile()
        self.assertEqual([row['label'] for row in body['links']], ['@target'])

    def test_everyone_setting_exposes_them(self):
        self.target.visibility_links = User.Audience.EVERYONE
        self.target.save(update_fields=['visibility_links'])
        self.assertIn('links', self.profile())

    def test_nobody_setting_hides_them_from_friends_too(self):
        FriendRequest.objects.create(
            sender=self.viewer, receiver=self.target,
            status=FriendRequest.Status.ACCEPTED,
        )
        self.target.visibility_links = User.Audience.NOBODY
        self.target.save(update_fields=['visibility_links'])
        self.assertNotIn('links', self.profile())

    def test_private_account_hides_links_from_non_approved(self):
        self.target.is_private = True
        self.target.visibility_links = User.Audience.EVERYONE
        self.target.save(update_fields=['is_private', 'visibility_links'])
        self.assertNotIn('links', self.profile())

    def test_blocked_user_gets_no_profile_at_all(self):
        BlockedUser.objects.create(blocker=self.target, blocked=self.viewer)
        self.assertEqual(
            self.client.get(f'/api/users/profile/{self.target.pk}/').status_code, 404
        )

    def test_own_links_always_visible_to_self(self):
        self.client.force_authenticate(self.target)
        body = self.client.get('/api/users/profile/').json()
        self.assertEqual(len(body['links']), 1)
