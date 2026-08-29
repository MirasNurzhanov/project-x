"""Startup checks for the AI moderation configuration.

These are `Warning`s rather than `Error`s, and the choice is deliberate. A
Django check at `ERROR` level aborts `manage.py migrate`, which is the first
thing the Procfile runs — so a mistyped variable would take the whole service
down rather than degrade one feature. That is the wrong trade here, because the
runtime already fails in the safe direction on its own: with credentials
missing, `cloudflare_ai.credentials()` raises, every job fails, and every new
Swift stays `PENDING` and therefore invisible. Nothing gets published
unscreened either way; the difference is whether the *rest* of Approx stays up
while somebody fixes a variable.

What the warnings buy is that the misconfiguration is noticed at deploy time,
in the log, next to the thing that caused it — rather than an hour later as a
pile of Swifts nobody can see and no obvious reason why.
"""
from django.conf import settings
from django.core.checks import Warning as CheckWarning, register

W001 = 'moderation.W001'
W002 = 'moderation.W002'
W003 = 'moderation.W003'


@register()
def check_ai_moderation(app_configs, **kwargs):
    from . import cloudflare_ai

    if not getattr(settings, 'SWIFT_AI_MODERATION_ENABLED', False):
        return []

    messages = []

    missing = [
        name for name in ('CLOUDFLARE_ACCOUNT_ID', 'CLOUDFLARE_API_TOKEN')
        if not (getattr(settings, name, '') or '').strip()
    ]
    if missing:
        messages.append(CheckWarning(
            'SWIFT_AI_MODERATION_ENABLED is on but '
            f'{" and ".join(missing)} '
            f'{"are" if len(missing) > 1 else "is"} not set.',
            hint=(
                'Every new Swift will be held at moderation=PENDING and stay '
                'invisible until this is fixed. Set the variables, or turn '
                'SWIFT_AI_MODERATION_ENABLED off to publish without AI '
                'screening.'
            ),
            id=W001,
        ))

    if not cloudflare_ai.transform_base():
        messages.append(CheckWarning(
            'AI moderation is on but no origin is configured for Media '
            'Transformations.',
            hint=(
                'Set CLOUDFLARE_MEDIA_TRANSFORM_BASE, or R2_PUBLIC_URL if the '
                'bucket is published on a custom domain in the same Cloudflare '
                'zone. Frames cannot be extracted without one.'
            ),
            id=W002,
        ))
    elif '.r2.dev' in cloudflare_ai.transform_base():
        # Worth its own message because it is the failure that looks like a
        # working configuration: the URL resolves, the video plays, and every
        # /cdn-cgi/media/ request 404s. r2.dev is Cloudflare's own hostname,
        # not a zone in this account, so there is nowhere to switch
        # transformations on.
        messages.append(CheckWarning(
            'Media Transformations are configured against an r2.dev address.',
            hint=(
                'Transformations are enabled per zone and r2.dev is not a zone '
                'you control, so /cdn-cgi/media/ will not be served there. '
                'Publish the bucket on a custom domain and point '
                'CLOUDFLARE_MEDIA_TRANSFORM_BASE at it.'
            ),
            id=W003,
        ))

    return messages
