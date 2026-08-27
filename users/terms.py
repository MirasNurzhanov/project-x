"""The agreement every account is held to, and the version of it they agreed to.

App Review's 1.2 requirement is not "publish a policy" — it is that a user
*explicitly agrees* to terms which state there is no tolerance for objectionable
content or abusive users, and that the agreement is recorded. So the version
string is the whole mechanism: acceptance is stored against a version, and a
user whose stored version is not the current one is treated as not having
agreed at all.

`CURRENT_VERSION` is a date, not a counter, because the question a moderator or
a regulator asks is "which text did they agree to", and a date answers it
against the published document without a lookup table. Bump it **only** when the
substance changes — a typo fix that re-prompts every account on the service is
worse than the typo.

The URLs live here rather than in the client so that a change of hosting does
not require an app release: `GET /api/users/terms/` hands the app whatever this
module says, and the app opens what it is given.
"""
from django.conf import settings

#: Bump when the substance of the agreement changes. ISO date of publication.
CURRENT_VERSION = '2026-08-25'

#: Where the documents live. This backend serves them itself at /legal/ - see
#: moderation/legal_views.py for why - so the default is its own origin, and
#: `LEGAL_BASE_URL` is what a marketing domain would later change.
_DEFAULT_BASE = 'https://project-x-production-3261.up.railway.app'


def _base():
    return (getattr(settings, 'LEGAL_BASE_URL', '') or _DEFAULT_BASE).rstrip('/')


def document_urls():
    """The three documents the sign-up gate links, in the order it shows them.

    `privacy_url` is separately overridable because the privacy policy is the
    one document that may already be published elsewhere - it is the URL App
    Store Connect holds, and moving it would break that listing. The other two
    are new and have no such history.
    """
    base = _base()
    return {
        'terms_url': f'{base}/legal/terms/',
        'guidelines_url': f'{base}/legal/guidelines/',
        'privacy_url': (
            getattr(settings, 'LEGAL_PRIVACY_URL', '') or f'{base}/legal/privacy/'
        ),
    }


def current_terms():
    """What `GET /api/users/terms/` answers, and what the gate renders from."""
    return {'version': CURRENT_VERSION, **document_urls()}


def terms_enforced_for(request):
    """Whether *this* request's client is one the login gate applies to.

    Three-way, and the third case is the whole point:

        build >= MIN      enforce. The client knows how to show the agreement
                          and how to send `accept_terms`.
        build <  MIN      do not enforce. A client that predates the mechanism.
        no build header   do not enforce *while the window is open*. This is
                          build 31, which is in review and in users' hands.

    An absent header is deliberately **not** read as "current". Defaulting it
    forward would enforce against exactly the clients that cannot comply, which
    is the lockout this function exists to prevent.

    ── On forgery ────────────────────────────────────────────────────────────
    The header is client-asserted, so a *modified* build 32 could omit it and be
    treated as legacy. That is worth stating plainly rather than papering over:

      * It buys nothing an attacker wants. The gate protects a compliance
        obligation, not data — skipping it grants no access, reveals nothing,
        and still leaves `TermsGate` in front of the app on first launch.
      * It cannot be closed by making the header cleverer. Anything the client
        sends, a modified client can change. Signing it would only move the
        secret into the same bundle.
      * It closes on its own. Set `TERMS_ENFORCEMENT_ALLOW_LEGACY=False` once
        build 31 is off the store and headerless requests are enforced too,
        which leaves no unenforced path at all. That is the real fix and it is
        one environment variable.

    The genuine users this protects — people on build 31 who cannot update
    yet — are the reason the window exists, and they are the reason it should
    be shut deliberately rather than left open.
    """
    from django.conf import settings

    from .clientinfo import client_build

    build = client_build(request)
    if build is None:
        # Legacy, for as long as the window is open.
        return not getattr(settings, 'TERMS_ENFORCEMENT_ALLOW_LEGACY', True)
    return build >= getattr(settings, 'TERMS_ENFORCEMENT_MIN_BUILD', 32)


def has_accepted(user):
    """Whether `user` has agreed to the agreement as it stands right now.

    Both halves are required. A row with a version but no timestamp is a
    half-written acceptance from a failed save, and a row with a timestamp but
    a stale version is somebody who agreed to a document we no longer offer —
    neither is consent to the current text.
    """
    return bool(user.terms_accepted_at) and user.terms_version == CURRENT_VERSION


def record_acceptance(user, *, save=True):
    """Stamp the current version and time onto `user`.

    Idempotent by construction: re-accepting the same version only moves the
    timestamp, which is the honest record of the most recent time they were
    shown it and agreed.
    """
    from django.utils import timezone

    user.terms_version = CURRENT_VERSION
    user.terms_accepted_at = timezone.now()
    if save:
        user.save(update_fields=['terms_version', 'terms_accepted_at'])
    return user
