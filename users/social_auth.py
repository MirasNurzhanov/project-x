"""Sign in with Google and Sign in with Apple.

Both providers hand the phone an **identity token** — a JWT the provider
itself signed, after authenticating the person on-device. Neither is trusted
on its say-so: this module re-verifies the signature against the provider's
own published keys and checks who it was issued *for* before reading a single
claim out of it. Only once that holds does the token's email become a claim
this server will act on.

Google and Apple disagree about what they hand over, which is why the two
verifiers return the same shape rather than the caller branching on provider
past this point:

  * Google's token carries email, name and a picture URL every time.
  * Apple's carries only email (and only a private relay address if the
    person chose to hide their real one) — never a name, never a picture.
    The one time Apple's own sheet *does* surface a name is the very first
    authorization, and it hands that to the app directly rather than putting
    it in the token; the app forwards it here as `full_name` because the
    server has no second chance to ask.
"""
import random
import string

import jwt
import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils.text import slugify
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from .models import User


class SocialAuthError(Exception):
    """A token failed verification, or the request was malformed.

    Carries the HTTP status the view should answer with, and a machine
    `code` naming *which* failure this is — "no email on the token" and
    "signature verification failed" are both otherwise just "401", and the
    client's messaging (and a developer's grep) needs to tell them apart.
    """

    def __init__(self, message, status=401, code='invalid_token'):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _claims(email, name, picture=None):
    return {'email': email, 'name': name or '', 'picture': picture}


def verify_google_token(id_token_str):
    """The payload of a Google identity token, once its signature and
    audience have checked out."""
    try:
        payload = google_id_token.verify_oauth2_token(
            id_token_str, google_requests.Request(), audience=settings.GOOGLE_CLIENT_ID,
        )
    except ValueError as exc:
        raise SocialAuthError('That Google sign-in could not be verified.') from exc

    email = payload.get('email')
    if not email:
        raise SocialAuthError('Google did not share an email address.', code='no_email')
    # `email_verified` is Google's own attestation that the address on the
    # token is real, not merely typed in somewhere — the one thing this flow
    # cannot ask the person to confirm itself, since there is no password
    # step to gate on it.
    if not payload.get('email_verified'):
        raise SocialAuthError(
            "That Google account's email address is not verified.", code='email_unverified',
        )

    return _claims(email, payload.get('name'), payload.get('picture'))


#: Apple rotates its signing keys slowly and publishes them at a fixed URL.
#: PyJWKClient does its own fetch-and-cache, so this is created once per
#: process rather than per request.
_apple_jwks_client = jwt.PyJWKClient('https://appleid.apple.com/auth/keys')


def verify_apple_token(identity_token, full_name=''):
    """The payload of an Apple identity token, once its signature, audience
    and issuer have checked out. `full_name` is not part of the token — see
    the module docstring — and is passed through as-is."""
    try:
        signing_key = _apple_jwks_client.get_signing_key_from_jwt(identity_token)
        payload = jwt.decode(
            identity_token,
            signing_key.key,
            algorithms=['RS256'],
            audience=settings.APPLE_BUNDLE_ID,
            issuer='https://appleid.apple.com',
        )
    except jwt.PyJWTError as exc:
        raise SocialAuthError('That Apple sign-in could not be verified.') from exc

    email = payload.get('email')
    if not email:
        raise SocialAuthError('Apple did not share an email address.', code='no_email')

    return _claims(email, full_name)


def _base_username(name, email):
    source = name.strip() or email.split('@', 1)[0]
    # `slugify` lowercases and drops anything that is not `[a-z0-9-]`, which
    # is stricter than Django needs but the same alphabet every other
    # username in this app already uses.
    base = slugify(source).replace('-', '')[:20]
    return base or 'user'


def _unique_username(name, email):
    base = _base_username(name, email)
    if not User.objects.filter(username=base).exists():
        return base
    # A four-digit suffix is tried first because it reads like a normal
    # username someone might have picked themselves; the random-alphanumeric
    # fallback below is deliberately ugly; it should never actually fire.
    for _ in range(20):
        candidate = f'{base}{random.randint(1000, 9999)}'
        if not User.objects.filter(username=candidate).exists():
            return candidate
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f'{base}{suffix}'


def _attach_picture(user, picture_url):
    """Best-effort only: a broken fetch of someone's Google avatar must not
    fail the sign-in that was otherwise perfectly good."""
    try:
        response = requests.get(picture_url, timeout=5)
        response.raise_for_status()
    except requests.RequestException:
        return
    user.profile_picture.save(f'{user.pk}.jpg', ContentFile(response.content), save=True)


def find_or_create_social_user(claims):
    """The account behind a verified token's email, creating one if this is
    its first time reaching Aura. Returns `(user, created)`.

    Matched case-insensitively: neither provider is guaranteed to report the
    same case a password account was originally registered under, and two
    accounts for one mailbox is a worse outcome than a slightly loose match.
    """
    email = claims['email']
    try:
        return User.objects.get(email__iexact=email), False
    except User.DoesNotExist:
        pass

    user = User(email=email, username=_unique_username(claims['name'], email))
    # No password this account will ever be asked for — the provider is the
    # only door in. `set_unusable_password` is Django's own marker for that,
    # not a blank/guessable value standing in for one.
    user.set_unusable_password()
    user.save()

    if claims.get('picture'):
        _attach_picture(user, claims['picture'])

    return user, True
