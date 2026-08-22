"""What a music provider has to be, so that Aura is not tied to any one of them.

A provider answers two questions: *what tracks are there*, and *where is the
audio*. Everything else — licensing, publication, ordering, use counts — is
Aura's, and lives on `SwiftTrack`.

The important structural decision is that providers **sync into the local
catalogue rather than being proxied at request time.** A track picker that
called an external API on every keystroke would be slow, would burn a partner
rate limit on people scrolling, and would go dark whenever that partner did —
during composition, which is the worst possible moment. So a provider's job is
to hand over a list of `ProviderTrack`s; `swifts.music.sync` writes them into
`SwiftTrack` rows, and the picker only ever reads our own table.

It also means the licence terms are captured at import, once, by a person who
read them — rather than being assumed on every request.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderTrack:
    """One track as a provider describes it, before it becomes a SwiftTrack.

    Deliberately not a Django model. A provider returns facts about a track;
    whether Aura *publishes* it is a separate decision made by a person, and
    keeping the two apart is what stops a sync silently putting unvetted audio
    in front of creators.
    """

    #: The provider's own identifier. Stable, and what a re-sync matches on.
    external_id: str
    title: str
    artist: str = ''
    genre: str = ''
    duration_ms: int = 0
    #: Streamable audio. For most providers this is a time-limited URL, which
    #: is why `sync` can be asked to mirror it to Cloudinary instead.
    audio_url: str = ''
    artwork_url: str = ''
    #: The licence this track is available under, in words a person can check.
    #: Never blank — a track with no licence statement is not importable.
    licence: str = ''
    #: The credit line the licence requires, if any.
    attribution: str = ''
    #: Anything provider-specific worth keeping for support or debugging.
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.external_id:
            raise ValueError('a provider track needs an external_id')
        if not self.licence:
            raise ValueError(
                f'track {self.external_id!r} has no licence statement — '
                'a track Aura cannot say it has the right to use is not importable'
            )


class MusicProviderError(RuntimeError):
    """The provider could not answer.

    Carries a `code` naming which failure this is, the same way
    `users.applemusic.AppleMusicError` does — "not configured", "credentials
    rejected" and "upstream is down" are three different things to go and fix,
    and a bare "unavailable" sends somebody hunting.
    """

    def __init__(self, message, code='unavailable', status=502):
        super().__init__(message)
        self.code = code
        self.status = status


class ProviderNotConfigured(MusicProviderError):
    def __init__(self, name, missing=()):
        names = ', '.join(missing)
        super().__init__(
            f'{name} is not configured on this server.'
            + (f' Missing: {names}.' if names else ''),
            code='not_configured',
            status=503,
        )


class MusicProvider:
    """The interface `swifts.music.sync` works against."""

    #: Stable key stored on `SwiftTrack.provider`. Never change one in place —
    #: it is what a re-sync matches existing rows on.
    key = ''
    #: Shown in the admin and in the provider status endpoint.
    label = ''

    def is_configured(self) -> bool:
        raise NotImplementedError

    def missing_settings(self) -> tuple:
        """Which credentials are absent, named individually.

        Named rather than counted, for the same reason `users.applemusic` does
        it: "missing EPIDEMIC_SOUND_CLIENT_SECRET" is a fix, and "not
        configured" is a scavenger hunt across three variables set in three
        different places.
        """
        return ()

    def search(self, term='', limit=50, offset=0):
        """Tracks matching `term`, as `ProviderTrack`s."""
        raise NotImplementedError

    def fetch(self, external_id):
        """One track by the provider's own id, or None."""
        raise NotImplementedError
