"""Turning provider tracks into catalogue rows.

Two rules, both deliberate and both about not letting an import decide what
creators can use:

**A sync never publishes.** New rows land with `is_active=False`, exactly as if
somebody had typed them into the admin. Publishing stays a person's act, after
they have read the licence — which is the whole argument for `is_active`
existing (see `SwiftTrack`). An import that switched tracks on would make that
gate decorative.

**A re-sync updates, it does not duplicate.** Rows are matched on
`(provider, provider_track_id)`, which is why those columns exist. And a
re-sync deliberately leaves `is_active`, `priority` and `cloudinary_id` alone:
those are Aura's editorial decisions about a track, not facts the provider owns,
and an upstream metadata change must not silently unpublish something or throw
away a mirrored file.
"""
from dataclasses import dataclass

from django.utils import timezone

from ..models import SwiftTrack
from . import registry


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def total(self):
        return self.created + self.updated

    def __str__(self):
        return (
            f'{self.created} created, {self.updated} updated, '
            f'{self.skipped} skipped'
        )


#: Fields a re-sync is allowed to overwrite. Everything absent from this list
#: is Aura's and survives an upstream change — see the module note.
PROVIDER_OWNED_FIELDS = (
    'title', 'artist', 'genre', 'duration_ms', 'audio_url', 'artwork_url',
    'licence', 'attribution',
)


def sync_provider(key, *, term='', limit=50, offset=0, dry_run=False):
    """Import one page of a provider's catalogue into `SwiftTrack`."""
    provider = registry.get(key)
    tracks = provider.search(term=term, limit=limit, offset=offset)
    return apply_tracks(key, tracks, dry_run=dry_run)


def apply_tracks(key, tracks, *, dry_run=False):
    result = SyncResult()
    now = timezone.now()

    for track in tracks:
        if not track.title or not track.audio_url:
            # A row with no title is unpickable and one with no audio is
            # unplayable. Either way it is not a track yet.
            result.skipped += 1
            continue

        existing = SwiftTrack.objects.filter(
            provider=key, provider_track_id=track.external_id,
        ).first()

        if dry_run:
            if existing:
                result.updated += 1
            else:
                result.created += 1
            continue

        values = {
            'title': track.title,
            'artist': track.artist,
            'genre': track.genre,
            'duration_ms': track.duration_ms,
            'audio_url': track.audio_url,
            'artwork_url': track.artwork_url,
            'licence': track.licence,
            'attribution': track.attribution,
            'synced_at': now,
        }

        if existing:
            for field in PROVIDER_OWNED_FIELDS:
                setattr(existing, field, values[field])
            existing.synced_at = now
            existing.save(update_fields=[*PROVIDER_OWNED_FIELDS, 'synced_at'])
            result.updated += 1
        else:
            SwiftTrack.objects.create(
                provider=key,
                provider_track_id=track.external_id,
                source=SwiftTrack.Source.LICENSED,
                # Off. A person publishes it. See the module note.
                is_active=False,
                **values,
            )
            result.created += 1

    return result
