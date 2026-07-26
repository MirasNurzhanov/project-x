"""Profile link validation, normalization and platform recognition.

Two rules govern this whole module:

1. **The server never fetches a user-supplied URL.** Not for a favicon, not
   for OpenGraph metadata, not to check the link is alive. Doing so would be
   an SSRF primitive pointed at our own network, and it would leak to the link
   owner exactly when someone viewed the profile. Recognition is therefore
   pure offline pattern-matching on the parsed host and path.

2. **What we store is rebuilt from parsed components, never the raw string.**
   `https://instagram.com@evil.com/x` has hostname `evil.com`; echoing the
   input back would let a link display as one site and open another.
   Reconstructing from `hostname` + `path` makes that impossible.
"""
import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

MAX_LINKS_PER_USER = 5
MAX_URL_LENGTH = 300
MAX_LABEL_LENGTH = 64


class LinkError(ValueError):
    """Rejected link, with a message safe to show the user."""


# ─── Hosts we refuse outright ─────────────────────────────────────────────────
BLOCKED_HOSTNAMES = {
    'localhost',
    'localhost.localdomain',
    'ip6-localhost',
    'ip6-loopback',
    'metadata',
    'metadata.google.internal',
    'instance-data',
}

BLOCKED_SUFFIXES = (
    '.local',
    '.localhost',
    '.internal',
    '.localdomain',
    '.home.arpa',
    '.lan',
    '.corp',
    '.intranet',
)

# Query parameters that identify the person clicking rather than the content.
TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'utm_id', 'utm_name', 'utm_reader', 'utm_social', 'utm_brand',
    'fbclid', 'gclid', 'dclid', 'msclkid', 'twclid', 'ttclid', 'yclid',
    'igshid', 'igsh', 'si', 'ref', 'ref_src', 'ref_url', 'referrer',
    'mc_cid', 'mc_eid', 'spm', 'scm', '_branch_match_id', 'share_id',
    'feature', 'app', 'is_from_webapp', 'source_application',
}


# ─── Platform recognition ─────────────────────────────────────────────────────
# (platform key, matching hostnames). Order matters only for readability;
# hostnames are unique across entries.
PLATFORM_HOSTS = {
    'instagram': ('instagram.com', 'instagr.am'),
    'spotify': ('spotify.com', 'open.spotify.com'),
    'telegram': ('t.me', 'telegram.me', 'telegram.org'),
    'tiktok': ('tiktok.com', 'vm.tiktok.com'),
    'youtube': ('youtube.com', 'youtu.be', 'm.youtube.com'),
    'x': ('x.com', 'twitter.com', 'mobile.twitter.com'),
    'github': ('github.com',),
}

PLATFORM_LABELS = {
    'instagram': 'Instagram',
    'spotify': 'Spotify',
    'telegram': 'Telegram',
    'tiktok': 'TikTok',
    'youtube': 'YouTube',
    'x': 'X',
    'github': 'GitHub',
    'website': 'Website',
}

#: Platforms where a bare `@handle` can be expanded to a canonical profile URL.
HANDLE_TEMPLATES = {
    'instagram': 'https://instagram.com/{handle}',
    'telegram': 'https://t.me/{handle}',
    'tiktok': 'https://tiktok.com/@{handle}',
    'x': 'https://x.com/{handle}',
    'github': 'https://github.com/{handle}',
    'youtube': 'https://youtube.com/@{handle}',
}

HANDLE_RE = re.compile(r'^@?[A-Za-z0-9._-]{1,64}$')

#: Bidi overrides and invisible characters. All of these change how a string
#: renders without changing what it resolves to.
SPOOFING_CHARS = frozenset(
    '​‌‍\u200E\u200F'   # zero-width, LTR/RTL marks
    '\u202A\u202B\u202C\u202D\u202E'   # embedding / override
    '\u2066\u2067\u2068\u2069'         # isolates
    '﻿­᠎'               # BOM, soft hyphen, Mongolian vowel sep
)

#: Path segments that are site furniture rather than a username.
NON_HANDLE_SEGMENTS = {
    'p', 'reel', 'reels', 'stories', 'explore', 'watch', 'shorts', 'channel',
    'c', 'user', 'playlist', 'track', 'album', 'artist', 'episode', 'show',
    'i', 'status', 'home', 'about', 'search', 's',
}


def _platform_for_host(hostname):
    bare = hostname[4:] if hostname.startswith('www.') else hostname
    for platform, hosts in PLATFORM_HOSTS.items():
        if bare in hosts:
            return platform
        # Subdomains of a known host (open.spotify.com, m.youtube.com, ...).
        for host in hosts:
            if bare.endswith('.' + host):
                return platform
    return 'website'


def _handle_from_path(platform, path):
    """First path segment, when it reads like a username. Offline only."""
    # Only known platforms have usernames in the path. Guessing one for an
    # arbitrary site would let https://evil.com/x render as "@x", hiding the
    # host the link actually opens.
    if platform == 'website':
        return None
    segments = [s for s in path.split('/') if s]
    if not segments:
        return None
    first = segments[0].lstrip('@')
    if not first or first.lower() in NON_HANDLE_SEGMENTS:
        return None
    if not HANDLE_RE.match(first):
        return None
    # Spotify paths are ids, not names — /user/xyz is the only nameable form.
    if platform == 'spotify':
        if len(segments) >= 2 and segments[0].lower() == 'user':
            return segments[1]
        return None
    if platform == 'youtube' and not segments[0].startswith('@'):
        return None
    return first


def display_label(platform, hostname, handle):
    """What the profile shows instead of a raw URL."""
    if handle:
        return f'@{handle}'
    if platform != 'website':
        return PLATFORM_LABELS[platform]
    return hostname[4:] if hostname.startswith('www.') else hostname


# ─── Validation ───────────────────────────────────────────────────────────────
def _reject_bad_host(hostname):
    if not hostname:
        raise LinkError('That link has no website address in it.')

    lowered = hostname.lower().rstrip('.')

    # An IP literal is never a legitimate profile link, and is the usual way
    # to aim a URL at a private network.
    try:
        ipaddress.ip_address(lowered.strip('[]'))
    except ValueError:
        pass
    else:
        raise LinkError('Links to IP addresses aren’t allowed.')

    if lowered in BLOCKED_HOSTNAMES or lowered.endswith(BLOCKED_SUFFIXES):
        raise LinkError('That link points to an internal address.')

    # Single-label hosts ("intranet", "router") only resolve on a private
    # network, so they cannot be a public profile.
    if '.' not in lowered:
        raise LinkError('That doesn’t look like a public web address.')

    if lowered.startswith('.') or '..' in lowered:
        raise LinkError('That web address isn’t valid.')

    return lowered


def _strip_tracking(query):
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    return urlencode(kept)


def normalize_link(raw, platform_hint=None):
    """Validate and canonicalize one link.

    Returns `(url, platform, handle, label)`. Raises LinkError with a
    user-safe message on anything rejected.

    `platform_hint` lets a bare `@handle` typed into a platform-specific field
    be expanded — typing `@someone` into the Instagram row should just work.
    """
    if raw is None:
        raise LinkError('Enter a link.')

    value = str(raw).strip()
    if not value:
        raise LinkError('Enter a link.')
    if len(value) > MAX_URL_LENGTH:
        raise LinkError(f'Links must be under {MAX_URL_LENGTH} characters.')

    # Control characters can be used to hide the real destination in display.
    # The Unicode ones matter as much as the ASCII ones: RIGHT-TO-LEFT OVERRIDE
    # reverses how the rest of the string renders, so a link to `moc.live/x`
    # can display as something else entirely, and zero-width characters hide
    # inside a hostname without showing up at all.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise LinkError('That link contains invalid characters.')
    if any(c in SPOOFING_CHARS for c in value):
        raise LinkError('That link contains invalid characters.')

    # A bare handle only means something if we know which platform it is for.
    if HANDLE_RE.match(value) and '.' not in value:
        hint = (platform_hint or '').lower()
        template = HANDLE_TEMPLATES.get(hint)
        if template is None:
            raise LinkError('Enter a full link, or pick a platform for that handle.')
        value = template.format(handle=value.lstrip('@'))

    # No scheme at all: assume https rather than rejecting a valid-looking host.
    if '://' not in value:
        value = 'https://' + value

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme not in ('http', 'https'):
        # javascript:, data:, file:, intent:, ...
        raise LinkError('Only https links are allowed.')

    hostname = _reject_bad_host(parsed.hostname)

    if parsed.port not in (None, 80, 443):
        raise LinkError('Links with a custom port aren’t allowed.')

    # A trailing slash is not a different page; normalizing it here is what
    # makes the per-user uniqueness constraint actually catch duplicates.
    path = (parsed.path or '').rstrip('/')

    platform = _platform_for_host(hostname)
    handle = _handle_from_path(platform, path)

    # Rebuilt from parsed parts: this is what drops any user:password@ prefix,
    # the port, and the fragment, so the stored URL cannot disagree with the
    # host we validated.
    canonical = urlunparse((
        'https',
        hostname,
        path,
        '',
        _strip_tracking(parsed.query),
        '',
    ))

    if len(canonical) > MAX_URL_LENGTH:
        raise LinkError(f'Links must be under {MAX_URL_LENGTH} characters.')

    return canonical, platform, handle or '', display_label(platform, hostname, handle)
