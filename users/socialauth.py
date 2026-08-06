"""Sign in with Google and Sign in with Apple, verified server-side.

The phone runs the provider's own SDK, which hands it an **identity token** — a
JWT the provider signed, saying "this is who just authenticated". That token is
all the phone sends here. It is not a password and it is not trusted on
arrival: anyone can POST a string to this endpoint, so the signature, the
issuer, the audience and the expiry are all checked against the provider's
published keys before a single row is touched. A token that only *looks* like
Google's is how you hand out accounts to strangers.

Why the server does the verifying at all, when the SDK already did: the SDK
proved it to the *phone*. Our API has no way to tell a real phone from curl, so
the proof has to be re-done on this side. That is the whole reason the identity
token is a signed JWT rather than an opaque handle.

Two things this module deliberately does not do:

* **Trust the email alone.** A provider subject (`sub`) is stable forever and is
  what `SocialAccount` records. Email is only the bridge used the first time,
  to attach a Google/Apple identity to a password account the same person had
  already made, and only when the provider says it verified that address.
  Matching on an unverified email would let anyone who can type your address
  into a badly-run identity provider walk into your account.
* **Ask the provider for a name.** Apple sends the display name exactly once,
  in the *authorization* response, never in the token — so it arrives as an
  ordinary request field, is used only to seed a username, and is never allowed
  to decide who the user is.
"""

import re
import unicodedata
from urllib.error import URLError

import jwt
from django.conf import settings
from django.db import IntegrityError, transaction

from .models import SocialAccount, User


class SocialAuthError(Exception):
    """The sign-in could not be completed.

    Carries the status the view should answer with and a machine-readable
    `code`, for the same reason applemusic.py does: "not configured on this
    server" and "that token is expired" are both things the app has to tell
    the user about, and they are completely different problems. The app keys
    its message off `code`, never off the prose.
    """

    def __init__(self, message, status=400, code='invalid_token'):
        super().__init__(message)
        self.status = status
        self.code = code


class ProviderNotConfigured(SocialAuthError):
    """No client ids set for this provider. A deployment fact, not a user error.

    503 rather than 400 so it never reads as "your token was bad" in a log:
    the token was never looked at.
    """

    def __init__(self, provider, setting_name):
        super().__init__(
            f'{provider.title()} sign-in is not configured on this server. '
            f'Set {setting_name}.',
            status=503,
            code='not_configured',
        )


class _Provider:
    """Everything that differs between Google and Apple, which is not much."""

    def __init__(self, name, jwks_uri, issuers, setting_name):
        self.name = name
        self.jwks_uri = jwks_uri
        self.issuers = issuers
        self.setting_name = setting_name
        # PyJWKClient caches the fetched key set and only re-fetches when it
        # sees a `kid` it does not know — which is what makes it safe to build
        # once at import and reuse. Rebuilding it per request would mean an
        # HTTPS round-trip to the provider on every sign-in.
        self._jwk_client = None

    @property
    def audiences(self):
        return [a for a in getattr(settings, self.setting_name, ()) if a]

    @property
    def jwk_client(self):
        if self._jwk_client is None:
            self._jwk_client = jwt.PyJWKClient(
                self.jwks_uri,
                cache_keys=True,
                # Providers rotate signing keys on the order of days. An hour
                # is short enough that a revoked key stops working promptly and
                # long enough that the cache does its job.
                lifespan=3600,
            )
        return self._jwk_client


PROVIDERS = {
    'google': _Provider(
        'google',
        'https://www.googleapis.com/oauth2/v3/certs',
        # Google has published tokens under both spellings for years and both
        # are documented as valid. Accepting one of them is a bug that shows up
        # only on some accounts.
        ('https://accounts.google.com', 'accounts.google.com'),
        'GOOGLE_OAUTH_CLIENT_IDS',
    ),
    'apple': _Provider(
        'apple',
        'https://appleid.apple.com/auth/keys',
        ('https://appleid.apple.com',),
        'APPLE_SIGN_IN_CLIENT_IDS',
    ),
}


def verify_identity_token(provider_name, token):
    """Check a provider identity token and return its claims.

    Raises SocialAuthError on anything short of a fully valid token.
    """
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise SocialAuthError(
            f'Unknown sign-in provider "{provider_name}".', code='unknown_provider'
        )

    audiences = provider.audiences
    if not audiences:
        raise ProviderNotConfigured(provider.name, provider.setting_name)

    if not token or not isinstance(token, str):
        raise SocialAuthError('No identity token was sent.', code='missing_token')

    try:
        signing_key = provider.jwk_client.get_signing_key_from_jwt(token)
    except jwt.exceptions.PyJWKClientError as exc:
        # Either the provider's key endpoint is unreachable or it does not have
        # the `kid` this token names. Both are "come back in a moment", not
        # "your account is wrong", so the app is told to retry rather than to
        # go and fix something.
        raise SocialAuthError(
            f'Could not reach {provider.name.title()} to check that sign-in.',
            status=503,
            code='provider_unreachable',
        ) from exc
    except (jwt.DecodeError, ValueError) as exc:
        raise SocialAuthError('That sign-in token is malformed.') from exc
    except (URLError, OSError) as exc:
        # PyJWKClient fetches over urllib and wraps most failures in
        # PyJWKClientError above, but a socket-level one can still surface raw.
        # Belt and braces, because the alternative is a 500 for "the wifi
        # dropped".
        raise SocialAuthError(
            f'Could not reach {provider.name.title()} to check that sign-in.',
            status=503,
            code='provider_unreachable',
        ) from exc

    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=['RS256'],
            # A list: iOS and Android are separate OAuth clients with separate
            # ids, and both sign in to the same account. PyJWT passes when any
            # one of them matches.
            audience=audiences,
            options={
                'require': ['exp', 'iat', 'aud', 'iss', 'sub'],
                'verify_exp': True,
                'verify_aud': True,
            },
            # 60s, because a phone with a slightly wrong clock is common and a
            # minute of slack cannot be exploited into anything.
            leeway=60,
        )
    except jwt.ExpiredSignatureError as exc:
        raise SocialAuthError(
            'That sign-in expired. Please try again.', code='token_expired'
        ) from exc
    except jwt.InvalidAudienceError as exc:
        # The commonest misconfiguration by a mile: the app was built against
        # an OAuth client the server was never told about. Named precisely so
        # it is one look at the log rather than an afternoon.
        raise SocialAuthError(
            'That sign-in was issued for a different app.', code='audience_mismatch'
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise SocialAuthError('That sign-in could not be verified.') from exc

    if claims.get('iss') not in provider.issuers:
        raise SocialAuthError('That sign-in came from the wrong issuer.')

    return claims


def _email_from_claims(provider_name, claims):
    """The address to trust, or None.

    None is not fatal for Apple — a returning Apple user's token can omit the
    email — so callers fall back to the `sub` they already have on file.
    """
    email = (claims.get('email') or '').strip().lower()
    if not email:
        return None

    verified = claims.get('email_verified')
    # Apple sends this as the *string* "true" often enough that treating it as
    # a bool silently drops every Apple email.
    if isinstance(verified, str):
        verified = verified.lower() == 'true'
    if verified is False:
        return None
    if verified is None and provider_name == 'google':
        # Google always sends the flag. Its absence means this is not a token
        # shaped the way Google's are, so the address is not evidence.
        return None
    return email


#: Django's AbstractUser username validator: letters, digits and @ . + - _
_USERNAME_STRIP = re.compile(r'[^\w.+-]', re.UNICODE)


def _candidate_username(email, full_name):
    """A first guess at a handle, before uniqueness is applied."""
    seed = ''
    if full_name:
        seed = full_name
    elif email:
        seed = email.split('@')[0]

    # Strip accents rather than reject them: "Renée" should become "renee",
    # not fall through to the "aura" fallback below.
    seed = unicodedata.normalize('NFKD', seed).encode('ascii', 'ignore').decode()
    seed = _USERNAME_STRIP.sub('', seed.replace(' ', '')).lower()
    seed = seed[:20].strip('.+-')
    # Apple private-relay addresses are a random hex string, and a name is
    # optional, so an empty seed is a real outcome rather than a defensive
    # branch. Everyone gets *something* they can change later on the profile.
    return seed or 'aura'


def _unique_username(base):
    if not User.objects.filter(username__iexact=base).exists():
        return base
    # Bounded rather than a while-true: at some point the answer is that this
    # base is unusable, and spinning on the database is not a fix.
    for suffix in range(1, 1000):
        candidate = f'{base}{suffix}'[:150]
        if not User.objects.filter(username__iexact=candidate).exists():
            return candidate
    raise SocialAuthError(
        'Could not pick a username for that account.',
        status=500,
        code='username_exhausted',
    )


@transaction.atomic
def resolve_user(provider_name, claims, full_name=''):
    """Find, link, or create the account this identity belongs to.

    Returns `(user, created)`.
    """
    subject = claims.get('sub')
    if not subject:
        raise SocialAuthError('That sign-in identified nobody.')

    link = (
        SocialAccount.objects
        .select_related('user')
        .filter(provider=provider_name, subject=subject)
        .first()
    )
    if link:
        # Deliberately does *not* overwrite the stored email from the token.
        # People change the address on their profile, and a provider that still
        # remembers the old one must not quietly undo that.
        return link.user, False

    email = _email_from_claims(provider_name, claims)
    if email:
        existing = User.objects.filter(email__iexact=email).first()
        if existing:
            # This account already has a *different* identity from the same
            # provider attached. Rare — it takes changing the email on one side
            # — but silently swapping the link would quietly hand the account
            # to whichever provider account signed in last.
            if existing.social_accounts.filter(provider=provider_name).exists():
                raise SocialAuthError(
                    f'That email is already linked to a different '
                    f'{provider_name.title()} account.',
                    status=409,
                    code='already_linked',
                )
            # Same person, second door. They made a password account first and
            # are now tapping the Google button; linking is the only outcome
            # that is not either a duplicate account or a locked-out user.
            SocialAccount.objects.create(
                provider=provider_name, subject=subject, user=existing, email=email
            )
            return existing, False

    if not email:
        # No prior link and no address to attach one to. Nothing here can build
        # an account — a User needs a unique email — and inventing a
        # placeholder would create a row nobody can ever sign into again.
        raise SocialAuthError(
            'That account did not share an email address, so it cannot be used '
            'to create an Aura account.',
            code='email_required',
        )

    username = _unique_username(_candidate_username(email, full_name))
    user = User(username=username, email=email)
    # Not a blank password, which would be a *usable* one that any empty string
    # matches. These accounts have no password until the person sets one.
    user.set_unusable_password()
    user.save()
    try:
        SocialAccount.objects.create(
            provider=provider_name, subject=subject, user=user, email=email
        )
    except IntegrityError as exc:
        # Two taps landing at once. The transaction this runs in is about to be
        # rolled back, so the retry finds the row the winner wrote.
        raise SocialAuthError(
            'That sign-in was already being processed. Please try again.',
            status=409,
            code='race',
        ) from exc
    return user, True
