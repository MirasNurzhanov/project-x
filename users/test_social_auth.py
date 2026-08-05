"""Sign in with Google and Sign in with Apple.

Neither provider is actually called: `verify_oauth2_token` and the Apple JWKS
round trip are mocked at the boundary, because what these tests are pinning
is Aura's own behaviour once a token is (or is not) genuine — not whether
Google or Apple's servers are up.
"""
from datetime import date
from unittest.mock import patch

import jwt
from django.test import TestCase
from rest_framework.test import APIClient

from users.models import User
from users.social_auth import (
    SocialAuthError,
    _unique_username,
    find_or_create_social_user,
    verify_apple_token,
    verify_google_token,
)


def google_payload(**overrides):
    payload = {
        'email': 'nur@example.com',
        'email_verified': True,
        'name': 'Nur Sultan',
        'picture': 'https://example.com/avatar.jpg',
    }
    payload.update(overrides)
    return payload


class VerifyGoogleTokenTests(TestCase):
    @patch('users.social_auth.google_id_token.verify_oauth2_token')
    def test_valid_token_yields_claims(self, verify):
        verify.return_value = google_payload()
        claims = verify_google_token('token')
        self.assertEqual(claims['email'], 'nur@example.com')
        self.assertEqual(claims['name'], 'Nur Sultan')
        self.assertEqual(claims['picture'], 'https://example.com/avatar.jpg')

    @patch('users.social_auth.google_id_token.verify_oauth2_token')
    def test_bad_signature_is_rejected(self, verify):
        verify.side_effect = ValueError('Token used too late')
        with self.assertRaises(SocialAuthError) as cm:
            verify_google_token('token')
        self.assertEqual(cm.exception.code, 'invalid_token')
        self.assertEqual(cm.exception.status, 401)

    @patch('users.social_auth.google_id_token.verify_oauth2_token')
    def test_missing_email_is_rejected(self, verify):
        verify.return_value = google_payload(email=None)
        with self.assertRaises(SocialAuthError) as cm:
            verify_google_token('token')
        self.assertEqual(cm.exception.code, 'no_email')

    @patch('users.social_auth.google_id_token.verify_oauth2_token')
    def test_unverified_email_is_rejected(self, verify):
        verify.return_value = google_payload(email_verified=False)
        with self.assertRaises(SocialAuthError) as cm:
            verify_google_token('token')
        self.assertEqual(cm.exception.code, 'email_unverified')


class VerifyAppleTokenTests(TestCase):
    @patch('users.social_auth._apple_jwks_client.get_signing_key_from_jwt')
    @patch('users.social_auth.jwt.decode')
    def test_valid_token_yields_claims_with_passed_through_name(self, decode, get_key):
        decode.return_value = {'email': 'nur@privaterelay.appleid.com'}
        claims = verify_apple_token('token', full_name='Nur Sultan')
        self.assertEqual(claims['email'], 'nur@privaterelay.appleid.com')
        # Apple's token never carries a name -- this is the app-supplied one,
        # passed straight through rather than read off the payload.
        self.assertEqual(claims['name'], 'Nur Sultan')
        self.assertIsNone(claims['picture'])

    @patch('users.social_auth._apple_jwks_client.get_signing_key_from_jwt')
    @patch('users.social_auth.jwt.decode')
    def test_missing_email_is_rejected(self, decode, get_key):
        decode.return_value = {}
        with self.assertRaises(SocialAuthError) as cm:
            verify_apple_token('token')
        self.assertEqual(cm.exception.code, 'no_email')

    @patch('users.social_auth._apple_jwks_client.get_signing_key_from_jwt')
    def test_unverifiable_token_is_rejected(self, get_key):
        get_key.side_effect = jwt.exceptions.PyJWKClientError('unknown kid')
        with self.assertRaises(SocialAuthError) as cm:
            verify_apple_token('token')
        self.assertEqual(cm.exception.code, 'invalid_token')


class FindOrCreateSocialUserTests(TestCase):
    def test_creates_a_new_user_with_unusable_password(self):
        user, created = find_or_create_social_user(
            {'email': 'new@example.com', 'name': 'New Person', 'picture': None},
        )
        self.assertTrue(created)
        self.assertEqual(user.email, 'new@example.com')
        self.assertFalse(user.has_usable_password())
        self.assertIsNone(user.date_of_birth)

    def test_finds_an_existing_user_case_insensitively(self):
        existing = User.objects.create_user(
            username='nur', email='Nur@Example.com', password='pw',
        )
        user, created = find_or_create_social_user(
            {'email': 'nur@example.com', 'name': 'Nur', 'picture': None},
        )
        self.assertFalse(created)
        self.assertEqual(user.pk, existing.pk)

    def test_username_collision_gets_a_suffix_not_a_duplicate(self):
        User.objects.create_user(username='newperson', email='taken@example.com', password='pw')
        user, created = find_or_create_social_user(
            {'email': 'other@example.com', 'name': 'New Person', 'picture': None},
        )
        self.assertTrue(created)
        self.assertTrue(user.username.startswith('newperson'))
        self.assertNotEqual(user.username, 'newperson')

    def test_blank_name_falls_back_to_email_local_part(self):
        username = _unique_username('', 'quietperson@example.com')
        self.assertEqual(username, 'quietperson')


class SocialAuthViewTests(TestCase):
    """The endpoint's own contract: services/socialAuth.ts on the client
    reads `tokens.access`/`tokens.refresh`, `created` and
    `needs_date_of_birth` from exactly this shape."""

    def setUp(self):
        self.client = APIClient()

    def test_missing_provider_is_a_400(self):
        response = self.client.post('/api/users/social/', {'id_token': 'x'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'unknown_provider')

    def test_missing_token_is_a_400(self):
        response = self.client.post('/api/users/social/', {'provider': 'google'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'missing_token')

    @patch('users.views.verify_google_token')
    def test_rejected_token_surfaces_the_verifier_error(self, verify):
        verify.side_effect = SocialAuthError('nope', status=401, code='invalid_token')
        response = self.client.post(
            '/api/users/social/', {'provider': 'google', 'id_token': 'x'}, format='json',
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'invalid_token')

    @patch('users.views.verify_google_token')
    def test_new_google_user_gets_tokens_and_needs_a_birthday(self, verify):
        verify.return_value = {'email': 'brandnew@example.com', 'name': 'Brand New', 'picture': None}
        response = self.client.post(
            '/api/users/social/', {'provider': 'google', 'id_token': 'x'}, format='json',
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body['created'])
        self.assertTrue(body['needs_date_of_birth'])
        self.assertIn('access', body['tokens'])
        self.assertIn('refresh', body['tokens'])
        self.assertTrue(User.objects.filter(email='brandnew@example.com').exists())

    @patch('users.views.verify_apple_token')
    def test_returning_user_with_a_birthday_on_file_does_not_need_one(self, verify):
        User.objects.create_user(
            username='returning', email='returning@example.com', password='pw',
            date_of_birth=date(1995, 1, 1),
        )
        verify.return_value = {'email': 'returning@example.com', 'name': '', 'picture': None}
        response = self.client.post(
            '/api/users/social/', {'provider': 'apple', 'id_token': 'x'}, format='json',
        )
        body = response.json()
        self.assertFalse(body['created'])
        self.assertFalse(body['needs_date_of_birth'])

    @patch('users.views.verify_apple_token')
    def test_apple_full_name_is_forwarded_to_the_verifier(self, verify):
        verify.return_value = {'email': 'someone@example.com', 'name': 'Someone', 'picture': None}
        self.client.post(
            '/api/users/social/',
            {'provider': 'apple', 'id_token': 'x', 'full_name': 'Someone Person'},
            format='json',
        )
        verify.assert_called_once_with('x', 'Someone Person')


class SocialBirthdayAgeGateTests(TestCase):
    """The screen at app/social-birthday.tsx patches date_of_birth onto an
    already-created account -- the age gate has to hold there exactly as it
    does at registration, not just on the way in."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='social', email='social@example.com', password='',
        )
        self.user.set_unusable_password()
        self.user.save()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_under_13_is_rejected(self):
        too_young = date.today().replace(year=date.today().year - 10)
        response = self.client.patch(
            '/api/users/profile/', {'date_of_birth': too_young.isoformat()}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('date_of_birth', response.json())

    def test_13_and_over_is_saved_and_recomputes_age_tier(self):
        seventeen = date.today().replace(year=date.today().year - 17)
        response = self.client.patch(
            '/api/users/profile/', {'date_of_birth': seventeen.isoformat()}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.age_tier, User.AgeTier.MINOR)
