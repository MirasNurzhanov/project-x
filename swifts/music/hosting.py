"""Swift music streams from Approx's own storage, never from a provider.

App Review asked (guideline 5.2.3) whether the app gives access to third-party
audio streaming or catalogue services. The Swift catalogue's answer is that it
is Approx's own library: each track is licensed, and its audio is served from
Approx's media storage. An imported row does not satisfy the second half on its
own — `sync` stores whatever URL the provider hands over, which is typically a
stream or preview on the provider's CDN — and serving that URL would have the
app fetching music from the provider every time someone opened the picker or
watched a Swift.

So a track is only *servable* once its audio is on a host in `media_hosts()`:
the picker does not list it, a Swift cannot attach it, and a Swift that already
uses it plays no music bed. Artwork follows the same rule but only costs the
picture — a self-hosted track with provider artwork shows without any.
"""
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import Q


def media_hosts():
    """Every host Approx serves music from, lower-cased."""
    hosts = {host.strip().lower() for host in getattr(settings, 'SWIFT_MUSIC_MEDIA_HOSTS', ())}
    public = urlsplit(getattr(settings, 'R2_PUBLIC_URL', '') or '').netloc.lower()
    if public:
        hosts.add(public)
    hosts.discard('')
    return frozenset(hosts)


def is_self_hosted(url):
    """Whether `url` is an https URL on Approx's own storage."""
    if not url:
        return False
    parts = urlsplit(url)
    return parts.scheme == 'https' and parts.netloc.lower() in media_hosts()


def self_hosted_q(field='audio_url'):
    """The database form of `is_self_hosted`, for filtering a queryset.

    Prefix matches on `https://<host>/`, so a lookalike such as
    `media.example.com.evil.test` cannot pass for `media.example.com`.
    """
    condition = Q(pk__in=[])
    for host in media_hosts():
        condition |= Q(**{f'{field}__istartswith': f'https://{host}/'})
    return condition


def servable_artwork(url):
    """`url` when it may be shown, otherwise an empty string."""
    return url if is_self_hosted(url) else ''
