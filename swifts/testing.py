"""Fixtures shared by the Swift test suites.

Kept out of any one module because the location tests and the engagement tests
need the same people and the same Swifts, and a second copy of `make_user` is
how two suites end up disagreeing about what a default is.
"""
from datetime import date

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from friends.models import BlockedUser, FriendRequest, HiRequest
from users.models import User
from users.testing import share_location

from .models import SwiftVideo

#: A coordinate to attach. Any pair will do; these are just recognisable.
LAT, LON = '43.234512', '76.956231'


class SwiftTestCase(TestCase):
    """A TestCase that starts with an empty throttle bucket.

    DRF's `ScopedRateThrottle` keeps its counters in the Django cache, keyed on
    the user's primary key. Two things make that a problem for a suite rather
    than for production: the cache is a process-wide LocMemCache that no
    transaction rolls back, and `TestCase` reuses primary keys across tests
    because each one rolls its rows away. So user pk 3 in one test inherits the
    quota that user pk 3 spent in the last one, and a suite that grows past the
    limit starts failing in whichever test happens to run last — which is the
    worst kind of flake, because it moves when you add tests.

    Clearing here rather than in each class for the same reason
    `core/test_query_budgets.py` does it: forgetting is silent.
    """

    def setUp(self):
        cache.clear()
        super().setUp()


def make_user(name, *, sharing=True, **extra):
    """A user, on the map by default.

    `location_sharing` defaults to PAUSED (see `users.visibility`), and a
    paused creator has every coordinate they send dropped at write time — which
    is correct, and which would silently make every location test measure the
    default instead of the thing it is about. Tests that care about the paused
    path pass `sharing=False` and say so.
    """
    user = User.objects.create_user(
        username=name, email=f'{name}@example.com', password='testpass-123',
        date_of_birth=date(1995, 1, 1), **extra
    )
    return share_location(user) if sharing else user


def befriend(a, b):
    FriendRequest.objects.create(
        sender=a, receiver=b, status=FriendRequest.Status.ACCEPTED,
    )


def connect(a, b):
    """An accepted Hi — the tier below friendship."""
    HiRequest.objects.create(
        sender=a, receiver=b, status=HiRequest.Status.ACCEPTED,
    )


def block(a, b):
    BlockedUser.objects.create(blocker=a, blocked=b)


def fake_video(name='clip.mp4', content=b'not a real video, just enough bytes'):
    """A file upload for `POST /api/swifts/`, which requires one since the R2
    migration — see `swifts.serializers.SwiftCreateSerializer.video`."""
    return SimpleUploadedFile(name, content, content_type='video/mp4')


def fake_image(name='cover.jpg', content=b'not a real jpeg either'):
    return SimpleUploadedFile(name, content, content_type='image/jpeg')


def make_swift(author, *, located=False, shared=False, **extra):
    """A ready, public Swift by `author`.

    `located` puts a coordinate on it; `shared` additionally turns on the
    creator's permission for anyone to ask where it was. The two are separate
    because the interesting cases are the ones where they disagree — a Swift
    with a coordinate and sharing off is exactly requirement 7.
    """
    fields = {
        'video_url': 'https://cdn.example.com/v/abc.mp4',
        'thumbnail_url': 'https://cdn.example.com/v/abc.jpg',
        'duration_ms': 15_000,
        'width': 1080,
        'height': 1920,
        'caption': 'a swift',
    }
    if located:
        fields['latitude'] = LAT
        fields['longitude'] = LON
        fields['place_name'] = 'Kok-Tobe Park'
        fields['location_shared'] = shared
    fields.update(extra)
    return SwiftVideo.objects.create(author=author, **fields)


#: Every key that must never appear in a feed or detail payload, at any depth.
#: One list, used by every leak assertion, so a new forbidden field is added in
#: one place rather than in each test that happens to remember it.
FORBIDDEN_LOCATION_KEYS = frozenset({
    'latitude', 'longitude', 'lat', 'lng', 'lon',
    'place_name', 'place', 'coordinates', 'location',
})


def find_forbidden(payload, path='response'):
    """Every forbidden key found anywhere inside `payload`, with its path.

    Walks the whole structure rather than checking the top level, because the
    author block, a nested comment or a future embedded object are all places a
    coordinate could reappear without anyone editing the field list they were
    thinking about.
    """
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in FORBIDDEN_LOCATION_KEYS:
                found.append(f'{path}.{key}')
            found.extend(find_forbidden(value, f'{path}.{key}'))
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            found.extend(find_forbidden(item, f'{path}[{i}]'))
    return found
