"""Google/Apple sign-in: token verification and account resolution.

The verification tests mint their own RSA key and point the provider's JWK
client at it, so a real signature is really checked without going near the
network. That matters more than it sounds: every "is this token good" branch
in socialauth.py is the only thing standing between a POST body and an
account, and a test that stubs out `jwt.decode` would prove none of them.
"""
import time
from datetime import date, timedelta
from unittest import mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from users import socialauth
from users.models import SocialAccount, User

GOOGLE_AUD = 'client-web.apps.googleusercontent.com'
APPLE_AUD = 'com.arcanix.aura'

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = 'test-key-1'


def make_token(*, iss='https://accounts.google.com', aud=GOOGLE_AUD, sub='sub-1',
               email='someone@example.com', email_verified=True, exp_delta=600,
               key=_KEY, kid=_KID, **extra):
    now = int(time.time())
    claims = {'iss': iss, 'aud': aud, 'sub': sub, 'iat': now, 'exp': now + exp_delta}
    if email is not None:
        claims['email'] = email
    if email_verified is not None:
        claims['email_verified'] = email_verified
    claims.update(extra)
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': kid})


class _StubJWKClient:
    """Answers with the test key, whatever `kid` is asked for."""

    def __init__(self, key=None):
        self._key = (key or _KEY).public_key()

    def get_signing_key_from_jwt(self, token):
        return mock.Mock(key=self._key)


def stub_keys(key=None):
    """Patch both providers' key clients for the duration of a `with`."""
    return mock.patch.multiple(
        socialauth.PROVIDERS['google'], _jwk_client=_StubJWKClient(key)
    ), mock.patch.multiple(
        socialauth.PROVIDERS['apple'], _jwk_client=_StubJWKClient(key)
    )


class SocialAuthTestCase(TestCase):
    """Shared scaffolding: configured client ids and a stubbed key endpoint."""

    def setUp(self):
        super().setUp()
        for patcher in stub_keys():
            patcher.start()
            self.addCleanup(patcher.stop)
        overrides = self.settings(
            GOOGLE_OAUTH_CLIENT_IDS=(GOOGLE_AUD,),
            APPLE_SIGN_IN_CLIENT_IDS=(APPLE_AUD,),
        )
        overrides.enable()
        self.addCleanup(overrides.disable)


class TokenVerificationTests(SocialAuthTestCase):
    def assertRejected(self, token, code=None, provider='google'):
        with self.assertRaises(socialauth.SocialAuthError) as ctx:
            socialauth.verify_identity_token(provider, token)
        if code:
            self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_valid_google_token_accepted(self):
        claims = socialauth.verify_identity_token('google', make_token())
        self.assertEqual(claims['sub'], 'sub-1')

    def test_bare_google_issuer_accepted(self):
        # Google publishes under both spellings; rejecting one breaks a subset
        # of real accounts and nothing else, which is the worst kind of bug.
        claims = socialauth.verify_identity_token(
            'google', make_token(iss='accounts.google.com'))
        self.assertEqual(claims['sub'], 'sub-1')

    def test_wrong_audience_rejected(self):
        self.assertRejected(make_token(aud='someone-elses-app'), 'audience_mismatch')

    def test_expired_token_rejected(self):
        self.assertRejected(make_token(exp_delta=-3600), 'token_expired')

    def test_wrong_issuer_rejected(self):
        self.assertRejected(make_token(iss='https://evil.example.com'))

    def test_token_signed_by_another_key_rejected(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.assertRejected(make_token(key=other))

    def test_unsigned_token_rejected(self):
        # The alg=none classic. PyJWT is told RS256 only, so this must not pass
        # however plausible the claims look.
        forged = jwt.encode(
            {'iss': 'https://accounts.google.com', 'aud': GOOGLE_AUD, 'sub': 'x',
             'iat': int(time.time()), 'exp': int(time.time()) + 600},
            key='', algorithm='none',
        )
        self.assertRejected(forged)

    def test_garbage_rejected(self):
        self.assertRejected('not-a-jwt')
        self.assertRejected('')

    def test_apple_token_accepted(self):
        claims = socialauth.verify_identity_token(
            'apple',
            make_token(iss='https://appleid.apple.com', aud=APPLE_AUD, sub='apple-1'),
            )
        self.assertEqual(claims['sub'], 'apple-1')

    def test_unknown_provider_rejected(self):
        self.assertRejected(make_token(), 'unknown_provider', provider='facebook')

    def test_unconfigured_provider_reports_deployment_error(self):
        with self.settings(GOOGLE_OAUTH_CLIENT_IDS=()):
            exc = self.assertRejected(make_token(), 'not_configured')
        self.assertEqual(exc.status, 503)


class ResolveUserTests(SocialAuthTestCase):
    def resolve(self, provider='google', **kwargs):
        full_name = kwargs.pop('full_name', '')
        claims = socialauth.verify_identity_token(provider, make_token(**kwargs))
        return socialauth.resolve_user(provider, claims, full_name)

    def test_creates_account_on_first_sign_in(self):
        user, created = self.resolve(email='newperson@example.com')
        self.assertTrue(created)
        self.assertEqual(user.email, 'newperson@example.com')
        self.assertFalse(user.has_usable_password())
        self.assertTrue(
            SocialAccount.objects.filter(user=user, provider='google').exists())

    def test_same_subject_returns_same_account(self):
        first, _ = self.resolve(sub='stable', email='p@example.com')
        second, created = self.resolve(sub='stable', email='p@example.com')
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(created)
        self.assertEqual(User.objects.count(), 1)

    def test_changed_email_still_finds_the_account(self):
        first, _ = self.resolve(sub='stable', email='old@example.com')
        second, created = self.resolve(sub='stable', email='brand-new@example.com')
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(created)
        # The profile keeps the address the person chose; a provider that still
        # remembers the old one does not get to undo that.
        second.refresh_from_db()
        self.assertEqual(second.email, 'old@example.com')

    def test_links_to_existing_password_account_by_verified_email(self):
        existing = User.objects.create_user(
            username='already', email='already@example.com', password='pw-123456')
        user, created = self.resolve(email='already@example.com')
        self.assertEqual(user.pk, existing.pk)
        self.assertFalse(created)
        # And the password still works — linking must not lock anyone out.
        user.refresh_from_db()
        self.assertTrue(user.check_password('pw-123456'))

    def test_email_match_is_case_insensitive(self):
        existing = User.objects.create_user(
            username='mixed', email='Mixed@Example.com', password='pw-123456')
        user, _ = self.resolve(email='mixed@example.com')
        self.assertEqual(user.pk, existing.pk)

    def test_unverified_email_never_takes_over_an_account(self):
        User.objects.create_user(
            username='victim', email='victim@example.com', password='pw-123456')
        with self.assertRaises(socialauth.SocialAuthError) as ctx:
            self.resolve(email='victim@example.com', email_verified=False)
        self.assertEqual(ctx.exception.code, 'email_required')
        self.assertFalse(SocialAccount.objects.exists())

    def test_second_google_identity_on_one_account_is_refused(self):
        existing = User.objects.create_user(
            username='linked', email='linked@example.com', password='pw-123456')
        SocialAccount.objects.create(
            provider='google', subject='original', user=existing,
            email='linked@example.com')
        with self.assertRaises(socialauth.SocialAuthError) as ctx:
            self.resolve(sub='impostor', email='linked@example.com')
        self.assertEqual(ctx.exception.code, 'already_linked')
        self.assertEqual(existing.social_accounts.count(), 1)

    def test_username_collision_gets_a_suffix(self):
        User.objects.create_user(
            username='taken', email='other@example.com', password='pw-123456')
        user, _ = self.resolve(email='taken@example.com')
        self.assertEqual(user.username, 'taken1')

    def test_username_falls_back_when_nothing_usable(self):
        user, _ = self.resolve(email='...@example.com')
        self.assertEqual(user.username, 'aura')

    def test_full_name_seeds_the_username(self):
        user, _ = self.resolve(email='x7f3@privaterelay.appleid.com',
                               full_name='Renée O\'Hara')
        self.assertEqual(user.username, 'reneeohara')

    def test_google_and_apple_can_share_one_account(self):
        first, _ = self.resolve(email='both@example.com')
        second, created = self.resolve(
            provider='apple', iss='https://appleid.apple.com', aud=APPLE_AUD,
            sub='apple-sub', email='both@example.com')
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(created)
        self.assertEqual(second.social_accounts.count(), 2)

    def test_apple_string_email_verified_is_honoured(self):
        # Apple sends "true", not true. Read as a bool it is truthy either way;
        # read as the literal False check in _email_from_claims it must not
        # fall through to "no address".
        user, created = self.resolve(
            provider='apple', iss='https://appleid.apple.com', aud=APPLE_AUD,
            sub='apple-str', email='apple@example.com', email_verified='true')
        self.assertTrue(created)
        self.assertEqual(user.email, 'apple@example.com')


class SocialLoginEndpointTests(SocialAuthTestCase):
    def post(self, **body):
        return APIClient().post('/api/users/social/', body, format='json')

    def test_returns_tokens_and_flags_a_new_account(self):
        res = self.post(provider='google', id_token=make_token(email='fresh@example.com'))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['created'])
        self.assertTrue(res.data['needs_date_of_birth'])
        self.assertTrue(res.data['tokens']['access'])
        self.assertTrue(res.data['tokens']['refresh'])

    def test_returning_account_with_a_birthday_needs_nothing(self):
        user = User.objects.create_user(
            username='old', email='old@example.com', password='pw-123456',
            date_of_birth=date(1995, 5, 5))
        SocialAccount.objects.create(provider='google', subject='sub-1', user=user)
        res = self.post(provider='google', id_token=make_token())
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['created'])
        self.assertFalse(res.data['needs_date_of_birth'])

    def test_bad_token_is_a_400_with_a_code(self):
        res = self.post(provider='google', id_token='nonsense')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'invalid_token')

    def test_unconfigured_provider_is_a_503(self):
        with self.settings(APPLE_SIGN_IN_CLIENT_IDS=()):
            res = self.post(provider='apple', id_token=make_token())
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data['code'], 'not_configured')

    def test_missing_token_does_not_500(self):
        res = self.post(provider='google')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'missing_token')


class LinkIdentityTests(SocialAuthTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='linker', email='linker@example.com', password='pw-123456')

    def link(self, provider='google', **kwargs):
        claims = socialauth.verify_identity_token(provider, make_token(**kwargs))
        return socialauth.link_identity(self.user, provider, claims)

    def test_attaches_a_fresh_identity(self):
        self.link(sub='fresh-sub')
        self.assertTrue(
            SocialAccount.objects.filter(
                user=self.user, provider='google', subject='fresh-sub').exists())

    def test_never_touches_email_unlike_resolve_user(self):
        # A different address on the token must not overwrite the account's —
        # this is not the email-matching path, it is already `self.user`.
        self.link(sub='fresh-sub', email='someone-elses-address@example.com')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'linker@example.com')

    def test_relinking_the_same_identity_is_a_no_op_success(self):
        self.link(sub='stable-sub')
        self.link(sub='stable-sub')  # second tap of "Connect", or a second device
        self.assertEqual(
            SocialAccount.objects.filter(
                user=self.user, provider='google', subject='stable-sub').count(),
            1,
        )

    def test_refuses_an_identity_already_linked_elsewhere(self):
        other = User.objects.create_user(
            username='other', email='other@example.com', password='pw-123456')
        SocialAccount.objects.create(
            provider='google', subject='taken-sub', user=other)
        with self.assertRaises(socialauth.SocialAuthError) as ctx:
            self.link(sub='taken-sub')
        self.assertEqual(ctx.exception.code, 'identity_already_linked')
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(other.social_accounts.count(), 1)
        self.assertEqual(self.user.social_accounts.count(), 0)

    def test_refuses_a_second_identity_of_the_same_provider(self):
        self.link(sub='first-sub')
        with self.assertRaises(socialauth.SocialAuthError) as ctx:
            self.link(sub='second-sub')
        self.assertEqual(ctx.exception.code, 'provider_already_connected')
        self.assertEqual(self.user.social_accounts.count(), 1)

    def test_google_and_apple_can_both_be_linked_to_one_account(self):
        self.link(provider='google', sub='g-sub')
        self.link(provider='apple', iss='https://appleid.apple.com', aud=APPLE_AUD,
                   sub='a-sub')
        self.assertEqual(self.user.social_accounts.count(), 2)


class SocialLinkEndpointTests(SocialAuthTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='linker', email='linker@example.com', password='pw-123456')
        self.client = APIClient()

    def test_requires_authentication(self):
        res = self.client.post(
            '/api/users/social/link/',
            {'provider': 'google', 'id_token': make_token()}, format='json')
        self.assertEqual(res.status_code, 401)

    def test_links_and_reports_connected_providers(self):
        self.client.force_authenticate(self.user)
        res = self.client.post(
            '/api/users/social/link/',
            {'provider': 'google', 'id_token': make_token(sub='linked-sub')},
            format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['connected_providers'], ['google'])
        self.assertTrue(
            SocialAccount.objects.filter(
                user=self.user, provider='google', subject='linked-sub').exists())

    def test_conflict_with_another_account_is_a_409(self):
        other = User.objects.create_user(
            username='other', email='other@example.com', password='pw-123456')
        SocialAccount.objects.create(
            provider='google', subject='taken-sub', user=other)
        self.client.force_authenticate(self.user)
        res = self.client.post(
            '/api/users/social/link/',
            {'provider': 'google', 'id_token': make_token(sub='taken-sub')},
            format='json')
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data['code'], 'identity_already_linked')

    def test_bad_token_is_a_400_with_a_code(self):
        self.client.force_authenticate(self.user)
        res = self.client.post(
            '/api/users/social/link/',
            {'provider': 'google', 'id_token': 'nonsense'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'invalid_token')


class AgeGateTests(TestCase):
    """The date of birth a social sign-up owes us arrives as a profile PATCH,
    so the 13+ floor has to hold on that route and not only at registration."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='kid', email='kid@example.com', password='pw-123456')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_under_thirteen_rejected_on_profile_patch(self):
        too_young = timezone.now().date() - timedelta(days=365 * 10)
        res = self.client.patch(
            '/api/users/profile/', {'date_of_birth': too_young.isoformat()},
            format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('date_of_birth', res.data)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.date_of_birth)

    def test_old_enough_accepted(self):
        dob = timezone.now().date() - timedelta(days=365 * 20)
        res = self.client.patch(
            '/api/users/profile/', {'date_of_birth': dob.isoformat()}, format='json')
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.date_of_birth, dob)
        self.assertEqual(self.user.age_tier, User.AgeTier.ADULT)

    def test_under_thirteen_rejected_at_registration(self):
        too_young = timezone.now().date() - timedelta(days=365 * 9)
        res = APIClient().post('/api/users/register/', {
            'username': 'young', 'email': 'young@example.com',
            'date_of_birth': too_young.isoformat(),
            'password': 'a-real-password-9', 'password2': 'a-real-password-9',
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('date_of_birth', res.data)
